"""End-to-end service tests against real servers (batch backup, verified
restore, schema and data comparison through an isolated restore)."""

from __future__ import annotations

import uuid

import pytest

from dbnero.core.app import AppContext
from dbnero.core.compare_service import LiveSide
from dbnero.core.models import BackupStatus
from dbnero.core.settings import Settings
from dbnero.security.credentials import InMemoryCredentialStore
from dbnero.storage.catalog import Catalog
from tests.integration.conftest import seed_database


@pytest.fixture
def app(tmp_path, target):
    ctx = AppContext.create(
        Settings(archive_root=str(tmp_path / "archive"), min_free_space_mb=10),
        catalog=Catalog(tmp_path / "catalog.sqlite3"),
        credentials=InMemoryCredentialStore(),
    )
    ctx.connections.save(target.profile, target.password)
    yield ctx
    ctx.close()


@pytest.fixture
def two_dbs(target):
    names = [f"dbn_it_{uuid.uuid4().hex[:8]}" for _ in range(2)]
    for n in names:
        seed_database(target, n)
    yield names
    adapter = target.adapter()
    for n in names:
        adapter.drop_database(n)


def _schema(target):
    return {"postgresql": "public", "mysql": "", "mssql": "dbo"}[target.profile.engine.value]


def _execute(target, db, sql):
    engine = target.profile.engine.value
    p = target.profile
    if engine == "postgresql":
        import psycopg

        with psycopg.connect(
            host=p.host,
            port=p.port,
            user=p.username,
            password=target.password,
            dbname=db,
            autocommit=True,
        ) as conn:
            conn.execute(sql)
    elif engine == "mysql":
        import mysql.connector

        conn = mysql.connector.connect(
            host=p.host, port=p.port, user=p.username, password=target.password, database=db
        )
        conn.cursor().execute(sql)
        conn.commit()
        conn.close()
    else:
        import pyodbc

        conn = pyodbc.connect(target.adapter().connection_string(db), autocommit=True)
        conn.cursor().execute(sql)
        conn.close()


def test_batch_backup_restore_and_compare(app, target, two_dbs, tmp_path):
    conn_id = target.profile.id
    batch = app.backups.backup_batch(conn_id, [*two_dbs, "does_not_exist_db"], "it", ["it"])
    assert len(batch.succeeded) == 2 and len(batch.failed) == 1
    files = {i.record.file_path for i in batch.succeeded}
    assert len(files) == 2
    for item in batch.succeeded:
        assert item.record.status is BackupStatus.SUCCEEDED
        assert app.backups.validate(item.record.backup_id).ok

    rec = batch.succeeded[0].record
    db = rec.database_name
    target_db = f"dbn_it_r_{uuid.uuid4().hex[:8]}"
    try:
        outcome = app.restores.restore(rec.backup_id, conn_id, target_db)
        assert outcome.verified, outcome.verification and outcome.verification.items

        # Change the live database, then compare.
        _execute(target, db, "CREATE TABLE extra_t (id INT PRIMARY KEY)")
        _execute(target, db, "UPDATE customers SET name = 'changed' WHERE id = 5")
        _execute(target, db, "DELETE FROM orders WHERE id = 7")

        diff = app.compares.schema_backup_vs_live(rec.backup_id, LiveSide(conn_id, db))
        assert [(i.category.value, i.kind.value) for i in diff.items] == [("table", "added")]

        data = app.compares.data_backup_vs_live(rec.backup_id, LiveSide(conn_id, db))
        by_name = {t.table.split(".")[-1]: t for t in data.tables}
        assert by_name["customers"].changed == 1
        assert by_name["orders"].removed == 1
        assert not any(n.startswith("dbnero_tmp_") for n in target.adapter().list_databases())

        # Overwrite with confirmation takes a protective backup first.
        outcome = app.restores.restore(rec.backup_id, conn_id, db, overwrite=True, confirmation=db)
        assert outcome.protective_backup is not None
        assert outcome.verified
    finally:
        target.adapter().drop_database(target_db)
