import pytest

from dbnero.core.backup_service import BackupRequest
from dbnero.core.compare_service import EngineMismatch, LiveSide, SnapshotMissing
from tests.unit.fakes import FakeTable


def test_schema_backup_vs_live(world):
    rec = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    svc = world.app.compares
    assert svc.schema_backup_vs_live(rec.backup_id, LiveSide(world.pg.id, "crm")).identical
    world.servers["pg1"].databases["crm"]["new_table"] = FakeTable(["id"], ["id"])
    diff = svc.schema_backup_vs_live(rec.backup_id, LiveSide(world.pg.id, "crm"))
    assert [(i.category.value, i.kind.value, i.object_name) for i in diff.items] == [
        ("table", "added", "public.new_table")
    ]


def test_schema_backup_vs_backup_and_missing_snapshot(world):
    a = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    world.servers["pg1"].databases["crm"]["customers"].columns.append("phone")
    b = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    diff = world.app.compares.schema_backup_vs_backup(a.backup_id, b.backup_id)
    assert [i.object_name for i in diff.items] == ["public.customers.phone"]
    a.schema_snapshot_path.unlink()
    with pytest.raises(SnapshotMissing):
        world.app.compares.schema_backup_vs_backup(a.backup_id, b.backup_id)


def test_engine_mismatch(world):
    rec = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    with pytest.raises(EngineMismatch):
        world.app.compares.schema_backup_vs_live(rec.backup_id, LiveSide(world.my.id, "shop"))


def test_data_backup_vs_live_uses_isolated_restore(world):
    rec = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    live = world.servers["pg1"].databases["crm"]
    live["customers"].rows[0] = (1, "changed@x")
    live["customers"].rows.append((99, "new@x"))
    del live["customers"].rows[5]
    live["logs"].rows.append(("c",))

    logs = []
    diff = world.app.compares.data_backup_vs_live(
        rec.backup_id,
        LiveSide(world.pg.id, "crm"),
        world.pg2.id,
        log_fn=lambda lvl, m: logs.append(m),
    )
    customers = next(t for t in diff.tables if t.table == "public.customers")
    assert (customers.added, customers.removed, customers.changed) == (1, 1, 1)
    logs_table = next(t for t in diff.tables if t.table == "public.logs")
    assert logs_table.identical is False and logs_table.method.value == "fingerprint"
    # temp database was created on the scratch server and dropped again
    assert world.servers["pg2"].restores and world.servers["pg2"].databases == {}
    assert any("dropped" in m for m in logs)


def test_data_backup_vs_backup(world):
    a = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    world.servers["pg1"].databases["crm"]["customers"].rows.pop()
    b = world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))
    diff = world.app.compares.data_backup_vs_backup(a.backup_id, b.backup_id, world.pg2.id)
    customers = next(t for t in diff.tables if t.table == "public.customers")
    assert customers.removed == 1 and not diff.identical
    assert world.servers["pg2"].databases == {}
