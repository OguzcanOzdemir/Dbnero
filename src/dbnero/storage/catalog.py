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
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
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
                )
            )

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
