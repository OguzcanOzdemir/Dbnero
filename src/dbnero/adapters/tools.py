"""Discovery of external client tools and their versions."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dbnero.core.models import Engine

ENGINE_TOOLS: dict[Engine, tuple[str, ...]] = {
    Engine.POSTGRESQL: ("pg_dump", "pg_restore"),
    Engine.MYSQL: ("mysqldump", "mysql"),
    Engine.MSSQL: (),  # uses T-SQL over ODBC; driver checked separately
}

_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")


@dataclass(slots=True, frozen=True)
class ToolInfo:
    name: str
    path: Path | None
    version: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


def find_tool(name: str, override: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate a tool, preferring a user-configured path from settings."""
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    found = shutil.which(name)
    return Path(found) if found else None


def parse_version(output: str) -> str | None:
    match = _VERSION_RE.search(output)
    return match.group(1) if match else None


def tool_version(path: Path) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_version(completed.stdout + completed.stderr)


def inspect_tool(name: str, override: str | os.PathLike[str] | None = None) -> ToolInfo:
    path = find_tool(name, override)
    return ToolInfo(name, path, tool_version(path) if path else None)


def list_odbc_drivers() -> list[str]:
    """Return installed ODBC drivers for SQL Server (empty if pyodbc missing)."""
    try:
        import pyodbc  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        return []
    drivers: list[str] = pyodbc.drivers()
    return [d for d in drivers if "SQL Server" in d]


def check_engine_tools(engine: Engine, overrides: dict[str, str] | None = None) -> list[ToolInfo]:
    overrides = overrides or {}
    return [inspect_tool(t, overrides.get(t)) for t in ENGINE_TOOLS[engine]]
