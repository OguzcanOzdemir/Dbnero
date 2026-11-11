from datetime import UTC, datetime

import pytest

from dbnero.compare.snapshots import (
    Column,
    ForeignKey,
    Index,
    Routine,
    SchemaSnapshot,
    Table,
)


def _snap():
    return SchemaSnapshot(
        engine="postgresql",
        database="crm",
        engine_version="16.4",
        captured_at_utc=datetime(2026, 11, 11, 20, 40, tzinfo=UTC),
        tables=[
            Table(
                schema="public",
                name="orders",
                columns=[Column("id", "integer", False, None, 1), Column("note", "text")],
                primary_key=["id"],
                indexes=[Index("ix_note", ["note"])],
                foreign_keys=[ForeignKey("fk", ["cid"], "public.customers", ["id"], "CASCADE")],
                row_count=10,
            )
        ],
        routines=[Routine("public", "v_orders", "view", "select 1")],
    )


def test_roundtrip(tmp_path):
    s = _snap()
    path = tmp_path / "s.json"
    s.save(path)
    assert SchemaSnapshot.load(path) == s


def test_maps():
    s = _snap()
    assert "public.orders" in s.table_map()
    assert ("view", "public.v_orders") in s.routine_map()


def test_rejects_future_version():
    data = _snap().to_dict()
    data["format_version"] = 999
    with pytest.raises(ValueError):
        SchemaSnapshot.from_dict(data)
