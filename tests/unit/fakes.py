"""In-memory fake adapter used by service-level unit tests."""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dbnero.adapters.base import AdapterError, JobContext
from dbnero.compare.snapshots import Column, SchemaSnapshot, Table
from dbnero.core.models import (
    BackupResult,
    ConnectionProfile,
    RestoreResult,
    ValidationResult,
    utc_now,
)

MAGIC = "FAKEDUMP"


@dataclass
class FakeTable:
    columns: list[str]
    pk: list[str]
    rows: list[tuple[Any, ...]] = field(default_factory=list)


@dataclass
class FakeServer:
    version: str = "16.1"
    databases: dict[str, dict[str, FakeTable]] = field(default_factory=dict)
    system: set[str] = field(default_factory=lambda: {"sysdb"})
    fail_backup: set[str] = field(default_factory=set)
    slow_backup: set[str] = field(default_factory=set)
    unreachable: bool = False
    restores: list[str] = field(default_factory=list)


class FakeAdapter:
    def __init__(self, profile: ConnectionProfile, password: str | None, server: FakeServer):
        self.profile = profile
        self.password = password
        self.server = server

    def _check(self) -> None:
        if self.server.unreachable:
            raise AdapterError("connection refused")

    def backup_format(self, options: dict[str, Any]) -> tuple[str, str]:
        return "fake", "fake_json"

    def test_connection(self) -> str:
        self._check()
        return self.server.version

    def list_databases(self, include_system: bool = False) -> list[str]:
        self._check()
        names = sorted(self.server.databases)
        return sorted(set(names) | self.server.system) if include_system else names

    def create_database(self, name: str) -> None:
        if name in self.server.databases:
            raise AdapterError("exists")
        self.server.databases[name] = {}

    def drop_database(self, name: str) -> None:
        self.server.databases.pop(name, None)

    def database_size_bytes(self, database: str) -> int | None:
        return 1000

    def backup(self, database, destination: Path, options, ctx: JobContext | None = None):
        self._check()
        if database in self.server.fail_backup:
            destination.write_bytes(b"partial garbage")
            raise AdapterError(f"backup of {database} failed")
        if database in self.server.slow_backup:
            destination.write_bytes(b"partial")
            for _ in range(200):
                if ctx:
                    ctx.raise_if_cancelled()
                time.sleep(0.01)
        payload = {
            name: {"columns": t.columns, "pk": t.pk, "rows": [list(r) for r in t.rows]}
            for name, t in self.server.databases[database].items()
        }
        destination.write_text(MAGIC + "\n" + json.dumps(payload))
        return BackupResult(destination, "fake_json", self.server.version, {"fake": "1.0"})

    def restore(self, source: Path, target_database, options, ctx=None):
        exists = target_database in self.server.databases
        if exists and not options.get("overwrite"):
            raise AdapterError("already exists")
        payload = json.loads(source.read_text("utf-8").split("\n", 1)[1])
        self.server.databases[target_database] = {
            name: FakeTable(t["columns"], t["pk"], [tuple(r) for r in t["rows"]])
            for name, t in payload.items()
        }
        self.server.restores.append(target_database)
        return RestoreResult(target_database)

    def validate_backup(self, source: Path) -> ValidationResult:
        ok = source.is_file() and source.read_text("utf-8").startswith(MAGIC)
        return ValidationResult(ok, ["fake ok"] if ok else [], [] if ok else ["bad fake dump"])

    def inspect_schema(self, database: str) -> SchemaSnapshot:
        self._check()
        db = self.server.databases[database]
        return SchemaSnapshot(
            engine=self.profile.engine.value,
            database=database,
            engine_version=self.server.version,
            captured_at_utc=utc_now(),
            tables=[
                Table(
                    schema="public",
                    name=name,
                    columns=[Column(c, "int" if c == "id" else "text") for c in t.columns],
                    primary_key=list(t.pk),
                )
                for name, t in sorted(db.items())
            ],
        )

    def count_rows(self, database: str, schema: str, table: str) -> int:
        return len(self.server.databases[database][table].rows)

    def iter_rows(
        self,
        database,
        schema,
        table,
        columns: Sequence[str],
        order_by: Sequence[str],
        batch_size: int = 5000,
    ) -> Iterator[tuple[Any, ...]]:
        t = self.server.databases[database][table]
        idx = [t.columns.index(c) for c in columns]
        rows = [tuple(r[i] for i in idx) for r in t.rows]
        if order_by:
            key_idx = [list(columns).index(c) for c in order_by]
            rows.sort(key=lambda r: tuple(r[i] for i in key_idx))
        return iter(copy.deepcopy(rows))


def sample_db() -> dict[str, FakeTable]:
    return {
        "customers": FakeTable(["id", "email"], ["id"], [(i, f"u{i}@x") for i in range(1, 21)]),
        "logs": FakeTable(["msg"], [], [("a",), ("b",)]),
    }
