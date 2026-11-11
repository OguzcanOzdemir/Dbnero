from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from dbnero.core import models as m
from dbnero.storage.catalog import BackupFilter, Catalog, JobRecord, NotFound
from dbnero.storage.migrations import LATEST_VERSION, current_version


def _profile(**kw):
    defaults = dict(name="prod-pg", engine=m.Engine.POSTGRESQL, host="db", port=5432, username="u")
    defaults.update(kw)
    return m.ConnectionProfile(**defaults)


def _backup(conn_id="c1", db="crm", **kw):
    defaults = dict(
        connection_id=conn_id,
        engine=m.Engine.POSTGRESQL,
        database_name=db,
        server_label="db:5432",
        backup_format="pg_custom",
        file_path=Path(f"/arch/{db}.dump"),
    )
    defaults.update(kw)
    return m.BackupRecord(**defaults)


def test_migrated_to_latest(catalog):
    with catalog._engine.connect() as conn:
        assert current_version(conn) == LATEST_VERSION


def test_reopen_is_idempotent(tmp_path):
    Catalog(tmp_path / "c.db").close()
    Catalog(tmp_path / "c.db").close()


def test_connection_crud(catalog):
    p = _profile(options={"odbc_driver": "ODBC Driver 18 for SQL Server"})
    catalog.save_connection(p)
    got = catalog.get_connection(p.id)
    assert got.name == "prod-pg" and got.options == p.options
    assert got.created_at_utc.tzinfo is not None

    p.host = "db2"
    catalog.save_connection(p)
    assert catalog.get_connection(p.id).host == "db2"
    assert [c.id for c in catalog.list_connections()] == [p.id]

    catalog.delete_connection(p.id)
    with pytest.raises(NotFound):
        catalog.get_connection(p.id)


def test_connection_name_unique(catalog):
    catalog.save_connection(_profile())
    with pytest.raises(IntegrityError):
        catalog.save_connection(_profile())


def test_no_password_column(catalog):
    with catalog._engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql("PRAGMA table_info(connections)")}
    assert not any("pass" in c or "secret" in c for c in cols)


def test_backup_roundtrip(catalog):
    rec = _backup(
        tags=["nightly", "prod"],
        tool_versions={"pg_dump": "16.4"},
        extra={"server_file_path": "/var/opt/x.bak"},
        description="Before release",
        schema_snapshot_path=Path("/arch/s.json"),
    )
    catalog.add_backup(rec)
    got = catalog.get_backup(rec.backup_id)
    assert got == rec


def test_update_backup_and_immutable_fields(catalog):
    rec = _backup()
    catalog.add_backup(rec)
    catalog.update_backup(
        rec.backup_id, status=m.BackupStatus.SUCCEEDED, checksum_sha256="ab", file_size=10
    )
    got = catalog.get_backup(rec.backup_id)
    assert got.status is m.BackupStatus.SUCCEEDED and got.file_size == 10

    with pytest.raises(ValueError):
        catalog.update_backup(rec.backup_id, created_at_utc=m.utc_now())
    with pytest.raises(ValueError):
        catalog.update_backup(rec.backup_id, database_name="other")
    with pytest.raises(NotFound):
        catalog.update_backup("missing", status=m.BackupStatus.FAILED)


def test_created_at_trigger_blocks_raw_update(catalog):
    rec = _backup()
    catalog.add_backup(rec)
    with pytest.raises(IntegrityError), catalog._engine.begin() as conn:
        conn.exec_driver_sql("UPDATE backups SET created_at_utc = 'x'")


def test_list_backups_filters(catalog):
    now = datetime(2026, 11, 11, tzinfo=UTC)
    a = _backup(
        db="crm", created_at_utc=now - timedelta(days=2), tags=["nightly"], description="weekly crm"
    )
    b = _backup(
        db="erp",
        created_at_utc=now - timedelta(days=1),
        tags=["adhoc_1"],
        status=m.BackupStatus.FAILED,
    )
    c = _backup(conn_id="c2", db="crm", engine=m.Engine.MYSQL, created_at_utc=now)
    for r in (a, b, c):
        catalog.add_backup(r)

    ids = lambda f: [r.backup_id for r in catalog.list_backups(f)]  # noqa: E731
    assert ids(None) == [c.backup_id, b.backup_id, a.backup_id]
    assert ids(BackupFilter(database_name="crm")) == [c.backup_id, a.backup_id]
    assert ids(BackupFilter(engine=m.Engine.MYSQL)) == [c.backup_id]
    assert ids(BackupFilter(tag="nightly")) == [a.backup_id]
    assert ids(BackupFilter(tag="adhoc")) == []
    assert ids(BackupFilter(status=m.BackupStatus.FAILED)) == [b.backup_id]
    assert ids(BackupFilter(text="WEEKLY")) == [a.backup_id]
    assert ids(BackupFilter(text="%")) == []
    assert ids(BackupFilter(text="_")) == []
    assert ids(BackupFilter(created_from=now - timedelta(days=1, hours=1))) == [
        c.backup_id,
        b.backup_id,
    ]
    assert ids(BackupFilter(limit=1)) == [c.backup_id]


