from dbnero.compare.schema_diff import Category, ChangeKind, DiffOptions, compare_schemas
from dbnero.compare.snapshots import Column, ForeignKey, Index, Routine, SchemaSnapshot, Table


def _snap(tables, routines=()):
    return SchemaSnapshot(engine="postgresql", database="d", tables=tables, routines=list(routines))


def _orders(**kw):
    t = Table(
        schema="public",
        name="orders",
        columns=[
            Column("id", "integer", False),
            Column("amount", "numeric(12,2)", False),
            Column("note", "text"),
        ],
        primary_key=["id"],
        indexes=[Index("ix_amount", ["amount"])],
        foreign_keys=[ForeignKey("fk_c", ["cid"], "public.customers", ["id"], "CASCADE")],
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def kinds(diff):
    return sorted((i.category.value, i.kind.value, i.object_name) for i in diff.items)


def test_identical():
    d = compare_schemas(_snap([_orders()]), _snap([_orders()]))
    assert d.identical and d.summary() == {}


def test_tables_added_removed():
    a = _snap([_orders(), Table("public", "old")])
    b = _snap([_orders(), Table("public", "new")])
    assert kinds(compare_schemas(a, b)) == [
        ("table", "added", "public.new"),
        ("table", "removed", "public.old"),
    ]


def test_column_changes():
    right = _orders(
        columns=[
            Column("id", "bigint", False),
            Column("amount", "numeric(12,2)", True, "0"),
            Column("created", "timestamp"),
        ]
    )
    d = compare_schemas(_snap([_orders()]), _snap([right]))
    items = {(i.object_name, i.kind): i for i in d.items}
    assert items[("public.orders.id", ChangeKind.CHANGED)].detail == "type"
    assert items[("public.orders.amount", ChangeKind.CHANGED)].detail == "nullability, default"
    assert ("public.orders.note", ChangeKind.REMOVED) in items
    assert ("public.orders.created", ChangeKind.ADDED) in items
    assert d.summary()["column"] == {"added": 1, "removed": 1, "changed": 2}


def test_keys_and_indexes():
    right = _orders(
        primary_key=["id", "amount"],
        indexes=[Index("ix_amount", ["amount"], unique=True), Index("ix_note", ["note"])],
        foreign_keys=[],
    )
    got = kinds(compare_schemas(_snap([_orders()]), _snap([right])))
    assert ("primary_key", "changed", "public.orders") in got
    assert ("index", "changed", "public.orders.ix_amount") in got
    assert ("index", "added", "public.orders.ix_note") in got
    assert ("foreign_key", "removed", "public.orders.fk_c") in got


def test_routines_whitespace_insensitive():
    a = _snap([], [Routine("public", "v", "view", "SELECT  1\n FROM t")])
    b = _snap([], [Routine("public", "v", "view", "SELECT 1 FROM t")])
    assert compare_schemas(a, b).identical
    assert not compare_schemas(a, b, DiffOptions(ignore_routine_whitespace=False)).identical
    c = _snap([], [Routine("public", "v", "view", "SELECT 2"), Routine("public", "p", "procedure")])
    got = kinds(compare_schemas(b, c))
    assert ("view", "changed", "public.v") in got and ("procedure", "added", "public.p") in got


def test_schema_map_and_column_order():
    a = _snap([Table("sales", "t", [Column("a", "int"), Column("b", "int")])])
    b = _snap([Table("public", "t", [Column("b", "int"), Column("a", "int")])])
    assert compare_schemas(a, b, DiffOptions(schema_map={"sales": "public"})).identical
    d = compare_schemas(
        a, b, DiffOptions(schema_map={"sales": "public"}, compare_column_order=True)
    )
    assert d.items[0].detail == "column order"
    assert d.by_category()[Category.COLUMN]
