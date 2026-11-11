"""Engine-independent domain models.

These are plain dataclasses with no Qt or SQLAlchemy dependency so that the
service layer can be tested without a GUI or a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Engine(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MSSQL = "mssql"

    @property
    def display_name(self) -> str:
        return {
            Engine.POSTGRESQL: "PostgreSQL",
            Engine.MYSQL: "MySQL",
            Engine.MSSQL: "Microsoft SQL Server",
        }[self]

    @property
    def default_port(self) -> int:
        return {Engine.POSTGRESQL: 5432, Engine.MYSQL: 3306, Engine.MSSQL: 1433}[self]


class AuthType(StrEnum):
    PASSWORD = "password"  # noqa: S105 - enum label, not a secret
    WINDOWS = "windows"  # MSSQL integrated security


class SslMode(StrEnum):
    DISABLE = "disable"
    PREFER = "prefer"
    REQUIRE = "require"
    VERIFY_FULL = "verify-full"


class BackupStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    CANCELLED = "cancelled"


class BackupMode(StrEnum):
    SINGLE = "single"
    BATCH = "batch"


class JobKind(StrEnum):
    BACKUP = "backup"
    RESTORE = "restore"
    COMPARE = "compare"
    VALIDATE = "validate"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class ConnectionProfile:
    """A saved server connection. The password is never stored here; it lives
    in the OS keyring keyed by ``id`` (see ``dbnero.security.credentials``)."""

    name: str
    engine: Engine
    host: str
    port: int
    username: str = ""
    auth_type: AuthType = AuthType.PASSWORD
    ssl_mode: SslMode = SslMode.PREFER
    default_database: str = ""
    options: dict[str, str] = field(default_factory=dict)
    id: str = field(default_factory=new_id)
    created_at_utc: datetime = field(default_factory=utc_now)
    updated_at_utc: datetime = field(default_factory=utc_now)

    @property
    def server_label(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(slots=True)
class BackupRecord:
    """Catalog entry for one backup file of one database."""

    connection_id: str
    engine: Engine
    database_name: str
    server_label: str
    backup_format: str
    file_path: Path
    backup_mode: BackupMode = BackupMode.SINGLE
    engine_version: str = ""
    batch_id: str | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)
    reference_date: datetime | None = None
    file_size: int | None = None
    checksum_sha256: str | None = None
    status: BackupStatus = BackupStatus.PENDING
    error_message: str | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    schema_snapshot_path: Path | None = None
    comparison_snapshot_path: Path | None = None
    backup_id: str = field(default_factory=new_id)
    # Assigned by the system and immutable after creation.
    created_at_utc: datetime = field(default_factory=utc_now)

    @property
    def short_id(self) -> str:
        return self.backup_id.split("-")[0]


@dataclass(slots=True)
class BackupBatch:
    connection_id: str
    database_names: list[str]
    description: str = ""
    batch_id: str = field(default_factory=new_id)
    created_at_utc: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class BackupResult:
    """Returned by an adapter after writing a backup file."""

    file_path: Path
    backup_format: str
    engine_version: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RestoreResult:
    target_database: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ValidationResult:
    ok: bool
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
