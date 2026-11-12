"""SQLite catalog of connection profiles, backups, batches and jobs.

Uses SQLAlchemy Core. The physical schema is created by ``migrations.py``;
the ``Table`` objects below only describe it for query building and must be
kept in sync with the latest migration (enforced by unit tests).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    Connection,
    Dialect,
    Engine,
    Integer,
    MetaData,
    Row,
    String,
    Table,
    TypeDecorator,
    create_engine,
    delete,
    event,
    exists,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.sql.elements import ColumnElement

from dbnero.core import models as m
from dbnero.storage.migrations import migrate


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware datetimes as ISO-8601 UTC text."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed in the catalog")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value).astimezone(UTC)


class JSONText(TypeDecorator[Any]):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)

    def process_result_value(self, value: str | None, dialect: Dialect) -> Any:
        return None if value is None else json.loads(value)


metadata = MetaData()

connections_t = Table(
    "connections",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("engine", String, nullable=False),
    Column("host", String, nullable=False),
    Column("port", Integer, nullable=False),
    Column("username", String, nullable=False),
    Column("auth_type", String, nullable=False),
    Column("ssl_mode", String, nullable=False),
    Column("default_database", String, nullable=False),
    Column("options", JSONText, nullable=False),
    Column("created_at_utc", UTCDateTime, nullable=False),
    Column("updated_at_utc", UTCDateTime, nullable=False),
)

batches_t = Table(
    "batches",
    metadata,
    Column("batch_id", String, primary_key=True),
    Column("connection_id", String, nullable=False),
    Column("description", String, nullable=False),
    Column("database_names", JSONText, nullable=False),
    Column("created_at_utc", UTCDateTime, nullable=False),
    Column("schedule_id", String),
)

notifications_t = Table(
    "notifications",
    metadata,
    Column("id", String, primary_key=True),
    Column("created_at_utc", UTCDateTime, nullable=False),
    Column("level", String, nullable=False),
    Column("title", String, nullable=False),
    Column("message", String, nullable=False),
    Column("schedule_id", String),
    Column("read_at_utc", UTCDateTime),
)

schedules_t = Table(
    "schedules",
    metadata,
    Column("id", String, primary_key=True),
    Column("name", String, nullable=False),
    Column("connection_id", String, nullable=False),
    Column("databases", JSONText, nullable=False),
    Column("all_databases", Integer, nullable=False),
    Column("frequency", String, nullable=False),
    Column("time_of_day", String, nullable=False),
    Column("weekdays", JSONText, nullable=False),
    Column("every_hours", Integer, nullable=False),
    Column("keep_last", Integer, nullable=False),
    Column("description", String, nullable=False),
    Column("tags", JSONText, nullable=False),
    Column("enabled", Integer, nullable=False),
    Column("created_at_utc", UTCDateTime, nullable=False),
    Column("updated_at_utc", UTCDateTime, nullable=False),
    Column("last_run_utc", UTCDateTime),
    Column("last_status", String, nullable=False),
    Column("last_message", String, nullable=False),
)

backups_t = Table(
    "backups",
    metadata,
    Column("backup_id", String, primary_key=True),
    Column("batch_id", String),
    Column("connection_id", String, nullable=False),
    Column("engine", String, nullable=False),
    Column("engine_version", String, nullable=False),
    Column("server_label", String, nullable=False),
    Column("database_name", String, nullable=False),
    Column("created_at_utc", UTCDateTime, nullable=False),
    Column("reference_date", UTCDateTime),
    Column("description", String, nullable=False),
    Column("tags", JSONText, nullable=False),
    Column("backup_format", String, nullable=False),
    Column("backup_mode", String, nullable=False),
    Column("file_path", String, nullable=False),
    Column("file_size", Integer),
    Column("checksum_sha256", String),
    Column("status", String, nullable=False),
    Column("error_message", String),
    Column("tool_versions", JSONText, nullable=False),
    Column("schema_snapshot_path", String),
    Column("comparison_snapshot_path", String),
    Column("extra", JSONText, nullable=False),
)

jobs_t = Table(
    "jobs",
    metadata,
    Column("job_id", String, primary_key=True),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False),
    Column("title", String, nullable=False),
    Column("connection_id", String),
    Column("backup_id", String),
    Column("batch_id", String),
    Column("started_at_utc", UTCDateTime, nullable=False),
    Column("finished_at_utc", UTCDateTime),
    Column("error_message", String),
)

job_logs_t = Table(
    "job_logs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String, nullable=False),
    Column("logged_at_utc", UTCDateTime, nullable=False),
    Column("level", String, nullable=False),
    Column("message", String, nullable=False),
)


@dataclass(slots=True)
class BackupFilter:
    text: str | None = None  # matches database name or description
    connection_id: str | None = None
    engine: m.Engine | None = None
    database_name: str | None = None
    tag: str | None = None
    status: m.BackupStatus | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    batch_id: str | None = None
    limit: int | None = None


@dataclass(slots=True)
class JobRecord:
    job_id: str
    kind: m.JobKind
    status: m.JobStatus
    title: str
    started_at_utc: datetime
    connection_id: str | None = None
    backup_id: str | None = None
    batch_id: str | None = None
    finished_at_utc: datetime | None = None
    error_message: str | None = None


# Fields of a backup that may change after creation. created_at_utc, ids,
# engine and source are deliberately excluded.
_MUTABLE_BACKUP_FIELDS = frozenset(
    {
        "description",
        "tags",
        "reference_date",
        "file_path",
        "file_size",
        "checksum_sha256",
        "status",
        "error_message",
        "engine_version",
        "tool_versions",
        "backup_format",
        "schema_snapshot_path",
        "comparison_snapshot_path",
        "extra",
    }
)


class NotFound(LookupError):
    pass


def _make_engine(path: Path | str) -> Engine:
    url = "sqlite://" if str(path) == ":memory:" else f"sqlite:///{path}"
    engine = create_engine(url)

    # Let SQLAlchemy own transactions so DDL in migrations is atomic.
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        # SQLite's lower() only folds ASCII; use Python's Unicode case folding.
        dbapi_conn.create_function("ufold", 1, _casefold, deterministic=True)
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


class Catalog:
    def __init__(self, path: Path | str) -> None:
        self.path: Path | None = None if str(path) == ":memory:" else Path(path)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = _make_engine(path)
        with self._engine.begin() as conn:
            migrate(conn)

    def close(self) -> None:
        self._engine.dispose()

    @contextmanager
    def _tx(self) -> Iterator[Connection]:
        with self._engine.begin() as conn:
            yield conn

    # -- connections ------------------------------------------------------

    def save_connection(self, profile: m.ConnectionProfile) -> None:
        profile.updated_at_utc = m.utc_now()
        values = {
            "name": profile.name,
            "engine": profile.engine.value,
            "host": profile.host,
            "port": profile.port,
            "username": profile.username,
            "auth_type": profile.auth_type.value,
            "ssl_mode": profile.ssl_mode.value,
            "default_database": profile.default_database,
            "options": profile.options,
            "updated_at_utc": profile.updated_at_utc,
        }
        with self._tx() as conn:
            found = conn.execute(
                select(connections_t.c.id).where(connections_t.c.id == profile.id)
            ).first()
            if found:
                conn.execute(
                    update(connections_t).where(connections_t.c.id == profile.id).values(**values)
                )
            else:
                conn.execute(
                    insert(connections_t).values(
                        id=profile.id, created_at_utc=profile.created_at_utc, **values
                    )
                )

    def get_connection(self, connection_id: str) -> m.ConnectionProfile:
        with self._tx() as conn:
            row = conn.execute(
                select(connections_t).where(connections_t.c.id == connection_id)
            ).first()
        if row is None:
            raise NotFound(f"Connection {connection_id} not found")
        return _row_to_connection(row)

    def list_connections(self) -> list[m.ConnectionProfile]:
        with self._tx() as conn:
            rows = conn.execute(
                select(connections_t).order_by(func.lower(connections_t.c.name))
            ).all()
        return [_row_to_connection(r) for r in rows]

    def delete_connection(self, connection_id: str) -> None:
        with self._tx() as conn:
            conn.execute(delete(connections_t).where(connections_t.c.id == connection_id))

    # -- batches ----------------------------------------------------------

    def ensure_batch(
        self, batch_id: str, connection_id: str, created_at_utc: datetime, database_name: str
    ) -> None:
        """Create a batch row if missing (used when importing manifests)."""
        with self._tx() as conn:
            row = conn.execute(
                select(batches_t.c.database_names).where(batches_t.c.batch_id == batch_id)
            ).first()
            if row is None:
                conn.execute(
                    insert(batches_t).values(
                        batch_id=batch_id,
                        connection_id=connection_id,
                        description="",
                        database_names=[database_name],
                        created_at_utc=created_at_utc,
                    )
                )
            elif database_name not in (row.database_names or []):
                conn.execute(
                    update(batches_t)
                    .where(batches_t.c.batch_id == batch_id)
                    .values(database_names=[*(row.database_names or []), database_name])
                )

    def mark_interrupted_backups(self) -> int:
        """Backups left RUNNING/PENDING by a previous session become FAILED."""
        with self._tx() as conn:
            result = conn.execute(
                update(backups_t)
                .where(
                    backups_t.c.status.in_(
                        [m.BackupStatus.RUNNING.value, m.BackupStatus.PENDING.value]
                    )
                )
                .values(
                    status=m.BackupStatus.FAILED.value,
                    error_message="Interrupted when the application closed",
                )
            )
        return int(result.rowcount or 0)

    def add_batch(self, batch: m.BackupBatch) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(batches_t).values(
                    batch_id=batch.batch_id,
                    connection_id=batch.connection_id,
                    description=batch.description,
                    database_names=batch.database_names,
                    created_at_utc=batch.created_at_utc,
                    schedule_id=batch.schedule_id,
                )
            )

    # -- schedules ----------------------------------------------------------

    def save_schedule(self, sched: m.Schedule) -> None:
        sched.updated_at_utc = m.utc_now()
        values = _schedule_to_values(sched)
        with self._tx() as conn:
            found = conn.execute(
                select(schedules_t.c.id).where(schedules_t.c.id == sched.id)
            ).first()
            if found is None:
                conn.execute(insert(schedules_t).values(**values))
            else:
                # Run bookkeeping is owned by the runner (claim/record), never the editor.
                for key in ("created_at_utc", "last_run_utc", "last_status", "last_message"):
                    values.pop(key)
                conn.execute(update(schedules_t).where(schedules_t.c.id == sched.id).values(values))

    def get_schedule(self, schedule_id: str) -> m.Schedule:
        with self._tx() as conn:
            row = conn.execute(select(schedules_t).where(schedules_t.c.id == schedule_id)).first()
        if row is None:
            raise NotFound(f"Schedule {schedule_id} not found")
        return _row_to_schedule(row)

    def list_schedules(self) -> list[m.Schedule]:
        with self._tx() as conn:
            rows = conn.execute(select(schedules_t).order_by(ufold_name(schedules_t))).all()
        return [_row_to_schedule(r) for r in rows]

    def delete_schedule(self, schedule_id: str) -> None:
        with self._tx() as conn:
            conn.execute(delete(schedules_t).where(schedules_t.c.id == schedule_id))

    def claim_schedule_run(
        self, schedule_id: str, previous: datetime | None, now: datetime
    ) -> bool:
        """Atomically mark a run as started. False when another process (or an
        earlier tick) already claimed it, so a run never happens twice."""
        c = schedules_t.c
        prev_cond = c.last_run_utc.is_(None) if previous is None else c.last_run_utc == previous
        with self._tx() as conn:
            result = conn.execute(
                update(schedules_t)
                .where(c.id == schedule_id, prev_cond)
                .values(last_run_utc=now, last_status="running", last_message="")
            )
        return bool(result.rowcount)

    def mark_interrupted_schedules(self) -> int:
        """Schedule runs left "running" by a closed or crashed process become failed."""
        with self._tx() as conn:
            result = conn.execute(
                update(schedules_t)
                .where(schedules_t.c.last_status == m.ScheduleRunStatus.RUNNING.value)
                .values(
                    last_status=m.ScheduleRunStatus.FAILED.value,
                    last_message="Interrupted before it finished",
                )
            )
        return int(result.rowcount or 0)

    def record_schedule_result(self, schedule_id: str, status: str, message: str) -> None:
        with self._tx() as conn:
            conn.execute(
                update(schedules_t)
                .where(schedules_t.c.id == schedule_id)
                .values(last_status=status, last_message=message)
            )

    def schedule_backups(self, schedule_id: str) -> list[m.BackupRecord]:
        """Backups made by a schedule, newest first."""
        batch_ids = select(batches_t.c.batch_id).where(batches_t.c.schedule_id == schedule_id)
        query = (
            select(backups_t)
            .where(backups_t.c.batch_id.in_(batch_ids))
            .order_by(backups_t.c.created_at_utc.desc())
        )
        with self._tx() as conn:
            rows = conn.execute(query).all()
        return [_row_to_backup(r) for r in rows]

    # -- notifications ------------------------------------------------------

    def add_notification(self, note: m.Notification) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(notifications_t).values(
                    id=note.id,
                    created_at_utc=note.created_at_utc,
                    level=note.level.value,
                    title=note.title,
                    message=note.message,
                    schedule_id=note.schedule_id,
                    read_at_utc=note.read_at_utc,
                )
            )

    def update_notification(
        self, note_id: str, *, level: m.NotificationLevel | None = None, message: str | None = None
    ) -> None:
        values: dict[str, Any] = {}
        if level is not None:
            values["level"] = level.value
        if message is not None:
            values["message"] = message
        if values:
            with self._tx() as conn:
                conn.execute(
                    update(notifications_t).where(notifications_t.c.id == note_id).values(values)
                )

    def list_notifications(self, unread_only: bool = True, limit: int = 50) -> list[m.Notification]:
        c = notifications_t.c
        query = select(notifications_t).order_by(c.created_at_utc.desc()).limit(limit)
        if unread_only:
            query = query.where(c.read_at_utc.is_(None))
        with self._tx() as conn:
            rows = conn.execute(query).all()
        return [
            m.Notification(
                id=r.id,
                created_at_utc=r.created_at_utc,
                level=m.NotificationLevel(r.level),
                title=r.title,
                message=r.message,
                schedule_id=r.schedule_id,
                read_at_utc=r.read_at_utc,
            )
            for r in rows
        ]

    def mark_notifications_read(self, ids: list[str] | None = None) -> None:
        """Mark the given notifications (or all unread ones) as read."""
        c = notifications_t.c
        query = update(notifications_t).where(c.read_at_utc.is_(None))
        if ids is not None:
            query = query.where(c.id.in_(ids))
        with self._tx() as conn:
            conn.execute(query.values(read_at_utc=m.utc_now()))

    # -- backups ----------------------------------------------------------

    def add_backup(self, record: m.BackupRecord) -> None:
        with self._tx() as conn:
            conn.execute(insert(backups_t).values(**_backup_to_values(record)))

    def update_backup(self, backup_id: str, **changes: Any) -> None:
        illegal = set(changes) - _MUTABLE_BACKUP_FIELDS
        if illegal:
            raise ValueError(f"Backup fields cannot be changed: {sorted(illegal)}")
        values = {k: _to_db_value(v) for k, v in changes.items()}
        with self._tx() as conn:
            result = conn.execute(
                update(backups_t).where(backups_t.c.backup_id == backup_id).values(**values)
            )
            if result.rowcount == 0:
                raise NotFound(f"Backup {backup_id} not found")

    def get_backup(self, backup_id: str) -> m.BackupRecord:
        with self._tx() as conn:
            row = conn.execute(select(backups_t).where(backups_t.c.backup_id == backup_id)).first()
        if row is None:
            raise NotFound(f"Backup {backup_id} not found")
        return _row_to_backup(row)

    def list_backups(self, flt: BackupFilter | None = None) -> list[m.BackupRecord]:
        flt = flt or BackupFilter()
        conds: list[ColumnElement[bool]] = []
        c = backups_t.c
        if flt.connection_id:
            conds.append(c.connection_id == flt.connection_id)
        if flt.engine:
            conds.append(c.engine == flt.engine.value)
        if flt.database_name:
            conds.append(c.database_name == flt.database_name)
        if flt.status:
            conds.append(c.status == flt.status.value)
        if flt.batch_id:
            conds.append(c.batch_id == flt.batch_id)
        if flt.created_from:
            conds.append(c.created_at_utc >= flt.created_from)
        if flt.created_to:
            conds.append(c.created_at_utc < flt.created_to)
        if flt.text:
            pattern = "%" + _escape_like(flt.text.casefold()) + "%"
            conds.append(
                func.ufold(c.database_name).like(pattern, escape="\\")
                | func.ufold(c.description).like(pattern, escape="\\")
            )
        if flt.tag:
            tags = func.json_each(c.tags).table_valued("value")
            conds.append(exists(select(tags.c.value).where(tags.c.value == flt.tag)))

        query = select(backups_t).where(*conds).order_by(c.created_at_utc.desc())
        if flt.limit:
            query = query.limit(flt.limit)
        with self._tx() as conn:
            rows = conn.execute(query).all()
        return [_row_to_backup(r) for r in rows]

    def list_tags(self) -> list[str]:
        """Every tag used by any backup, sorted case-insensitively."""
        with self._tx() as conn:
            rows = conn.exec_driver_sql(
                "SELECT DISTINCT j.value FROM backups, json_each(backups.tags) AS j"
            ).all()
        return sorted({str(r[0]) for r in rows if r[0]}, key=str.casefold)

    def delete_backup(self, backup_id: str) -> None:
        with self._tx() as conn:
            conn.execute(delete(backups_t).where(backups_t.c.backup_id == backup_id))

    # -- jobs -------------------------------------------------------------

    def add_job(self, job: JobRecord) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(jobs_t).values(
                    job_id=job.job_id,
                    kind=job.kind.value,
                    status=job.status.value,
                    title=job.title,
                    connection_id=job.connection_id,
                    backup_id=job.backup_id,
                    batch_id=job.batch_id,
                    started_at_utc=job.started_at_utc,
                    finished_at_utc=job.finished_at_utc,
                    error_message=job.error_message,
                )
            )

    def finish_job(
        self, job_id: str, status: m.JobStatus, error_message: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                update(jobs_t)
                .where(jobs_t.c.job_id == job_id)
                .values(
                    status=status.value,
                    finished_at_utc=m.utc_now(),
                    error_message=error_message,
                )
            )

    def list_jobs(self, limit: int = 200) -> list[JobRecord]:
        with self._tx() as conn:
            rows = conn.execute(
                select(jobs_t).order_by(jobs_t.c.started_at_utc.desc()).limit(limit)
            ).all()
        return [
            JobRecord(
                job_id=r.job_id,
                kind=m.JobKind(r.kind),
                status=m.JobStatus(r.status),
                title=r.title,
                started_at_utc=r.started_at_utc,
                connection_id=r.connection_id,
                backup_id=r.backup_id,
                batch_id=r.batch_id,
                finished_at_utc=r.finished_at_utc,
                error_message=r.error_message,
            )
            for r in rows
        ]

    def add_job_log(self, job_id: str, level: str, message: str) -> None:
        with self._tx() as conn:
            conn.execute(
                insert(job_logs_t).values(
                    job_id=job_id, logged_at_utc=m.utc_now(), level=level, message=message
                )
            )

    def job_logs(self, job_id: str) -> list[tuple[datetime, str, str]]:
        with self._tx() as conn:
            rows = conn.execute(
                select(job_logs_t.c.logged_at_utc, job_logs_t.c.level, job_logs_t.c.message)
                .where(job_logs_t.c.job_id == job_id)
                .order_by(job_logs_t.c.id)
            ).all()
        return [(r.logged_at_utc, r.level, r.message) for r in rows]


# -- row mapping ----------------------------------------------------------


def ufold_name(table: Table) -> ColumnElement[Any]:
    return func.ufold(table.c.name)


def _schedule_to_values(s: m.Schedule) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "connection_id": s.connection_id,
        "databases": list(s.databases),
        "all_databases": int(s.all_databases),
        "frequency": s.frequency.value,
        "time_of_day": s.time_of_day,
        "weekdays": sorted(set(s.weekdays)),
        "every_hours": s.every_hours,
        "keep_last": s.keep_last,
        "description": s.description,
        "tags": list(s.tags),
        "enabled": int(s.enabled),
        "created_at_utc": s.created_at_utc,
        "updated_at_utc": s.updated_at_utc,
        "last_run_utc": s.last_run_utc,
        "last_status": s.last_status.value,
        "last_message": s.last_message,
    }


def _row_to_schedule(row: Row[Any]) -> m.Schedule:
    status = row.last_status if row.last_status in m.ScheduleRunStatus._value2member_map_ else ""
    return m.Schedule(
        id=row.id,
        name=row.name,
        connection_id=row.connection_id,
        databases=list(row.databases or []),
        all_databases=bool(row.all_databases),
        frequency=m.ScheduleFrequency(row.frequency),
        time_of_day=row.time_of_day,
        weekdays=list(row.weekdays or []),
        every_hours=int(row.every_hours),
        keep_last=int(row.keep_last),
        description=row.description,
        tags=list(row.tags or []),
        enabled=bool(row.enabled),
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
        last_run_utc=row.last_run_utc,
        last_status=m.ScheduleRunStatus(status),
        last_message=row.last_message,
    )


def _casefold(value: str | None) -> str | None:
    return value.casefold() if isinstance(value, str) else value


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _to_db_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    return value


def _opt_path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _row_to_connection(row: Row[Any]) -> m.ConnectionProfile:
    return m.ConnectionProfile(
        id=row.id,
        name=row.name,
        engine=m.Engine(row.engine),
        host=row.host,
        port=row.port,
        username=row.username,
        auth_type=m.AuthType(row.auth_type),
        ssl_mode=m.SslMode(row.ssl_mode),
        default_database=row.default_database,
        options=dict(row.options or {}),
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
    )


def _backup_to_values(r: m.BackupRecord) -> dict[str, Any]:
    return {
        "backup_id": r.backup_id,
        "batch_id": r.batch_id,
        "connection_id": r.connection_id,
        "engine": r.engine.value,
        "engine_version": r.engine_version,
        "server_label": r.server_label,
        "database_name": r.database_name,
        "created_at_utc": r.created_at_utc,
        "reference_date": r.reference_date,
        "description": r.description,
        "tags": list(r.tags),
        "backup_format": r.backup_format,
        "backup_mode": r.backup_mode.value,
        "file_path": str(r.file_path),
        "file_size": r.file_size,
        "checksum_sha256": r.checksum_sha256,
        "status": r.status.value,
        "error_message": r.error_message,
        "tool_versions": dict(r.tool_versions),
        "schema_snapshot_path": _to_db_value(r.schema_snapshot_path),
        "comparison_snapshot_path": _to_db_value(r.comparison_snapshot_path),
        "extra": dict(r.extra),
    }


def _row_to_backup(row: Row[Any]) -> m.BackupRecord:
    return m.BackupRecord(
        backup_id=row.backup_id,
        batch_id=row.batch_id,
        connection_id=row.connection_id,
        engine=m.Engine(row.engine),
        engine_version=row.engine_version,
        server_label=row.server_label,
        database_name=row.database_name,
        created_at_utc=row.created_at_utc,
        reference_date=row.reference_date,
        description=row.description,
        tags=list(row.tags or []),
        backup_format=row.backup_format,
        backup_mode=m.BackupMode(row.backup_mode),
        file_path=Path(row.file_path),
        file_size=row.file_size,
        checksum_sha256=row.checksum_sha256,
        status=m.BackupStatus(row.status),
        error_message=row.error_message,
        tool_versions=dict(row.tool_versions or {}),
        schema_snapshot_path=_opt_path(row.schema_snapshot_path),
        comparison_snapshot_path=_opt_path(row.comparison_snapshot_path),
        extra=dict(row.extra or {}),
    )
