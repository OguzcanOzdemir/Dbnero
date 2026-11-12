from dbnero.compare.data_diff import DataDiffOptions, DataMethod, compare_data
from dbnero.core.models import ConnectionProfile, Engine
from tests.unit.fakes import FakeAdapter, FakeServer, FakeTable


def _adapter(tables):
    server = FakeServer(databases={"db": tables})
    profile = ConnectionProfile(name="x", engine=Engine.POSTGRESQL, host="h", port=1)
    return FakeAdapter(profile, None, server)


def _run(left_tables, right_tables, **opts):
    la, ra = _adapter(left_tables), _adapter(right_tables)
    return compare_data(
        la,
        "db",
        la.inspect_schema("db"),
        ra,
        "db",
        ra.inspect_schema("db"),
        DataDiffOptions(**opts),
    )


def test_keyed_diff_counts_and_samples():
    left = {"t": FakeTable(["id", "v"], ["id"], [(1, "a"), (2, "b"), (3, "c")])}
    right = {"t": FakeTable(["id", "v"], ["id"], [(2, "B"), (3, "c"), (4, "d")])}
    (t,) = _run(left, right).tables
    assert t.method is DataMethod.KEYED
    assert (t.added, t.removed, t.changed, t.identical) == (1, 1, 1, False)
    assert {s.kind for s in t.samples} == {"added", "removed", "changed"}
    changed = next(s for s in t.samples if s.kind == "changed")
    assert changed.key == (2,) and changed.left == (2, "b") and changed.right == (2, "B")


def test_identical_and_sample_limit():
    rows = [(i, str(i)) for i in range(100)]
    same = _run(
        {"t": FakeTable(["id", "v"], ["id"], rows)}, {"t": FakeTable(["id", "v"], ["id"], rows)}
    )
    assert same.identical
    diff = _run(
        {"t": FakeTable(["id", "v"], ["id"], rows)},
        {"t": FakeTable(["id", "v"], ["id"], [])},
        max_samples=5,
    )
    assert diff.tables[0].removed == 100 and len(diff.tables[0].samples) == 5


def test_fallback_when_db_order_differs_from_python(monkeypatch):
    # Simulate a case-insensitive collation: DB returns keys in an order Python disagrees with.
    left = {"t": FakeTable(["id", "v"], ["id"], [("a", 1), ("B", 2), ("c", 3)])}
    right = {"t": FakeTable(["id", "v"], ["id"], [("a", 1), ("B", 2), ("c", 4)])}
    la, ra = _adapter(left), _adapter(right)

    def ci_rows(self, database, schema, table, columns, order_by, batch_size=5000):
        rows = [tuple(r) for r in self.server.databases[database][table].rows]
        return iter(sorted(rows, key=lambda r: str(r[0]).lower()))

    monkeypatch.setattr(FakeAdapter, "iter_rows", ci_rows)
    (t,) = compare_data(la, "db", la.inspect_schema("db"), ra, "db", ra.inspect_schema("db")).tables
    assert (t.added, t.removed, t.changed) == (0, 0, 1)
    assert "in memory" in t.note


def test_fingerprint_for_tables_without_key():
    a = {"t": FakeTable(["msg"], [], [("x",), ("y",)])}
    b = {"t": FakeTable(["msg"], [], [("y",), ("x",)])}
    c = {"t": FakeTable(["msg"], [], [("y",), ("z",)])}
    assert _run(a, b).tables[0].identical
    res = _run(a, c).tables[0]
    assert res.method is DataMethod.FINGERPRINT and res.identical is False


def test_count_only_and_skipped():
    a = {"t": FakeTable(["id"], ["id"], [(1,)]), "only_left": FakeTable(["id"], ["id"])}
    b = {"t": FakeTable(["id"], ["id"], [(1,), (2,)])}
    res = _run(a, b, count_only=True)
    assert res.tables[0].method is DataMethod.COUNT_ONLY and res.tables[0].identical is False
    assert res.skipped == ["public.only_left"]
    assert _run(a, b, tables=[]).tables == []


def test_tables_on_one_side_are_listed():
    a = {"customers": FakeTable(["id"], ["id"], [(1,), (2,)])}
    b = {"invoices": FakeTable(["id"], ["id"], [(1,)])}
    res = _run(a, b)
    by_name = {t.table: t for t in res.tables}
    assert by_name["public.customers"].method is DataMethod.ONE_SIDE
    assert by_name["public.customers"].left_rows == 2 and by_name["public.customers"].removed == 2
    assert by_name["public.invoices"].right_rows == 1 and by_name["public.invoices"].added == 1
    assert not res.identical
