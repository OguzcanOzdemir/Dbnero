from datetime import UTC, datetime

import pytest

from dbnero.core.models import Engine
from dbnero.storage.archive import (
    backup_paths,
    finalize,
    find_stale_partials,
    partial_file,
    sanitize_component,
    sha256_file,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("crm", "crm"),
        ("db.example.com:5432", "db.example.com_5432"),
        ("../../etc", "etc"),
        ("Müşteri Veri", "Musteri_Veri"),
        ("CON", "_CON"),
        ("a/b\\c", "a_b_c"),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize_component(raw) == expected


def test_sanitize_non_ascii_is_stable_and_distinct():
    a, b = sanitize_component("数据"), sanitize_component("資料")
    assert a.startswith("x") and a != b and a == sanitize_component("数据")


def test_backup_paths(tmp_path):
    ts = datetime(2026, 11, 11, 20, 40, 0, tzinfo=UTC)
    p = backup_paths(tmp_path, Engine.POSTGRESQL, "db:5432", "crm", ts, "ab12cd34", "dump")
    assert p.backup == (
        tmp_path
        / "postgresql"
        / "db_5432"
        / "crm"
        / "2026"
        / "11"
        / "2026-11-11_204000_postgresql_crm_ab12cd34.dump"
    )
    assert p.manifest.name == "2026-11-11_204000_postgresql_crm_ab12cd34.manifest.json"
    assert p.schema.name.endswith(".schema.json")
    assert p.partial.name.endswith(".dump.partial")


def test_partial_and_finalize(tmp_path):
    final = tmp_path / "sub" / "b.dump"
    with partial_file(final) as partial:
        partial.write_bytes(b"data")
        assert find_stale_partials(tmp_path) == [partial]
        finalize(partial, final)
    assert final.read_bytes() == b"data"
    assert find_stale_partials(tmp_path) == []
    assert sha256_file(final) == (
        "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"
    )


def test_partial_removed_on_error(tmp_path):
    final = tmp_path / "b.dump"
    with pytest.raises(RuntimeError), partial_file(final) as partial:
        partial.write_bytes(b"half")
        raise RuntimeError("tool failed")
    assert not partial.exists() and not final.exists()


def test_finalize_never_overwrites(tmp_path):
    final = tmp_path / "b.dump"
    final.write_bytes(b"old")
    partial = tmp_path / "b.dump.partial"
    partial.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        finalize(partial, final)
    assert final.read_bytes() == b"old"
