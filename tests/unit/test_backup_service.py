import json
import threading
from datetime import timedelta

import pytest

from dbnero.adapters.base import AdapterError, JobContext, OperationCancelled
from dbnero.core.backup_service import BackupRequest, InsufficientDiskSpace, backup_files
from dbnero.core.models import BackupMode, BackupStatus
from dbnero.storage.archive import find_stale_partials, sha256_file
from dbnero.storage.catalog import BackupFilter


def test_single_backup_writes_everything(world):
    svc = world.app.backups
    rec = svc.backup_database(
        BackupRequest(world.pg.id, "crm", "Before release", ["prod", " prod ", "x"])
    )
    assert rec.status is BackupStatus.SUCCEEDED
    assert rec.file_path.is_file() and rec.file_path.suffix == ".fake"
    assert rec.checksum_sha256 == sha256_file(rec.file_path)
    assert rec.tags == ["prod", "x"]
    assert rec.schema_snapshot_path and rec.schema_snapshot_path.is_file()
    manifest = next(rec.file_path.parent.glob("*.manifest.json"))
    data = json.loads(manifest.read_text("utf-8"))
    assert data["backup_id"] == rec.backup_id and data["checksum_sha256"] == rec.checksum_sha256
    assert "secret-pw" not in manifest.read_text("utf-8")
    stored = world.app.catalog.get_backup(rec.backup_id)
    assert stored.status is BackupStatus.SUCCEEDED and stored.file_size == rec.file_size
    assert stored.backup_mode is BackupMode.SINGLE
    rel = rec.file_path.relative_to(svc.archive_root).parts
    assert rel[:3] == ("postgresql", "pg1_5432", "crm")


def test_failed_backup_leaves_no_partial_and_is_recorded(world):
    world.servers["pg1"].fail_backup.add("crm")
    with pytest.raises(AdapterError):
        world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    (rec,) = world.app.catalog.list_backups()
    assert rec.status is BackupStatus.FAILED and "failed" in rec.error_message
    assert not rec.file_path.exists()
    assert find_stale_partials(world.app.backups.archive_root) == []
    assert not list(world.app.backups.archive_root.rglob("*.schema.json"))


def test_cancel_backup(world):
    world.servers["pg1"].slow_backup.add("crm")
    ctx = JobContext()
    threading.Timer(0.1, ctx.cancel).start()
    with pytest.raises(OperationCancelled):
        world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"), ctx)
    (rec,) = world.app.catalog.list_backups()
    assert rec.status is BackupStatus.CANCELLED
    assert find_stale_partials(world.app.backups.archive_root) == []


def test_batch_separate_files_and_isolated_failures(world):
    world.servers["pg1"].fail_backup.add("erp")
    logs = []
    result = world.app.backups.backup_batch(
        world.pg.id,
        ["crm", "erp"],
        "nightly",
        ["n"],
        log_fn=lambda lvl, m: logs.append((lvl, m)),
        max_parallel=2,
    )
    assert [i.database for i in result.succeeded] == ["crm"]
    assert [i.database for i in result.failed] == ["erp"]
    recs = world.app.catalog.list_backups(BackupFilter(batch_id=result.batch.batch_id))
    assert {r.database_name: r.status for r in recs} == {
        "crm": BackupStatus.SUCCEEDED,
        "erp": BackupStatus.FAILED,
    }
    assert all(r.backup_mode is BackupMode.BATCH for r in recs)
    assert any("1 succeeded, 1 failed" in m for _, m in logs)


def test_batch_two_successes_two_files(world):
    result = world.app.backups.backup_batch(world.pg.id, ["crm", "erp"], max_parallel=1)
    files = {i.record.file_path for i in result.succeeded}
    assert len(files) == 2 and all(f.is_file() for f in files)


def test_insufficient_disk_space(world):
    world.app.settings.min_free_space_mb = 10**12
    with pytest.raises(InsufficientDiskSpace):
        world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    assert world.app.catalog.list_backups()[0].status is BackupStatus.FAILED


def test_validate_detects_tampering(world):
    svc = world.app.backups
    rec = svc.backup_database(BackupRequest(world.pg.id, "crm"))
    assert svc.validate(rec.backup_id).ok
    with rec.file_path.open("ab") as fh:
        fh.write(b"x")
    result = svc.validate(rec.backup_id)
    assert not result.ok
    assert world.app.catalog.get_backup(rec.backup_id).status is BackupStatus.UNVERIFIED
    rec.file_path.unlink()
    assert "not found" in svc.validate(rec.backup_id).errors[0]


