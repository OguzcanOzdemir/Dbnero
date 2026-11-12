"""Discovery of external client tools and their versions."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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


def bundled_tools_dir() -> Path | None:
    """Folder with client tools shipped inside the packaged app, if any."""
    override = os.environ.get("DBNERO_TOOLS_DIR")
    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        Path(override) if override else None,
        exe_dir / "tools",  # Windows / Linux one-folder build
        exe_dir.parent / "Resources" / "tools",  # macOS .app bundle
        Path(getattr(sys, "_MEIPASS", exe_dir)) / "tools",
    ]
    for c in candidates:
        if c is not None and c.is_dir():
            return c
    return None


def _executable(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def bundled_tool(name: str) -> Path | None:
    root = bundled_tools_dir()
    if root is None:
        return None
    for path in sorted(root.glob(f"*/bin/{_executable(name)}")):
        if path.is_file():
            return path
    return None


def tool_candidates(name: str, override: str | os.PathLike[str] | None = None) -> list[Path]:
    """Every usable copy of a tool, in preference order: the path set in
    Settings, the copy bundled with dbnero, then the one on PATH."""
    found: list[Path] = []
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            found.append(candidate)
    bundled = bundled_tool(name)
    if bundled is not None:
        found.append(bundled)
    on_path = shutil.which(name)
    if on_path:
        found.append(Path(on_path))
    unique: list[Path] = []
    for p in found:
        if p not in unique:
            unique.append(p)
    return unique


def find_tool(name: str, override: str | os.PathLike[str] | None = None) -> Path | None:
    """The preferred copy of a tool (see ``tool_candidates``)."""
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        return None
    candidates = tool_candidates(name)
    return candidates[0] if candidates else None


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
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

        drivers: list[str] = pyodbc.drivers()
    except Exception:  # missing module, missing libodbc, broken driver manager
        return []
    return [d for d in drivers if "SQL Server" in d]


def check_engine_tools(engine: Engine, overrides: dict[str, str] | None = None) -> list[ToolInfo]:
    overrides = overrides or {}
    return [inspect_tool(t, overrides.get(t)) for t in ENGINE_TOOLS[engine]]
