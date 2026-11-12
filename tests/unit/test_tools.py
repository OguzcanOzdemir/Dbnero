import sys
from pathlib import Path

from dbnero.adapters.tools import find_tool, inspect_tool, parse_version


def test_parse_version():
    assert parse_version("pg_dump (PostgreSQL) 16.4") == "16.4"
    assert parse_version("mysqldump  Ver 8.0.39 for Linux") == "8.0.39"
    assert parse_version("nothing here") is None


def test_find_tool_override(tmp_path):
    assert find_tool("pg_dump", tmp_path / "nope") is None
    assert find_tool("python", sys.executable) == Path(sys.executable)


def test_inspect_missing():
    info = inspect_tool("definitely-not-a-real-tool-xyz")
    assert not info.available and info.version is None


def test_inspect_python():
    info = inspect_tool("python", sys.executable)
    assert info.available
    assert info.version and info.version.startswith("3.")


def test_list_odbc_drivers_survives_broken_driver_manager(monkeypatch):
    import builtins

    from dbnero.adapters.tools import list_odbc_drivers

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyodbc":
            raise ImportError("dlopen: libodbc.2.dylib not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert list_odbc_drivers() == []


def test_bundled_tools_are_found_before_path(tmp_path, monkeypatch):
    from dbnero.adapters import tools

    exe = "pg_dump.exe" if sys.platform == "win32" else "pg_dump"
    bundled = tmp_path / "postgresql" / "bin" / exe
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\n")
    bundled.chmod(0o755)
    monkeypatch.setenv("DBNERO_TOOLS_DIR", str(tmp_path))
    monkeypatch.setattr(tools.shutil, "which", lambda name: "/usr/bin/" + name)
    assert tools.find_tool("pg_dump") == bundled
    assert tools.tool_candidates("pg_dump") == [bundled, Path("/usr/bin/pg_dump")]
    override = tmp_path / "custom"
    override.write_text("x")
    override.chmod(0o755)
    assert tools.tool_candidates("pg_dump", override)[0] == override
