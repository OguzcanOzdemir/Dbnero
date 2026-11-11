"""Row-level data comparison between two live databases of the same engine.

* Tables with a primary key: both sides are streamed ordered by the key and
  merge-joined, producing added / removed / changed row counts plus samples.
  If the database's key ordering does not match Python ordering (collations),
  the table falls back to an in-memory keyed comparison up to a row limit.
* Tables without a primary key: row counts plus an order-independent
  fingerprint of all rows ("same" / "different", no row-level detail).

A backup is compared by first restoring it into an isolated temporary
database (see ``core.compare_service``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from dbnero.adapters.base import DatabaseAdapter, JobContext
from dbnero.compare.snapshots import SchemaSnapshot, Table


class DataMethod(StrEnum):
    KEYED = "keyed"
    FINGERPRINT = "fingerprint"
    COUNT_ONLY = "count_only"


@dataclass(slots=True)
class RowSample:
    kind: str  # "added" | "removed" | "changed"
    key: tuple[Any, ...]
    left: tuple[Any, ...] | None = None
    right: tuple[Any, ...] | None = None


@dataclass(slots=True)
class TableDataDiff:
    table: str
    method: DataMethod
    left_rows: int = 0
    right_rows: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    identical: bool | None = None
    columns: list[str] = field(default_factory=list)
    key_columns: list[str] = field(default_factory=list)
    samples: list[RowSample] = field(default_factory=list)
    note: str = ""


@dataclass(slots=True)
class DataDiff:
    left_label: str
    right_label: str
    tables: list[TableDataDiff] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return all(t.identical for t in self.tables)


@dataclass(slots=True)
class DataDiffOptions:
    tables: list[str] | None = None  # qualified names; None = all common tables
    max_samples: int = 20
    in_memory_row_limit: int = 2_000_000
    count_only: bool = False
    batch_size: int = 5000


class _OrderViolation(Exception):
    pass


def compare_data(
    left: DatabaseAdapter,
    left_db: str,
    left_schema: SchemaSnapshot,
    right: DatabaseAdapter,
    right_db: str,
    right_schema: SchemaSnapshot,
    options: DataDiffOptions | None = None,
    ctx: JobContext | None = None,
    left_label: str | None = None,
    right_label: str | None = None,
) -> DataDiff:
    opts = options or DataDiffOptions()
    result = DataDiff(left_label or left_db, right_label or right_db)
    lmap, rmap = left_schema.table_map(), right_schema.table_map()
    names = sorted(lmap.keys() & rmap.keys())
    if opts.tables is not None:
        wanted = set(opts.tables)
        names = [n for n in names if n in wanted]
    result.skipped = sorted(lmap.keys() ^ rmap.keys())

    for i, name in enumerate(names):
        if ctx:
            ctx.raise_if_cancelled()
            ctx.progress(i / max(len(names), 1), f"Comparing data: {name}")
        result.tables.append(
            _compare_table(left, left_db, lmap[name], right, right_db, rmap[name], opts, ctx)
        )
    if ctx:
        ctx.progress(1.0, "Data comparison finished")
    return result


def _compare_table(
    left: DatabaseAdapter,
    left_db: str,
    lt: Table,
    right: DatabaseAdapter,
    right_db: str,
    rt: Table,
    opts: DataDiffOptions,
    ctx: JobContext | None,
) -> TableDataDiff:
    rnames = {c.name for c in rt.columns}
    columns = [c.name for c in lt.columns if c.name in rnames]
    key = list(lt.primary_key) if lt.primary_key == rt.primary_key else []
    diff = TableDataDiff(table=lt.qualified_name, method=DataMethod.COUNT_ONLY, columns=columns)
    diff.left_rows = left.count_rows(left_db, lt.schema, lt.name)
    diff.right_rows = right.count_rows(right_db, rt.schema, rt.name)

    if opts.count_only or not columns:
        diff.identical = diff.left_rows == diff.right_rows
        diff.note = "Row counts only" if opts.count_only else "No common columns"
        return diff

    if key and all(k in columns for k in key):
        diff.method = DataMethod.KEYED
        diff.key_columns = key
        key_idx = [columns.index(k) for k in key]

        def stream(adapter: DatabaseAdapter, db: str, t: Table) -> Iterator[tuple[Any, ...]]:
            return adapter.iter_rows(db, t.schema, t.name, columns, key, opts.batch_size)

        try:
            _merge_join(
                stream(left, left_db, lt), stream(right, right_db, rt), key_idx, diff, opts, ctx
            )
        except _OrderViolation:
            _reset_counts(diff)
            if max(diff.left_rows, diff.right_rows) > opts.in_memory_row_limit:
                diff.method = DataMethod.COUNT_ONLY
                diff.identical = None
                diff.note = (
                    "Key order depends on the collation and the table is too large to "
                    "compare in memory; only row counts were compared"
                )
                return diff
            _hash_join(stream(left, left_db, lt), stream(right, right_db, rt), key_idx, diff, opts)
            diff.note = "Compared in memory (collation-dependent key order)"
        diff.identical = diff.added == diff.removed == diff.changed == 0
        return diff

    diff.method = DataMethod.FINGERPRINT
    diff.note = "No primary key: compared with an order-independent fingerprint"
    lf = _fingerprint(left.iter_rows(left_db, lt.schema, lt.name, columns, [], opts.batch_size))
    rf = _fingerprint(right.iter_rows(right_db, rt.schema, rt.name, columns, [], opts.batch_size))
    diff.identical = lf == rf and diff.left_rows == diff.right_rows
    return diff


def _reset_counts(diff: TableDataDiff) -> None:
    diff.added = diff.removed = diff.changed = 0
    diff.samples.clear()


def _key(row: Sequence[Any], idx: list[int]) -> tuple[Any, ...]:
    return tuple(row[i] for i in idx)


def _sortable(key: tuple[Any, ...]) -> tuple[Any, ...]:
    # NULLs never appear in primary keys, but guard against mixed types anyway.
    return tuple((v is None, v) for v in key)


def _add_sample(diff: TableDataDiff, opts: DataDiffOptions, sample: RowSample) -> None:
    if len(diff.samples) < opts.max_samples:
        diff.samples.append(sample)


def _merge_join(
    left: Iterator[tuple[Any, ...]],
    right: Iterator[tuple[Any, ...]],
    key_idx: list[int],
    diff: TableDataDiff,
    opts: DataDiffOptions,
    ctx: JobContext | None,
) -> None:
    sentinel: tuple[Any, ...] | None = None
    lrow, rrow = next(left, sentinel), next(right, sentinel)
    last_l: tuple[Any, ...] | None = None
    last_r: tuple[Any, ...] | None = None
    processed = 0

    def check(prev: tuple[Any, ...] | None, cur: tuple[Any, ...]) -> tuple[Any, ...]:
        s = _sortable(cur)
        if prev is not None and s <= prev:
            raise _OrderViolation
        return s

    while lrow is not None or rrow is not None:
        processed += 1
        if ctx and processed % 10000 == 0:
            ctx.raise_if_cancelled()
        if rrow is None or (
            lrow is not None and _sortable(_key(lrow, key_idx)) < _sortable(_key(rrow, key_idx))
        ):
            assert lrow is not None  # noqa: S101
            last_l = check(last_l, _key(lrow, key_idx))
            diff.removed += 1
            _add_sample(diff, opts, RowSample("removed", _key(lrow, key_idx), left=lrow))
            lrow = next(left, sentinel)
        elif lrow is None or _sortable(_key(rrow, key_idx)) < _sortable(_key(lrow, key_idx)):
            last_r = check(last_r, _key(rrow, key_idx))
            diff.added += 1
            _add_sample(diff, opts, RowSample("added", _key(rrow, key_idx), right=rrow))
            rrow = next(right, sentinel)
        else:
            last_l = check(last_l, _key(lrow, key_idx))
            last_r = check(last_r, _key(rrow, key_idx))
            if tuple(lrow) != tuple(rrow):
                diff.changed += 1
                _add_sample(
                    diff, opts, RowSample("changed", _key(lrow, key_idx), tuple(lrow), tuple(rrow))
                )
            lrow, rrow = next(left, sentinel), next(right, sentinel)


def _hash_join(
    left: Iterator[tuple[Any, ...]],
    right: Iterator[tuple[Any, ...]],
    key_idx: list[int],
    diff: TableDataDiff,
    opts: DataDiffOptions,
) -> None:
    left_rows = {_key(r, key_idx): tuple(r) for r in left}
    for row in right:
        k = _key(row, key_idx)
        lrow = left_rows.pop(k, None)
        if lrow is None:
            diff.added += 1
            _add_sample(diff, opts, RowSample("added", k, right=tuple(row)))
        elif lrow != tuple(row):
            diff.changed += 1
            _add_sample(diff, opts, RowSample("changed", k, lrow, tuple(row)))
    for k, lrow in left_rows.items():
        diff.removed += 1
        _add_sample(diff, opts, RowSample("removed", k, left=lrow))


def _fingerprint(rows: Iterator[tuple[Any, ...]]) -> tuple[int, int]:
    """Order-independent multiset fingerprint: (sum, xor) of 64-bit row hashes."""
    total = 0
    xor = 0
    for row in rows:
        h = int.from_bytes(hashlib.blake2b(repr(tuple(row)).encode(), digest_size=8).digest())
        total = (total + h) & 0xFFFFFFFFFFFFFFFF
        xor ^= h
    return total, xor
