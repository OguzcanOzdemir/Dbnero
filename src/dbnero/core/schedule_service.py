"""Scheduled backups: when a schedule is due, running it and its retention.

Two runners share this service:

* the desktop app checks every minute while it is open;
* a small OS task (``dbnero --run-due``, see ``os_scheduler``) runs it when
  the app is closed.

A run is claimed atomically in the catalog (``claim_schedule_run``), and the
whole check runs under an inter-process lock (``scheduler_lock``), so a
schedule never runs twice even when both runners wake up at once.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import IO, Any

from dbnero.adapters.base import JobContext, OperationCancelled
from dbnero.core.backup_service import BackupService
from dbnero.core.connection_service import ConnectionService
from dbnero.core.logging_setup import mask_secrets
from dbnero.core.models import (
    BackupStatus,
    Notification,
    NotificationLevel,
    Schedule,
    ScheduleFrequency,
    ScheduleRunStatus,
    utc_now,
)
from dbnero.storage.catalog import Catalog

log = logging.getLogger(__name__)

LogFn = Callable[[str, str], None]

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
# A run that starts later than this after its time counts as missed: the OS
# task starts every few minutes, so only a closed app or computer is this late.
MISSED_GRACE = timedelta(minutes=15)
SCHEDULED_TAG = "scheduled"


class ScheduleError(ValueError):
    pass


class SchedulerBusy(ScheduleError):
    pass


def parse_time(value: str) -> tuple[int, int]:
    try:
        hour_s, minute_s = value.strip().split(":")
        hour, minute = int(hour_s), int(minute_s)
    except ValueError:
        raise ScheduleError(f"Invalid time '{value}', use HH:MM") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ScheduleError(f"Invalid time '{value}', use HH:MM")
    return hour, minute


def _local(year: int, month: int, day: int, hour: int, minute: int, tz: tzinfo | None) -> datetime:
    if tz is not None:
        return datetime(year, month, day, hour, minute, tzinfo=tz)
    # A naive time read as the system's local time; handles daylight saving.
    return datetime(year, month, day, hour, minute).astimezone()


def next_run_after(sched: Schedule, after: datetime, tz: tzinfo | None = None) -> datetime | None:
    """The first scheduled time strictly after ``after`` (UTC), or None if the
    schedule can never run. ``tz`` defaults to the system time zone."""
    if sched.frequency is ScheduleFrequency.HOURLY:
        hours = max(1, sched.every_hours)
        return after + timedelta(hours=hours)
    days = {d for d in sched.weekdays if 0 <= d <= 6}
    if not days:
        return None
    hour, minute = parse_time(sched.time_of_day)
    start = after.astimezone(tz) if tz is not None else after.astimezone()
    for offset in range(8):
        day = start.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        candidate = _local(day.year, day.month, day.day, hour, minute, tz)
        if candidate > after:
            return candidate.astimezone(UTC)
    return None


def next_run(sched: Schedule, tz: tzinfo | None = None) -> datetime | None:
    if not sched.enabled:
        return None
    base = sched.last_run_utc or sched.created_at_utc
    return next_run_after(sched, base, tz)


@dataclass(slots=True)
class MissedRuns:
    count: int
    first_due: datetime
    last_due: datetime

    def text(self) -> str:
        first = fmt_when(self.first_due)
        if self.count == 1:
            return f"It was due {first} and did not run because dbnero or the computer was off."
        return (
            f"{self.count} runs did not happen because dbnero or the computer was off "
            f"(the first was due {first})."
        )


def fmt_when(dt: datetime) -> str:
    """Local time for messages, e.g. "Thu 12 Nov 02:00"."""
    return dt.astimezone().strftime("%a %d %b %H:%M")


def missed_runs(
    sched: Schedule, now: datetime, tz: tzinfo | None = None, grace: timedelta = MISSED_GRACE
) -> MissedRuns | None:
    """Scheduled times that passed without a run, if the oldest is overdue by
    more than ``grace``."""
    first = next_run(sched, tz)
    if first is None or first > now - grace:
        return None
    count, last = 0, first
    when: datetime | None = first
    while when is not None and when <= now and count < 1000:
        count, last = count + 1, when
        when = next_run_after(sched, when, tz)
    return MissedRuns(count, first, last)


def describe(sched: Schedule) -> str:
    """Short human text, e.g. "Every day at 02:00" or "Every 6 hours"."""
    if sched.frequency is ScheduleFrequency.HOURLY:
        hours = max(1, sched.every_hours)
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    days = sorted({d for d in sched.weekdays if 0 <= d <= 6})
    if len(days) == 7:
        when = "Every day"
    elif days == [0, 1, 2, 3, 4]:
        when = "Weekdays"
    else:
        when = ", ".join(WEEKDAY_NAMES[d] for d in days) or "Never"
    return f"{when} at {sched.time_of_day}"


@dataclass(slots=True)
class ScheduleRun:
    schedule: Schedule
    status: ScheduleRunStatus
    message: str
    backup_ids: list[str] = field(default_factory=list)
    deleted: int = 0


class ScheduleService:
    def __init__(
        self, catalog: Catalog, connections: ConnectionService, backups: BackupService
    ) -> None:
        self.catalog = catalog
        self.connections = connections
        self.backups = backups

    # -- editing ------------------------------------------------------------

    def list_all(self) -> list[Schedule]:
        return self.catalog.list_schedules()

    def get(self, schedule_id: str) -> Schedule:
        return self.catalog.get_schedule(schedule_id)

    def validate(self, sched: Schedule) -> None:
        if not sched.name.strip():
            raise ScheduleError("Give the schedule a name")
        self.connections.get(sched.connection_id)  # raises when the connection is gone
        if not sched.all_databases and not sched.databases:
            raise ScheduleError("Pick at least one database, or choose all databases")
        if sched.frequency is ScheduleFrequency.DAILY:
            parse_time(sched.time_of_day)
            if not sched.weekdays:
                raise ScheduleError("Pick at least one day")
        elif not 1 <= sched.every_hours <= 168:
            raise ScheduleError("Repeat every 1 to 168 hours")
        if sched.keep_last < 0:
            raise ScheduleError("Keep last cannot be negative")
        if any(
            s.name.casefold() == sched.name.strip().casefold() and s.id != sched.id
            for s in self.list_all()
        ):
            raise ScheduleError(f"A schedule named '{sched.name.strip()}' already exists")

    def save(self, sched: Schedule) -> None:
        sched.name = sched.name.strip()
        sched.tags = list(dict.fromkeys(t.strip() for t in sched.tags if t.strip()))
        self.validate(sched)
        self.catalog.save_schedule(sched)

    def delete(self, schedule_id: str) -> None:
        """Remove the schedule. Its backups stay in the archive."""
        self.catalog.delete_schedule(schedule_id)

    # -- running --------------------------------------------------------------

    def due(self, now: datetime | None = None, tz: tzinfo | None = None) -> list[Schedule]:
        now = now or utc_now()
        out = []
        for sched in self.list_all():
            if sched.last_status is ScheduleRunStatus.RUNNING:
                continue
            when = next_run(sched, tz)
            if when is not None and when <= now:
                out.append(sched)
        return out

    def run_due(
        self,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
        now: datetime | None = None,
        tz: tzinfo | None = None,
    ) -> list[ScheduleRun]:
        """Run every due schedule once, one after another. Missed runs (the
        computer was off) are caught up once, not repeated.

        Call this only while holding ``scheduler_lock``: every schedule run
        happens under it, so a run still marked "running" now is a leftover
        of a closed or crashed process."""
        self.catalog.mark_interrupted_schedules()
        now = now or utc_now()
        runs = []
        for sched in self.due(now, tz):
            if ctx and ctx.cancelled:
                break
            missed = missed_runs(sched, now, tz)
            if not self.catalog.claim_schedule_run(sched.id, sched.last_run_utc, utc_now()):
                continue  # another runner got it first
            note = None
            if missed is not None:
                # Tell the user right away; the result is added when the run ends.
                note = Notification(
                    title=f"Missed backup: {sched.name}",
                    message=f"{missed.text()} Backing it up now.",
                    level=NotificationLevel.WARNING,
                    schedule_id=sched.id,
                )
                self.catalog.add_notification(note)
            run = self.run(sched, ctx, log_fn, claimed=True, notify_failure=note is None)
            if note is not None:
                ok = run.status is ScheduleRunStatus.SUCCEEDED
                self.catalog.update_notification(
                    note.id,
                    level=NotificationLevel.WARNING if ok else NotificationLevel.DANGER,
                    message=f"{missed.text() if missed else ''} Caught up "
                    f"{fmt_when(utc_now())}: {run.message}.".strip(),
                )
            runs.append(run)
        return runs

    def is_busy(self, lock: Path) -> bool:
        """True while another process (or job) is running scheduled backups."""
        with scheduler_lock(lock) as acquired:
            return not acquired

    def run_locked(
        self,
        lock: Path,
        schedule_id: str | None = None,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
    ) -> list[ScheduleRun]:
        """Run one schedule now (``schedule_id``) or every due one, holding the
        inter-process lock for the whole run."""
        with scheduler_lock(lock) as acquired:
            if not acquired:
                raise SchedulerBusy(
                    "Scheduled backups are already running (in the background or another "
                    "dbnero window). Try again in a minute."
                )
            if schedule_id is None:
                return self.run_due(ctx, log_fn)
            self.catalog.mark_interrupted_schedules()
            # "Run now": the result is on screen, no notification needed.
            return [self.run(self.get(schedule_id), ctx, log_fn, notify_failure=False)]

    def run(
        self,
        sched: Schedule,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
        claimed: bool = False,
        notify_failure: bool = True,
    ) -> ScheduleRun:
        """Back up the schedule's databases now and apply its retention. A
        failed or partly failed run leaves a notification for the user."""
        emit = log_fn or (lambda level, msg: log.log(logging.getLevelName(level), msg))
        if not claimed:
            self.catalog.claim_schedule_run(sched.id, sched.last_run_utc, utc_now())
        emit("INFO", f"Schedule “{sched.name}”: {describe(sched)}")
        try:
            run = self._run(sched, ctx, emit)
        except OperationCancelled:
            run = ScheduleRun(sched, ScheduleRunStatus.FAILED, "Cancelled")
        except Exception as exc:
            message = mask_secrets(str(exc)) or type(exc).__name__
            emit("ERROR", f"Schedule “{sched.name}” failed: {message}")
            run = ScheduleRun(sched, ScheduleRunStatus.FAILED, message)
        self.catalog.record_schedule_result(sched.id, run.status.value, run.message)
        failed = run.status in {ScheduleRunStatus.FAILED, ScheduleRunStatus.PARTIAL}
        if notify_failure and failed and run.message != "Cancelled":
            partly = run.status is ScheduleRunStatus.PARTIAL
            self.catalog.add_notification(
                Notification(
                    title=f"Scheduled backup {'partly ' if partly else ''}failed: {sched.name}",
                    message=run.message,
                    level=NotificationLevel.DANGER,
                    schedule_id=sched.id,
                )
            )
        return run

    def _run(self, sched: Schedule, ctx: JobContext | None, emit: LogFn) -> ScheduleRun:
        if sched.all_databases:
            databases = self.connections.list_databases(sched.connection_id)
            if not databases:
                raise ScheduleError("The server has no databases to back up")
        else:
            databases = list(sched.databases)
        tags = list(dict.fromkeys([*sched.tags, SCHEDULED_TAG]))
        description = sched.description or f"Scheduled: {sched.name}"
        result = self.backups.backup_batch(
            sched.connection_id,
            databases,
            description=description,
            tags=tags,
            ctx=ctx,
            log_fn=emit,
            schedule_id=sched.id,
        )
        ok, failed = len(result.succeeded), len(result.failed)
        if failed == 0:
            status = ScheduleRunStatus.SUCCEEDED
            message = f"{ok} database{'s' if ok != 1 else ''} backed up"
        elif ok == 0:
            status = ScheduleRunStatus.FAILED
            errors = "; ".join(f"{i.database}: {i.error}" for i in result.failed if i.error)
            message = (
                f"Backup failed. {errors}" if failed == 1 else f"All {failed} failed. {errors}"
            ).strip()
        else:
            status = ScheduleRunStatus.PARTIAL
            names = ", ".join(i.database for i in result.failed)
            message = f"{ok} backed up, {failed} failed ({names})"
        deleted = self.apply_retention(sched, emit) if ok else 0
        if deleted:
            message += f"; {deleted} old backup{'s' if deleted != 1 else ''} removed"
        return ScheduleRun(
            sched,
            status,
            message[:500],
            [i.record.backup_id for i in result.succeeded if i.record],
            deleted,
        )

    def retention_plan(self, sched: Schedule) -> list[str]:
        """Backups of this schedule beyond "keep last N", per database. Only
        usable backups count; failed ones of this schedule are removed too once
        a newer usable backup of the same database exists."""
        if sched.keep_last <= 0:
            return []
        usable = {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}
        groups: dict[tuple[str, str], list[Any]] = {}
        for rec in self.catalog.schedule_backups(sched.id):
            groups.setdefault((rec.server_label, rec.database_name), []).append(rec)
        doomed: list[str] = []
        for recs in groups.values():  # newest first
            good = [r for r in recs if r.status in usable]
            doomed.extend(r.backup_id for r in good[sched.keep_last :])
            if good:
                newest_good = good[0].created_at_utc
                doomed.extend(
                    r.backup_id
                    for r in recs
                    if r.status in {BackupStatus.FAILED, BackupStatus.CANCELLED}
                    and r.created_at_utc < newest_good
                )
        return doomed

    def apply_retention(self, sched: Schedule, emit: LogFn) -> int:
        count = 0
        for backup_id in self.retention_plan(sched):
            try:
                self.backups.delete_backup(backup_id)
                count += 1
            except Exception as exc:
                emit("WARNING", f"Could not remove an old backup: {mask_secrets(str(exc))}")
        if count:
            plural = "s" if count != 1 else ""
            emit("INFO", f"Removed {count} old backup{plural} (keep last {sched.keep_last})")
        return count


# -- inter-process lock -------------------------------------------------------


@contextlib.contextmanager
def scheduler_lock(path: Path) -> Iterator[bool]:
    """Non-blocking exclusive lock shared by every dbnero process. Yields True
    when acquired; False means another process is running schedules now."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh: IO[bytes] = open(path, "a+b")  # noqa: SIM115 - closed below
    acquired = False
    try:
        try:
            _lock_file(fh)
            acquired = True
        except OSError:
            acquired = False
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                _unlock_file(fh)
        fh.close()


def _lock_file(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
