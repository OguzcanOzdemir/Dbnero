"""User settings stored as JSON in the platform config directory."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from platformdirs import PlatformDirs

from dbnero import APP_NAME

log = logging.getLogger(__name__)

_dirs = PlatformDirs(APP_NAME, appauthor=False)


def config_dir() -> Path:
    return Path(_dirs.user_config_dir)


def data_dir() -> Path:
    return Path(_dirs.user_data_dir)


def log_dir() -> Path:
    return Path(_dirs.user_log_dir)


@dataclass(slots=True)
class Settings:
    archive_root: str = ""
    catalog_path: str = ""
    # tool name -> absolute path override (e.g. {"pg_dump": "/opt/pg16/bin/pg_dump"})
    tool_paths: dict[str, str] = field(default_factory=dict)
    max_parallel_jobs: int = 2
    first_run_completed: bool = False
    # Refuse to start a backup when the archive volume would drop below this.
    min_free_space_mb: int = 1024
    # Warn on the overview page when the archive volume is fuller than this.
    disk_warning_percent: int = 90
    # Retention policy (0 = disabled). Applied only after explicit confirmation.
    retention_keep_last: int = 0
    retention_max_age_days: int = 0
    # Default backup options per engine, e.g. {"mysql": {"compress": true}}.
    backup_defaults: dict[str, dict[str, Any]] = field(default_factory=dict)

    def resolved_archive_root(self) -> Path:
        return Path(self.archive_root) if self.archive_root else data_dir() / "archive"

    def resolved_catalog_path(self) -> Path:
        return Path(self.catalog_path) if self.catalog_path else data_dir() / "catalog.sqlite3"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Settings:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_settings(path: Path | None = None) -> Settings:
    path = path or settings_path()
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        log.warning("Could not read settings from %s (%s); using defaults", path, exc)
        return Settings()


def save_settings(settings: Settings, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2), "utf-8")
    tmp.replace(path)