def test_batch_with_separate_records(catalog):
    batch = m.BackupBatch(connection_id="c1", database_names=["crm", "erp"])
    catalog.add_batch(batch)
    for db in batch.database_names:
        catalog.add_backup(_backup(db=db, batch_id=batch.batch_id, backup_mode=m.BackupMode.BATCH))
    got = catalog.list_backups(BackupFilter(batch_id=batch.batch_id))
    assert sorted(r.database_name for r in got) == ["crm", "erp"]
    assert len({r.backup_id for r in got}) == 2


def test_naive_datetime_rejected(catalog):
    with pytest.raises(Exception, match="Naive"):
        catalog.add_backup(_backup(created_at_utc=datetime(2026, 1, 1)))


def test_jobs_and_logs(catalog):
    job = JobRecord(
        job_id="j1",
        kind=m.JobKind.BACKUP,
        status=m.JobStatus.RUNNING,
        title="Backup crm",
        started_at_utc=m.utc_now(),
    )
    catalog.add_job(job)
    catalog.add_job_log("j1", "INFO", "started")
    catalog.add_job_log("j1", "ERROR", "failed")
    catalog.finish_job("j1", m.JobStatus.FAILED, "boom")
    (got,) = catalog.list_jobs()
    assert got.status is m.JobStatus.FAILED and got.finished_at_utc is not None
    assert [msg for _, _, msg in catalog.job_logs("j1")] == ["started", "failed"]


def test_upgrade_from_v1_keeps_data(tmp_path):
    """A catalog created by schema v1 is upgraded in place without data loss."""
    from sqlalchemy import create_engine, text

    from dbnero.storage.migrations import MIGRATIONS

    path = tmp_path / "old.db"
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as conn:
        for stmt in MIGRATIONS[0]:
            conn.execute(text(stmt))
        conn.exec_driver_sql("PRAGMA user_version = 1")
        conn.exec_driver_sql(
            "INSERT INTO backups (backup_id, connection_id, engine, server_label, database_name,"
            " created_at_utc, backup_format, backup_mode, file_path, status)"
            " VALUES ('b1', 'c1', 'mysql', 's', 'd', '2026-01-01T00:00:00+00:00', 'sql',"
            " 'single', '/x.sql', 'succeeded')"
        )
    eng.dispose()

    cat = Catalog(path)
    try:
        got = cat.get_backup("b1")
        assert got.extra == {} and got.database_name == "d"
    finally:
        cat.close()


def test_refuses_newer_catalog(tmp_path):
    from sqlalchemy import create_engine

    from dbnero.storage.migrations import CatalogTooNew

    path = tmp_path / "new.db"
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 999")
    eng.dispose()
    with pytest.raises(CatalogTooNew):
        Catalog(path)


def test_unicode_case_insensitive_search(catalog):
    rec = _backup(db="ÜRÜNLER", description="Şube yedeği")
    catalog.add_backup(rec)
    for term in ("ürün", "ÜRÜN", "şube", "ŞUBE"):
        assert [r.backup_id for r in catalog.list_backups(BackupFilter(text=term))] == [
            rec.backup_id
        ], term


def test_mark_interrupted_backups(catalog):
    running = _backup(status=m.BackupStatus.RUNNING)
    done = _backup(db="other", status=m.BackupStatus.SUCCEEDED)
    catalog.add_backup(running)
    catalog.add_backup(done)
    assert catalog.mark_interrupted_backups() == 1
    assert catalog.get_backup(running.backup_id).status is m.BackupStatus.FAILED
    assert catalog.get_backup(done.backup_id).status is m.BackupStatus.SUCCEEDED
