import pytest

from dbnero.core.backup_service import BackupRequest
from dbnero.core.restore_service import (
    ConfirmationRequired,
    RestoreBlocked,
    Severity,
    suggest_target_name,
)


@pytest.fixture
def backup(world):
    return world.app.backups.backup_database(BackupRequest(world.pg.id, "crm"))


def test_restore_to_new_database_and_verify(world, backup):
    outcome = world.app.restores.restore(backup.backup_id, world.pg2.id, "crm_copy")
    assert outcome.verified and outcome.protective_backup is None
    assert world.servers["pg2"].databases["crm_copy"]["customers"].rows[0] == (1, "u1@x")


def test_preflight_engine_mismatch(world, backup):
    report = world.app.restores.preflight(backup.backup_id, world.my.id, "x")
    assert not report.can_proceed
    assert "Engine mismatch" in report.errors[0].message


def test_existing_target_needs_overwrite_and_confirmation(world, backup):
    svc = world.app.restores
    report = svc.preflight(backup.backup_id, world.pg.id, "crm")
    assert report.target_exists and not report.can_proceed
    with pytest.raises(RestoreBlocked):
        svc.restore(backup.backup_id, world.pg.id, "crm")
    with pytest.raises(ConfirmationRequired):
        svc.restore(backup.backup_id, world.pg.id, "crm", overwrite=True, confirmation="CRM")
    assert world.servers["pg1"].restores == []

    outcome = svc.restore(backup.backup_id, world.pg.id, "crm", overwrite=True, confirmation="crm")
    assert outcome.protective_backup is not None
    assert "pre-restore" in outcome.protective_backup.tags
    assert world.servers["pg1"].restores == ["crm"]


def test_system_database_rejected(world, backup):
    report = world.app.restores.preflight(backup.backup_id, world.pg.id, "sysdb", overwrite=True)
    assert any("system database" in c.message for c in report.errors)


def test_corrupted_file_blocks_restore(world, backup):
    with backup.file_path.open("ab") as fh:
        fh.write(b"tamper")
    report = world.app.restores.preflight(backup.backup_id, world.pg2.id, "x")
    assert any("SHA-256" in c.message for c in report.errors)


def test_unreachable_target(world, backup):
    world.servers["pg2"].unreachable = True
    report = world.app.restores.preflight(backup.backup_id, world.pg2.id, "x")
    assert any("Cannot connect" in c.message for c in report.errors)


def test_older_server_warns(world, backup):
    world.servers["pg2"].version = "15.0"
    report = world.app.restores.preflight(backup.backup_id, world.pg2.id, "x")
    assert report.can_proceed
    assert any(c.severity is Severity.WARNING and "older" in c.message for c in report.checks)


def test_suggest_name(backup):
    assert suggest_target_name(backup).startswith("crm_restored_")
