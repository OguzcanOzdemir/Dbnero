"""Adapter contract shared by all database engines.

Everything engine-specific (drivers, CLI tools, SQL dialects) lives behind
``DatabaseAdapter``. Services obtain an adapter via ``create_adapter`` and
never branch on the engine themselves.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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

    Adapters are constructed with a connection profile and the password read
    from the keyring; they must never persist or log the password.
    """

    profile: ConnectionProfile

    def test_connection(self) -> str:
        """Connect and return the server version string. Raise ConnectionFailed."""
        ...

    def list_databases(self, include_system: bool = False) -> list[str]: ...

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


AdapterFactory = Callable[[ConnectionProfile, str | None], DatabaseAdapter]

_REGISTRY: dict[Engine, AdapterFactory] = {}


def register_adapter(engine: Engine, factory: AdapterFactory) -> None:
    _REGISTRY[engine] = factory


def create_adapter(profile: ConnectionProfile, password: str | None) -> DatabaseAdapter:
    try:
        factory = _REGISTRY[profile.engine]
    except KeyError:
        raise AdapterError(
            f"No adapter registered for engine {profile.engine.display_name}"
        ) from None
    return factory(profile, password)


def registered_engines() -> list[Engine]:
    return list(_REGISTRY)
