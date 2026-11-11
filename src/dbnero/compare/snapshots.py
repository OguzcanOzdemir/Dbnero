"""Normalized, engine-independent schema snapshot model.

Adapters translate their native catalog (``pg_catalog``, ``information_schema``,
``sys.*``) into these structures; the diff engine only ever sees this model.
Snapshots are stored next to backups as JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

SNAPSHOT_FORMAT_VERSION = 1


@dataclass(slots=True)
class Column:
    name: str
    data_type: str
    nullable: bool = True
    default: str | None = None
    ordinal: int = 0


@dataclass(slots=True)
class Index:
    name: str
    columns: list[str]
    unique: bool = False


@dataclass(slots=True)
class ForeignKey:
    name: str
    columns: list[str]
    ref_table: str  # qualified "schema.table"
    ref_columns: list[str]
    on_delete: str | None = None
    on_update: str | None = None


@dataclass(slots=True)
class Table:
    schema: str
    name: str
    columns: list[Column] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    row_count: int | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass(slots=True)
class Routine:
    """A view, trigger, procedure or function, compared by its definition."""

    schema: str
    name: str
    kind: str  # "view" | "trigger" | "procedure" | "function"
    definition: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name


@dataclass(slots=True)
class SchemaSnapshot:
    engine: str
    database: str
    engine_version: str = ""
    captured_at_utc: datetime | None = None
    tables: list[Table] = field(default_factory=list)
    routines: list[Routine] = field(default_factory=list)
    format_version: int = SNAPSHOT_FORMAT_VERSION

    def table_map(self) -> dict[str, Table]:
        return {t.qualified_name: t for t in self.tables}

    def routine_map(self) -> dict[tuple[str, str], Routine]:
        return {(r.kind, r.qualified_name): r for r in self.routines}

    # -- serialization -------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["captured_at_utc"] = self.captured_at_utc.isoformat() if self.captured_at_utc else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchemaSnapshot:
        version = data.get("format_version", SNAPSHOT_FORMAT_VERSION)
        if version > SNAPSHOT_FORMAT_VERSION:
            raise ValueError(f"Unsupported snapshot format version: {version}")
        captured = data.get("captured_at_utc")
        return cls(
            engine=data["engine"],
            database=data["database"],
            engine_version=data.get("engine_version", ""),
            captured_at_utc=datetime.fromisoformat(captured) if captured else None,
            tables=[
                Table(
                    schema=t["schema"],
                    name=t["name"],
                    columns=[Column(**c) for c in t.get("columns", [])],
                    primary_key=list(t.get("primary_key", [])),
                    indexes=[Index(**i) for i in t.get("indexes", [])],
                    foreign_keys=[ForeignKey(**f) for f in t.get("foreign_keys", [])],
                    row_count=t.get("row_count"),
                )
                for t in data.get("tables", [])
            ],
            routines=[Routine(**r) for r in data.get("routines", [])],
            format_version=version,
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), "utf-8")

    @classmethod
    def load(cls, path: Path) -> SchemaSnapshot:
        return cls.from_dict(json.loads(path.read_text("utf-8")))
