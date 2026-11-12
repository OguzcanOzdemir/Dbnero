from __future__ import annotations

import plistlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from dbnero.core.models import BackupStatus, Schedule, ScheduleFrequency, ScheduleRunStatus
from dbnero.core.os_scheduler import RUN_DUE_FLAG, TASK_NAME, OsScheduler, launch_command
from dbnero.core.schedule_service import (
    ScheduleError,
    describe,
    next_run,
    next_run_after,
    scheduler_lock,
)

UTC_TZ = UTC


def _sched(**kw) -> Schedule:
    base = {"name": "nightly", "connection_id": "c", "databases": ["crm"]}
    base.update(kw)
    return Schedule(**base)


# -- timing -------------------------------------------------------------------


def test_daily_next_run_same_day_and_next_day():
    s = _sched(time_of_day="02:30")
    at = datetime(2026, 3, 2, 1, 0, tzinfo=UTC)  # Monday
    assert next_run_after(s, at, UTC_TZ) == datetime(2026, 3, 2, 2, 30, tzinfo=UTC)
    at = datetime(2026, 3, 2, 2, 30, tzinfo=UTC)  # exactly at the time: strictly after
    assert next_run_after(s, at, UTC_TZ) == datetime(2026, 3, 3, 2, 30, tzinfo=UTC)


def test_weekday_selection_skips_days():
    s = _sched(time_of_day="22:00", weekdays=[4])  # Fridays only
    at = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)  # Monday
    assert next_run_after(s, at, UTC_TZ) == datetime(2026, 3, 6, 22, 0, tzinfo=UTC)
    assert next_run_after(_sched(weekdays=[]), at, UTC_TZ) is None


def test_hourly_interval():
    s = _sched(frequency=ScheduleFrequency.HOURLY, every_hours=6)
    at = datetime(2026, 3, 2, 1, 15, tzinfo=UTC)
    assert next_run_after(s, at, UTC_TZ) == at + timedelta(hours=6)


def test_next_run_uses_last_run_and_disabled():
    created = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    s = _sched(time_of_day="03:00", created_at_utc=created)
    assert next_run(s, UTC_TZ) == datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
    s.last_run_utc = datetime(2026, 3, 2, 3, 0, 5, tzinfo=UTC)
    assert next_run(s, UTC_TZ) == datetime(2026, 3, 3, 3, 0, tzinfo=UTC)
    s.enabled = False
    assert next_run(s, UTC_TZ) is None


def test_local_time_zone_is_used_by_default():
    s = _sched(time_of_day="02:00")
    nxt = next_run_after(s, datetime(2026, 3, 2, tzinfo=UTC))
    assert nxt is not None and nxt.tzinfo is not None
    assert nxt.astimezone().strftime("%H:%M") == "02:00"


def test_describe():
    assert describe(_sched(time_of_day="02:00")) == "Every day at 02:00"
    assert describe(_sched(weekdays=[0, 1, 2, 3, 4])) == "Weekdays at 02:00"
    assert describe(_sched(weekdays=[0, 3])) == "Mon, Thu at 02:00"
    assert describe(_sched(frequency=ScheduleFrequency.HOURLY, every_hours=1)) == "Every hour"
    assert describe(_sched(frequency=ScheduleFrequency.HOURLY, every_hours=12)) == (
        "Every 12 hours"
    )


# -- service --------------------------------------------------------------------


def _save(world, **kw) -> Schedule:
    s = _sched(connection_id=world.pg.id, **kw)
    world.app.schedules.save(s)
    return s


def test_validation(world):
    svc = world.app.schedules
    with pytest.raises(ScheduleError, match="name"):
        svc.save(_sched(name=" ", connection_id=world.pg.id))
    with pytest.raises(ScheduleError, match="at least one database"):
        svc.save(_sched(connection_id=world.pg.id, databases=[]))
    with pytest.raises(ScheduleError, match="HH:MM"):
        svc.save(_sched(connection_id=world.pg.id, time_of_day="25:00"))
    with pytest.raises(ScheduleError, match="day"):
        svc.save(_sched(connection_id=world.pg.id, weekdays=[]))
    _save(world)
    with pytest.raises(ScheduleError, match="already exists"):
        svc.save(_sched(name="NIGHTLY", connection_id=world.pg.id))


def test_save_roundtrip_keeps_run_state(world):
    s = _save(world, tags=["prod", " ", "prod"], weekdays=[2, 0])
    got = world.app.schedules.get(s.id)
    assert got.tags == ["prod"] and got.weekdays == [0, 2]
    now = datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
    assert world.app.catalog.claim_schedule_run(s.id, None, now)
    # A second claim with the stale "previous" value loses: no double run.
    assert not world.app.catalog.claim_schedule_run(s.id, None, now)
    s.description = "edited"
    world.app.schedules.save(s)  # editing never resets the run bookkeeping
    got = world.app.schedules.get(s.id)
    assert got.last_run_utc == now and got.description == "edited"


def test_run_due_backs_up_and_records(world):
    created = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    s = _save(world, databases=["crm", "erp"], created_at_utc=created, tags=["prod"])
    now = datetime(2026, 3, 2, 3, 0, tzinfo=UTC)
    assert [d.id for d in world.app.schedules.due(now, UTC_TZ)] == [s.id]
    runs = world.app.schedules.run_due(now=now, tz=UTC_TZ)
    assert len(runs) == 1 and runs[0].status is ScheduleRunStatus.SUCCEEDED
    assert len(runs[0].backup_ids) == 2
    rec = world.app.catalog.get_backup(runs[0].backup_ids[0])
    assert "scheduled" in rec.tags and "prod" in rec.tags
    assert rec.description == "Scheduled: nightly"
    got = world.app.schedules.get(s.id)
    assert got.last_status is ScheduleRunStatus.SUCCEEDED
    assert "2 databases backed up" in got.last_message
    # Ran now: not due again until tomorrow.
    assert world.app.schedules.due(now, UTC_TZ) == []