def test_delete_backup_removes_files(world):
    svc = world.app.backups
    rec = svc.backup_database(BackupRequest(world.pg.id, "crm"))
    files = [p for p in backup_files(rec) if p.exists()]
    assert len(files) == 3  # backup, manifest, schema
    svc.delete_backup(rec.backup_id)
    assert not any(p.exists() for p in files)
    assert world.app.catalog.list_backups() == []
    assert not (svc.archive_root / "postgresql").exists()


def test_retention_keeps_newest_successful(world):
    svc = world.app.backups
    recs = [svc.backup_database(BackupRequest(world.pg.id, "crm")) for _ in range(3)]
    world.app.settings.retention_keep_last = 1
    doomed = {r.backup_id for r in svc.retention_plan()}
    assert doomed == {recs[0].backup_id, recs[1].backup_id}
    world.app.settings.retention_keep_last = 0
    world.app.settings.retention_max_age_days = 1
    future = recs[-1].created_at_utc + timedelta(days=30)
    plan = {r.backup_id for r in svc.retention_plan(now=future)}
    assert recs[-1].backup_id not in plan and len(plan) == 2
    assert svc.apply_retention(list(plan)) == 2
    assert len(world.app.catalog.list_backups()) == 1


def test_backup_defaults_from_settings(world, monkeypatch):
    seen = {}
    from tests.unit.fakes import FakeAdapter

    original = FakeAdapter.backup

    def spy(self, database, destination, options, ctx=None):
        seen.update(options)
        return original(self, database, destination, options, ctx)

    monkeypatch.setattr(FakeAdapter, "backup", spy)
    world.app.settings.backup_defaults = {"postgresql": {"compress_level": 9}}
    world.app.backups.backup_database(BackupRequest(world.pg.id, "crm", options={"x": 1}))
    assert seen == {"compress_level": 9, "x": 1}


def test_update_metadata_rewrites_manifest(world):
    from dbnero.core.backup_service import manifest_path

    svc = world.app.backups
    rec = svc.backup_database(BackupRequest(world.pg.id, "crm", "old"))
    updated = svc.update_metadata(rec.backup_id, " new ", ["a", "a", "b"])
    assert updated.description == "new" and updated.tags == ["a", "b"]
    assert updated.created_at_utc == rec.created_at_utc
    data = json.loads(manifest_path(rec).read_text("utf-8"))
    assert data["description"] == "new" and data["tags"] == ["a", "b"]


def test_retention_ignores_failed_entries_for_keep_last(world):
    svc = world.app.backups
    for _ in range(2):
        svc.backup_database(BackupRequest(world.pg.id, "crm"))
    world.servers["pg1"].fail_backup.add("crm")
    for _ in range(3):
        with pytest.raises(AdapterError):
            svc.backup_database(BackupRequest(world.pg.id, "crm"))
    world.app.settings.retention_keep_last = 2
    # Three newer failures must not push the two good backups out.
    assert svc.retention_plan() == []


def test_retention_keeps_last_unverified_copy(world):
    svc = world.app.backups
    recs = [svc.backup_database(BackupRequest(world.pg.id, "crm")) for _ in range(2)]
    for r in recs:
        world.app.catalog.update_backup(r.backup_id, status=BackupStatus.UNVERIFIED)
    world.app.settings.retention_max_age_days = 1
    future = recs[-1].created_at_utc + timedelta(days=30)
    plan = {r.backup_id for r in svc.retention_plan(now=future)}
    assert plan == {recs[0].backup_id}


def test_failed_entries_removed_by_age_only(world):
    svc = world.app.backups
    ok = svc.backup_database(BackupRequest(world.pg.id, "crm"))
    world.servers["pg1"].fail_backup.add("crm")
    with pytest.raises(AdapterError):
        svc.backup_database(BackupRequest(world.pg.id, "crm"))
    failed = next(r for r in world.app.catalog.list_backups() if r.status is BackupStatus.FAILED)
    world.app.settings.retention_keep_last = 1
    assert svc.retention_plan() == []
    world.app.settings.retention_max_age_days = 1
    future = failed.created_at_utc + timedelta(days=5)
    assert [r.backup_id for r in svc.retention_plan(now=future)] == [failed.backup_id]
    assert ok.status is BackupStatus.SUCCEEDED
