"""Adapter contract shared by all database engines.

Everything engine-specific (drivers, CLI tools, SQL dialects) lives behind
``DatabaseAdapter``. Services obtain an adapter via ``create_adapter`` and
never branch on the engine themselves.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dbnero.compare.snapshots import SchemaSnapshot
from dbnero.core.models import (
    BackupResult,
    ConnectionProfile,
    Engine,
    RestoreResult,
    ValidationResult,
)

ProgressCallback = Callable[[float | None, str], None]
"""Called with (fraction 0..1 or None if unknown, human readable message)."""


class AdapterError(Exception):
    """Base error raised by adapters. Messages must never contain secrets."""


class ConnectionFailed(AdapterError):
    pass


class ToolNotFound(AdapterError):
    def __init__(self, tool: str) -> None:
        super().__init__(f"Required tool not found: {tool}")
        self.tool = tool


class OperationCancelled(AdapterError):
    def __init__(self) -> None:
        super().__init__("Operation cancelled")


@dataclass(slots=True)
class JobContext:
    """Cancellation flag and progress sink passed to long-running operations."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    on_progress: ProgressCallback | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self) -> None:
        self.cancel_event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise OperationCancelled

    def progress(self, fraction: float | None, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(fraction, message)


@runtime_checkable
class DatabaseAdapter(Protocol):
    """Operations every engine must support.

    Adapters are constructed with a connection profile, the password read
    from the keyring and optional tool path overrides; they must never
    persist or log the password.

    Common ``options`` keys:

    * backup: ``compress`` (bool, MySQL) - engine-specific extras are ignored
      by other engines.
    * restore: ``overwrite`` (bool) - replace an existing target database;
      ``no_owner`` (bool, PostgreSQL).
    """

    profile: ConnectionProfile

    #: File extension (without dot) and format label produced by ``backup``.
    def backup_format(self, options: dict[str, Any]) -> tuple[str, str]: ...

    def test_connection(self) -> str:
        """Connect and return the server version string. Raise ConnectionFailed."""
        ...

    def list_databases(self, include_system: bool = False) -> list[str]: ...

    def create_database(self, name: str) -> None: ...

    def drop_database(self, name: str) -> None: ...

    def database_size_bytes(self, database: str) -> int | None:
        """Approximate on-disk size, used for free-space checks."""
        ...

    def backup(
        self,
        database: str,
        destination: Path,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> BackupResult: ...

    def restore(
        self,
        source: Path,
        target_database: str,
        options: dict[str, Any],
        ctx: JobContext | None = None,
    ) -> RestoreResult: ...

    def inspect_schema(self, database: str) -> SchemaSnapshot: ...

    def validate_backup(self, source: Path) -> ValidationResult: ...

    def count_rows(self, database: str, schema: str, table: str) -> int: ...

    def iter_rows(
        self,
        database: str,
        schema: str,
        table: str,
        columns: Sequence[str],
        order_by: Sequence[str],
        batch_size: int = 5000,
    ) -> Iterator[tuple[Any, ...]]:
        """Stream rows ordered by ``order_by`` (used by the data diff engine)."""
        ...


class AdapterFactory(Protocol):
    def __call__(
        self,
        profile: ConnectionProfile,
        password: str | None,
        tool_paths: dict[str, str] | None = None,
    ) -> DatabaseAdapter: ...


_REGISTRY: dict[Engine, AdapterFactory] = {}


def register_adapter(engine: Engine, factory: AdapterFactory) -> None:
    _REGISTRY[engine] = factory


def create_adapter(
    profile: ConnectionProfile,
    password: str | None,
    tool_paths: dict[str, str] | None = None,
) -> DatabaseAdapter:
    _ensure_builtin_adapters()
    try:
        factory = _REGISTRY[profile.engine]
    except KeyError:
        raise AdapterError(
            f"No adapter registered for engine {profile.engine.display_name}"
        ) from None
    return factory(profile, password, tool_paths)


def registered_engines() -> list[Engine]:
    _ensure_builtin_adapters()
    return list(_REGISTRY)


def _ensure_builtin_adapters() -> None:
    # Imported lazily: each module registers itself and imports its driver
    # only when a connection is actually opened.
    from dbnero.adapters import mssql, mysql, postgresql  # noqa: F401