def test_all_databases_and_partial_failure(world):
    s = _save(world, databases=[], all_databases=True)
    world.servers["pg1"].fail_backup = {"erp"}
    run = world.app.schedules.run(s)
    assert run.status is ScheduleRunStatus.PARTIAL
    assert "1 backed up, 1 failed (erp)" in run.message


def test_retention_keeps_last_n_of_this_schedule_only(world):
    s = _save(world, keep_last=2)
    manual = world.app.backups.backup_batch(world.pg.id, ["crm"])
    for _ in range(4):
        world.app.schedules.run(s)
    mine = world.app.catalog.schedule_backups(s.id)
    assert len(mine) == 2
    assert all(r.status is BackupStatus.SUCCEEDED for r in mine)
    # The manual backup of the same database is never touched.
    assert world.app.catalog.get_backup(manual.items[0].record.backup_id)
    archive = world.app.backups.archive_root
    assert len(list(archive.rglob("*.fake"))) == 3


def test_interrupted_run_is_recovered(world):
    s = _save(world)
    world.app.catalog.claim_schedule_run(s.id, None, datetime.now(UTC))
    assert world.app.schedules.get(s.id).last_status is ScheduleRunStatus.RUNNING
    assert world.app.schedules.due() == []  # never started twice while "running"
    world.app.schedules.run_due()  # holding the lock means nobody else is running
    got = world.app.schedules.get(s.id)
    assert got.last_status is ScheduleRunStatus.FAILED
    assert "Interrupted" in got.last_message


def test_scheduler_lock_is_exclusive(tmp_path):
    path = tmp_path / "s.lock"
    with scheduler_lock(path) as first:
        assert first
        with scheduler_lock(path) as second:
            assert not second
    with scheduler_lock(path) as again:
        assert again


def test_headless_run_due(world, monkeypatch):
    """`dbnero --run-due` runs due schedules without a GUI."""
    from dbnero import main as main_mod

    created = datetime.now(UTC) - timedelta(days=2)
    s = _save(world, created_at_utc=created)
    monkeypatch.setattr(
        "dbnero.core.app.AppContext.create",
        lambda *a, **k: world.app,
    )
    monkeypatch.setattr(world.app, "close", lambda: None)
    assert main_mod.run_due(None) == 0
    got = world.app.schedules.get(s.id)
    assert got.last_status is ScheduleRunStatus.SUCCEEDED
    assert any(j.title.startswith("Scheduled: nightly") for j in world.app.catalog.list_jobs())


# -- OS integration ---------------------------------------------------------------


def _recorder():
    calls: list[list[str]] = []

    def run(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, "", "")

    return calls, run


def test_windows_task_scheduler_commands(tmp_path):
    calls, run = _recorder()
    osch = OsScheduler(platform="win32", home=tmp_path, run=run)
    osch.install()
    create = calls[-1]
    assert create[:2] == ["schtasks", "/Create"] and TASK_NAME in create
    assert create[create.index("/MO") + 1] == "5"
    assert RUN_DUE_FLAG in create[create.index("/TR") + 1]
    assert osch.is_installed()
    osch.uninstall()
    assert calls[-1][:2] == ["schtasks", "/Delete"]


def test_macos_launch_agent(tmp_path):
    calls, run = _recorder()
    osch = OsScheduler(platform="darwin", home=tmp_path, run=run)
    assert not osch.is_installed()
    osch.install()
    plist = tmp_path / "Library" / "LaunchAgents" / "app.dbnero.scheduler.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"][-1] == RUN_DUE_FLAG and data["StartInterval"] == 300
    assert ["launchctl", "load", "-w", str(plist)] in calls
    osch.uninstall()
    assert not plist.exists()


def test_linux_systemd_timer(tmp_path, monkeypatch):
    calls, run = _recorder()
    monkeypatch.setattr("dbnero.core.os_scheduler.shutil.which", lambda _n: "/bin/systemctl")
    osch = OsScheduler(platform="linux", home=tmp_path, run=run)
    osch.install()
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert RUN_DUE_FLAG in (unit_dir / "dbnero-scheduler.service").read_text()
    assert "OnUnitActiveSec=5min" in (unit_dir / "dbnero-scheduler.timer").read_text()
    assert ["systemctl", "--user", "enable", "--now", "dbnero-scheduler.timer"] in calls
    osch.uninstall()
    assert not (unit_dir / "dbnero-scheduler.timer").exists()


def test_failed_os_command_is_reported(tmp_path):
    def run(args):
        return subprocess.CompletedProcess(list(args), 1, "", "ERROR: Access is denied.\n")

    from dbnero.core.os_scheduler import OsSchedulerError

    with pytest.raises(OsSchedulerError, match="Access is denied"):
        OsScheduler(platform="win32", home=tmp_path, run=run).install()


def test_launch_command(monkeypatch):
    monkeypatch.delenv("APPIMAGE", raising=False)
    cmd = launch_command()
    assert cmd[-1] == RUN_DUE_FLAG and cmd[-3:-1] == ["-m", "dbnero"]
    monkeypatch.setenv("APPIMAGE", "/home/u/dbnero.AppImage")
    assert launch_command() == ["/home/u/dbnero.AppImage", RUN_DUE_FLAG]
    monkeypatch.delenv("APPIMAGE")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launch_command() == [sys.executable, RUN_DUE_FLAG]
