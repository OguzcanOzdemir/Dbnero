"""Engine-independent schema comparison over ``SchemaSnapshot`` objects."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from dbnero.compare.snapshots import Column, ForeignKey, Index, Routine, SchemaSnapshot, Table


class ChangeKind(StrEnum):
    ADDED = "added"  # exists only on the right ("target") side
    REMOVED = "removed"  # exists only on the left ("source") side
    CHANGED = "changed"


class Category(StrEnum):
    TABLE = "table"
    COLUMN = "column"
    PRIMARY_KEY = "primary_key"
    INDEX = "index"
    FOREIGN_KEY = "foreign_key"
    VIEW = "view"
    TRIGGER = "trigger"
    PROCEDURE = "procedure"
    FUNCTION = "function"


@dataclass(slots=True)
class DiffItem:
    category: Category
    kind: ChangeKind
    object_name: str  # e.g. "public.orders" or "public.orders.amount"
    detail: str = ""
    left: str | None = None
    right: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: (v.value if isinstance(v, StrEnum) else v) for k, v in asdict(self).items()}


@dataclass(slots=True)
class SchemaDiff:
    left_label: str
    right_label: str
    items: list[DiffItem] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not self.items

    def summary(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for item in self.items:
            bucket = out.setdefault(item.category.value, {k.value: 0 for k in ChangeKind})
            bucket[item.kind.value] += 1
        return out

    def by_category(self) -> dict[Category, list[DiffItem]]:
        out: dict[Category, list[DiffItem]] = {}
        for item in self.items:
            out.setdefault(item.category, []).append(item)
        return out


@dataclass(slots=True)
class DiffOptions:
    ignore_routine_whitespace: bool = True
    compare_column_order: bool = False
    #: When comparing across differently named schemas (e.g. restored copy)
    #: map left schema name -> right schema name.
    schema_map: dict[str, str] = field(default_factory=dict)


_WS = re.compile(r"\s+")


def _norm_def(text: str | None, opts: DiffOptions) -> str:
    if text is None:
        return ""
    return _WS.sub(" ", text).strip() if opts.ignore_routine_whitespace else text


def _col_desc(c: Column) -> str:
    parts = [c.data_type, "NULL" if c.nullable else "NOT NULL"]
    if c.default is not None:
        parts.append(f"DEFAULT {c.default}")
    return " ".join(parts)


def _idx_desc(i: Index) -> str:
    return ("UNIQUE " if i.unique else "") + "(" + ", ".join(i.columns) + ")"


def _fk_desc(f: ForeignKey) -> str:
    desc = f"({', '.join(f.columns)}) -> {f.ref_table}({', '.join(f.ref_columns)})"
    if f.on_delete:
        desc += f" ON DELETE {f.on_delete}"
    if f.on_update:
        desc += f" ON UPDATE {f.on_update}"
    return desc


def _mapped_name(name: str, schema: str, opts: DiffOptions) -> str:
    if schema in opts.schema_map:
        new_schema = opts.schema_map[schema]
        return f"{new_schema}.{name}" if new_schema else name
    return f"{schema}.{name}" if schema else name


def compare_schemas(
    left: SchemaSnapshot,
    right: SchemaSnapshot,
    options: DiffOptions | None = None,
    left_label: str | None = None,
    right_label: str | None = None,
) -> SchemaDiff:
    """Compare two snapshots. "removed" = only in left, "added" = only in right."""
    opts = options or DiffOptions()
    diff = SchemaDiff(left_label or left.database, right_label or right.database)
    items = diff.items

    left_tables = {_mapped_name(t.name, t.schema, opts): t for t in left.tables}
    right_tables = {t.qualified_name: t for t in right.tables}

    for name in sorted(left_tables.keys() - right_tables.keys()):
        items.append(DiffItem(Category.TABLE, ChangeKind.REMOVED, name))
    for name in sorted(right_tables.keys() - left_tables.keys()):
        items.append(DiffItem(Category.TABLE, ChangeKind.ADDED, name))
    for name in sorted(left_tables.keys() & right_tables.keys()):
        _compare_table(name, left_tables[name], right_tables[name], opts, items)

    left_routines = {(r.kind, _mapped_name(r.name, r.schema, opts)): r for r in left.routines}
    right_routines = {(r.kind, r.qualified_name): r for r in right.routines}
    for key in sorted(left_routines.keys() | right_routines.keys()):
        kind, name = key
        category = _routine_category(kind)
        lr, rr = left_routines.get(key), right_routines.get(key)
        if rr is None:
            items.append(DiffItem(category, ChangeKind.REMOVED, name))
        elif lr is None:
            items.append(DiffItem(category, ChangeKind.ADDED, name))
        elif _norm_def(lr.definition, opts) != _norm_def(rr.definition, opts):
            items.append(
                DiffItem(
                    category,
                    ChangeKind.CHANGED,
                    name,
                    "definition differs",
                    lr.definition,
                    rr.definition,
                )
            )
    return diff


def _routine_category(kind: str) -> Category:
    try:
        return Category(kind)
    except ValueError:
        return Category.FUNCTION


def _compare_table(
    name: str, lt: Table, rt: Table, opts: DiffOptions, items: list[DiffItem]
) -> None:
    lcols = {c.name: c for c in lt.columns}
    rcols = {c.name: c for c in rt.columns}
    for col in [c.name for c in lt.columns if c.name not in rcols]:
        items.append(
            DiffItem(
                Category.COLUMN, ChangeKind.REMOVED, f"{name}.{col}", left=_col_desc(lcols[col])
            )
        )
    for col in [c.name for c in rt.columns if c.name not in lcols]:
        items.append(
            DiffItem(
                Category.COLUMN, ChangeKind.ADDED, f"{name}.{col}", right=_col_desc(rcols[col])
            )
        )
    for col in [c.name for c in lt.columns if c.name in rcols]:
        lc, rc = lcols[col], rcols[col]
        changes = []
        if lc.data_type != rc.data_type:
            changes.append("type")
        if lc.nullable != rc.nullable:
            changes.append("nullability")
        if (lc.default or None) != (rc.default or None):
            changes.append("default")
        if changes:
            items.append(
                DiffItem(
                    Category.COLUMN,
                    ChangeKind.CHANGED,
                    f"{name}.{col}",
                    ", ".join(changes),
                    _col_desc(lc),
                    _col_desc(rc),
                )
            )
    if opts.compare_column_order:
        lorder = [c.name for c in lt.columns if c.name in rcols]
        rorder = [c.name for c in rt.columns if c.name in lcols]
        if lorder != rorder:
            items.append(
                DiffItem(
                    Category.COLUMN,
                    ChangeKind.CHANGED,
                    name,
                    "column order",
                    ", ".join(lorder),
                    ", ".join(rorder),
                )
            )

    if lt.primary_key != rt.primary_key:
        kind = (
            ChangeKind.ADDED
            if not lt.primary_key
            else ChangeKind.REMOVED
            if not rt.primary_key
            else ChangeKind.CHANGED
        )
        items.append(
            DiffItem(
                Category.PRIMARY_KEY,
                kind,
                name,
                "",
                ", ".join(lt.primary_key) or None,
                ", ".join(rt.primary_key) or None,
            )
        )

    _compare_named(
        name,
        Category.INDEX,
        {i.name: i for i in lt.indexes},
        {i.name: i for i in rt.indexes},
        _idx_desc,
        items,
    )
    _compare_named(
        name,
        Category.FOREIGN_KEY,
        {f.name: f for f in lt.foreign_keys},
        {f.name: f for f in rt.foreign_keys},
        _fk_desc,
        items,
    )


def _compare_named(
    table: str,
    category: Category,
    left: dict[str, Any],
    right: dict[str, Any],
    describe: Any,
    items: list[DiffItem],
) -> None:
    for key in sorted(left.keys() | right.keys()):
        obj = f"{table}.{key}"
        if key not in right:
            items.append(DiffItem(category, ChangeKind.REMOVED, obj, left=describe(left[key])))
        elif key not in left:
            items.append(DiffItem(category, ChangeKind.ADDED, obj, right=describe(right[key])))
        else:
            ld, rd = describe(left[key]), describe(right[key])
            if ld != rd:
                items.append(DiffItem(category, ChangeKind.CHANGED, obj, "", ld, rd))


__all__ = [
    "Category",
    "ChangeKind",
    "DiffItem",
    "DiffOptions",
    "Routine",
    "SchemaDiff",
    "compare_schemas",
]
