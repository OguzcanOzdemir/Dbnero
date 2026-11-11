"""Adapter unit tests: pure helpers and argv construction with mocked tools."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pytest

from dbnero.adapters import mssql as mssql_mod
from dbnero.adapters import mysql as mysql_mod
from dbnero.adapters import postgresql as pg_mod
from dbnero.adapters.base import AdapterError, create_adapter, registered_engines
from dbnero.core.models import AuthType, ConnectionProfile, Engine, SslMode


def _profile(engine: Engine, **kw: Any) -> ConnectionProfile:
    return ConnectionProfile(
        name="p", engine=engine, host="db.local", port=engine.default_port, username="admin", **kw
    )


def test_registry_has_all_engines():
    assert set(registered_engines()) == set(Engine)
    adapter = create_adapter(_profile(Engine.MYSQL), "pw")
    assert isinstance(adapter, mysql_mod.MySQLAdapter)


# -- PostgreSQL ---------------------------------------------------------------


def test_pg_conninfo_escaping():
    assert pg_mod._dbname_conninfo("crm") == "dbname='crm'"
    assert pg_mod._dbname_conninfo("a'b\\c") == "dbname='a\\'b\\\\c'"


def test_pg_version_warnings():
    assert pg_mod._version_warnings("16.4", "16.2", "pg_dump") == []
    assert pg_mod._version_warnings("17.0", "16.2", "pg_dump") == []
    assert pg_mod._version_warnings("15.1", "16.2", "pg_dump")[0].startswith("ERROR")


def test_pg_backup_never_puts_password_in_argv(monkeypatch, tmp_path):
    calls: list[dict[str, Any]] = []
    adapter = pg_mod.PostgreSQLAdapter(_profile(Engine.POSTGRESQL), "S3cret!")
    monkeypatch.setattr(adapter, "_tool", lambda name: Path(f"/usr/bin/{name}"))
    monkeypatch.setattr(adapter, "test_connection", lambda: "16.2")
    monkeypatch.setattr(pg_mod, "tool_version", lambda p: "16.4")
    monkeypatch.setattr(pg_mod, "run_tool", lambda args, **kw: calls.append({"args": args, **kw}))

    result = adapter.backup("crm", tmp_path / "x.dump", {})
    (call,) = calls
    argv = [str(a) for a in call["args"]]
    assert "--format=custom" in argv and "--dbname=dbname='crm'" in argv
    assert not any("S3cret" in a for a in argv)
    assert call["env"]["PGPASSWORD"] == "S3cret!"
    assert result.tool_versions == {"pg_dump": "16.4"}


def test_pg_backup_refuses_old_pg_dump(monkeypatch, tmp_path):
    adapter = pg_mod.PostgreSQLAdapter(_profile(Engine.POSTGRESQL), None)
    monkeypatch.setattr(adapter, "_tool", lambda name: Path(name))
    monkeypatch.setattr(adapter, "test_connection", lambda: "17.1")
    monkeypatch.setattr(pg_mod, "tool_version", lambda p: "15.0")
    with pytest.raises(AdapterError, match="older than server"):
        adapter.backup("crm", tmp_path / "x.dump", {})


def test_pg_validate_rejects_non_archive(tmp_path):
    f = tmp_path / "x.dump"
    f.write_bytes(b"hello")
    adapter = pg_mod.PostgreSQLAdapter(_profile(Engine.POSTGRESQL), None)
    assert not adapter.validate_backup(f).ok
    assert not adapter.validate_backup(tmp_path / "missing.dump").ok


def test_pg_refuses_dropping_maintenance_db():
    adapter = pg_mod.PostgreSQLAdapter(_profile(Engine.POSTGRESQL), None)
    for name in ("postgres", "template1"):
        with pytest.raises(AdapterError):
            adapter.drop_database(name)


# -- MySQL --------------------------------------------------------------------


def test_mysql_option_file_quoting(monkeypatch):
    adapter = mysql_mod.MySQLAdapter(_profile(Engine.MYSQL, ssl_mode=SslMode.REQUIRE), 'p"a\\ss')
    monkeypatch.setattr(adapter, "_tool_version_line", lambda p: "mysqldump  Ver 8.0.39")
    content = adapter._options_file(Path("mysqldump"))
    assert 'password="p\\"a\\\\ss"' in content
    assert "ssl-mode=REQUIRED" in content
    monkeypatch.setattr(adapter, "_tool_version_line", lambda p: "mysqldump from 11.4-MariaDB")
    assert "ssl-mode" not in adapter._options_file(Path("mysqldump"))


def test_mysql_backup_uses_defaults_file(monkeypatch, tmp_path):
    seen: dict[str, Any] = {}
    adapter = mysql_mod.MySQLAdapter(_profile(Engine.MYSQL), "pw-123")
    monkeypatch.setattr(adapter, "_tool", lambda name: Path(name))
    monkeypatch.setattr(adapter, "_tool_version_line", lambda p: "mysqldump  Ver 8.0.39")
    monkeypatch.setattr(adapter, "test_connection", lambda: "8.0.39")
    monkeypatch.setattr(adapter, "_non_transactional_warnings", lambda db: [])

    def fake_run(args, **kw):
        argv = [str(a) for a in args]
        seen["argv"] = argv
        cnf = Path(argv[1].split("=", 1)[1])
        seen["cnf"] = cnf.read_text()
        kw["stdout_sink"](b"-- dump\n-- Dump completed on 2026\n")

    monkeypatch.setattr(mysql_mod, "run_tool", fake_run)
    dest = tmp_path / "x.sql.gz"
    result = adapter.backup("shop", dest, {"compress": True})

    assert seen["argv"][1].startswith("--defaults-extra-file=")
    assert "--single-transaction" in seen["argv"] and seen["argv"][-1] == "shop"
    assert not any("pw-123" in a for a in seen["argv"])
    assert 'password="pw-123"' in seen["cnf"]
    assert not Path(seen["argv"][1].split("=", 1)[1]).exists()  # temp file removed
    assert gzip.decompress(dest.read_bytes()).startswith(b"-- dump")
    assert result.backup_format == "mysqldump_sql_gz"
    assert adapter.validate_backup(dest).ok


def test_mysql_validate(tmp_path):
    adapter = mysql_mod.MySQLAdapter(_profile(Engine.MYSQL), None)
    good = tmp_path / "a.sql"
    good.write_bytes(b"CREATE TABLE t (id int);\n-- Dump completed on 2026-11-11\n")
    assert adapter.validate_backup(good).ok
    partial = tmp_path / "b.sql"
    partial.write_bytes(b"CREATE TABLE t (id int);\n")
    assert not adapter.validate_backup(partial).ok
    truncated_gz = tmp_path / "c.sql.gz"
    truncated_gz.write_bytes(gzip.compress(good.read_bytes() * 1000)[:200])
    assert not adapter.validate_backup(truncated_gz).ok


def test_mysql_rejects_option_like_names():
    with pytest.raises(AdapterError):
        mysql_mod._check_db_name("--all-databases")


# -- MSSQL --------------------------------------------------------------------


def test_mssql_connection_string(monkeypatch):
    profile = _profile(
        Engine.MSSQL,
        ssl_mode=SslMode.REQUIRE,
        options={"odbc_driver": "ODBC Driver 18 for SQL Server", "trust_server_certificate": "no"},
    )
    cs = mssql_mod.MSSQLAdapter(profile, "p;w}d").connection_string("Sales DB")
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in cs
    assert "SERVER={db.local,1433}" in cs
    assert "PWD={p;w}}d}" in cs
    assert "DATABASE={Sales DB}" in cs
    assert "Encrypt=yes" in cs and "TrustServerCertificate=no" in cs

    win = _profile(Engine.MSSQL, auth_type=AuthType.WINDOWS, options={"odbc_driver": "X"})
    cs = mssql_mod.MSSQLAdapter(win, None).connection_string()
    assert "Trusted_Connection=yes" in cs and "PWD" not in cs


def test_mssql_literals_and_types():
    assert mssql_mod._literal("C:\\b\\o'k.bak") == "N'C:\\b\\o''k.bak'"
    assert mssql_mod._format_type("nvarchar", -1, 0, 0) == "nvarchar(max)"
    assert mssql_mod._format_type("nvarchar", 400, 0, 0) == "nvarchar(200)"
    assert mssql_mod._format_type("decimal", 9, 12, 2) == "decimal(12,2)"
    assert mssql_mod._format_type("int", 4, 10, 0) == "int"


def test_mssql_transfer_mode():
    a = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL, options={"local_backup_dir": "/mnt"}), None)
    assert a._transfer_mode() == "share"
    b = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL), None)
    assert b._transfer_mode() == "sql"
    c = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL, options={"transfer": "ftp"}), None)
    with pytest.raises(AdapterError):
        c._transfer_mode()


def test_mssql_server_path_join(monkeypatch):
    a = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL), None)
    a._platform = "Linux"
    assert a._server_join("/var/opt/mssql/backup", "x.bak") == "/var/opt/mssql/backup/x.bak"
    a._platform = "Windows"
    assert a._server_join("D:\\Backups", "x.bak") == "D:\\Backups\\x.bak"


def test_mssql_validate_header(tmp_path):
    a = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL), None)
    good = tmp_path / "a.bak"
    good.write_bytes(b"TAPE" + b"\0" * 100)
    bad = tmp_path / "b.bak"
    bad.write_bytes(b"nope")
    assert a.validate_backup(good).ok and not a.validate_backup(bad).ok


def test_mssql_restore_without_share_or_server_copy_fails(monkeypatch, tmp_path):
    a = mssql_mod.MSSQLAdapter(_profile(Engine.MSSQL), None)
    monkeypatch.setattr(a, "_all_database_names", lambda: [])
    with pytest.raises(AdapterError, match="shared backup"):
        a.restore(tmp_path / "x.bak", "newdb", {})


def test_mssql_odbc_error_cleanup():
    exc = Exception(
        "28000",
        "[Microsoft][ODBC Driver 18 for SQL Server][SQL Server]"
        + "Login failed for user 'sa'. (18456) (SQLDriverConnect)",
    )
    assert mssql_mod._odbc_error(exc) == "Login failed for user 'sa'. (18456)"
