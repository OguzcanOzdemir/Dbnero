"""Backup -> validate -> restore -> schema compare round trip for every engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbnero.adapters.base import AdapterError, JobContext


def _schema_signature(snap):
    return {
        t.qualified_name: (
            [(c.name, c.data_type, c.nullable) for c in t.columns],
            t.primary_key,
            sorted((i.name, tuple(i.columns), i.unique) for i in t.indexes),
            sorted((f.name, tuple(f.columns), f.ref_table) for f in t.foreign_keys),
        )
        for t in snap.tables
    }


def test_connection_and_listing(seeded):
    target, db = seeded
    adapter = target.adapter()
    assert adapter.test_connection()
    assert db in adapter.list_databases()
    system = set(adapter.list_databases(include_system=True)) - set(adapter.list_databases())
    assert system  # every engine has system databases that are hidden by default


def test_inspect_schema(seeded):
    target, db = seeded
    snap = target.adapter().inspect_schema(db)
    tables = {t.name: t for t in snap.tables}
    assert {"customers", "orders", "audit_log"} <= set(tables)
    assert tables["customers"].primary_key == ["id"]
    assert tables["orders"].foreign_keys and tables["orders"].foreign_keys[0].columns == [
        "customer_id"
    ]
    assert any(i.columns == ["customer_id"] for i in tables["orders"].indexes)
    kinds = {r.kind for r in snap.routines}
    assert {"view", "trigger"} <= kinds


def test_backup_restore_roundtrip(seeded, scratch_db_name, tmp_path: Path):
    target, db = seeded
    adapter = target.adapter()
    ext, label = adapter.backup_format({})
    dest = tmp_path / f"backup.{ext}"
    progress: list[str] = []
    ctx = JobContext(on_progress=lambda f, m: progress.append(m))

    result = adapter.backup(db, dest, {}, ctx)
    assert dest.is_file() and dest.stat().st_size > 0
    assert result.backup_format == label and result.engine_version
    assert adapter.validate_backup(dest).ok

    restore_opts = {"server_file_path": result.extra.get("server_file_path")}
    adapter.restore(dest, scratch_db_name, restore_opts, ctx)
    assert adapter.count_rows(scratch_db_name, _schema(target), "orders") == 1000
    assert _schema_signature(adapter.inspect_schema(scratch_db_name)) == _schema_signature(
        adapter.inspect_schema(db)
    )

    # Overwrite protection, then explicit overwrite.
    with pytest.raises(AdapterError, match="already exists"):
        adapter.restore(dest, scratch_db_name, restore_opts)
    adapter.restore(dest, scratch_db_name, {**restore_opts, "overwrite": True})
    assert adapter.count_rows(scratch_db_name, _schema(target), "customers") == 200


def test_corrupt_backup_detected(seeded, tmp_path: Path):
    target, db = seeded
    adapter = target.adapter()
    ext, _ = adapter.backup_format({})
    dest = tmp_path / f"b.{ext}"
    adapter.backup(db, dest, {})
    data = dest.read_bytes()
    truncated = tmp_path / f"t.{ext}"
    truncated.write_bytes(data[: len(data) // 2])
    garbage = tmp_path / f"g.{ext}"
    garbage.write_bytes(b"not a backup" * 10)
    assert not adapter.validate_backup(garbage).ok
    if target.profile.engine.value != "mssql":  # MSSQL needs the server to detect truncation
        assert not adapter.validate_backup(truncated).ok


def test_iter_rows_ordered(seeded):
    target, db = seeded
    adapter = target.adapter()
    rows = list(
        adapter.iter_rows(db, _schema(target), "customers", ["id", "email"], ["id"], batch_size=50)
    )
    assert len(rows) == 200
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def _schema(target) -> str:
    return {"postgresql": "public", "mysql": "", "mssql": "dbo"}[target.profile.engine.value]


def test_mssql_sql_transfer_mode(seeded, scratch_db_name, tmp_path: Path):
    """Without a shared directory the .bak is streamed through OPENROWSET and the
    restore uses the server-side copy after verifying its checksum."""
    target, db = seeded
    if target.profile.engine.value != "mssql":
        pytest.skip("MSSQL only")
    from dataclasses import replace

    from dbnero.adapters.base import create_adapter
    from dbnero.storage.archive import sha256_file

    opts = {k: v for k, v in target.profile.options.items() if k != "local_backup_dir"}
    adapter = create_adapter(replace(target.profile, options=opts), target.password)
    dest = tmp_path / "x.bak"
    result = adapter.backup(db, dest, {})
    assert result.extra["transfer"] == "sql"
    assert adapter.validate_backup(dest).ok
    checksum = sha256_file(dest)
    restore_opts = {
        "server_file_path": result.extra["server_file_path"],
        "checksum_sha256": checksum,
    }
    adapter.restore(dest, scratch_db_name, restore_opts)
    assert adapter.count_rows(scratch_db_name, "dbo", "orders") == 1000

    with pytest.raises(AdapterError, match="does not match"):
        adapter.restore(
            dest, scratch_db_name, {**restore_opts, "checksum_sha256": "0" * 64, "overwrite": True}
        )


def test_overwrite_restore_of_default_database(seeded, tmp_path: Path):
    """Replacing the database the profile connects to by default must work:
    admin statements may not run inside the database being dropped."""
    from dataclasses import replace

    from dbnero.adapters.base import create_adapter

    target, db = seeded
    adapter = target.adapter()
    ext, _ = adapter.backup_format({})
    dest = tmp_path / f"b.{ext}"
    result = adapter.backup(db, dest, {})
    bound = create_adapter(replace(target.profile, default_database=db), target.password)
    bound.restore(
        dest,
        db,
        {"overwrite": True, "server_file_path": result.extra.get("server_file_path")},
    )
    assert db in adapter.list_databases()
    assert adapter.count_rows(db, _schema(target), "orders") == 1000
