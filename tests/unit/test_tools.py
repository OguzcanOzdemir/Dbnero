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
