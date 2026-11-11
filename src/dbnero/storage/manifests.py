"""Portable ``manifest.json`` files written next to every backup.

A manifest holds the same metadata as the catalog row (never any secret), so
the catalog can be rebuilt from an archive if it is lost.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from dbnero import __version__
from dbnero.core.models import BackupMode, BackupRecord, BackupStatus, Engine
from dbnero.storage.archive import MANIFEST_SUFFIX

log = logging.getLogger(__name__)

MANIFEST_FORMAT_VERSION = 1


class InvalidManifest(ValueError):
    pass


def _rel(path: Path | None, base: Path) -> str | None:
    if path is None:
        return None
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


def record_to_manifest(record: BackupRecord) -> dict[str, Any]:
    base = record.file_path.parent
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "app": {"name": "dbnero", "version": __version__},
        "backup_id": record.backup_id,
        "batch_id": record.batch_id,
        "connection_id": record.connection_id,
        "engine": record.engine.value,
        "engine_version": record.engine_version,
        "server_label": record.server_label,
        "database_name": record.database_name,
        "created_at_utc": record.created_at_utc.isoformat(),
        "reference_date": record.reference_date.isoformat() if record.reference_date else None,
        "description": record.description,
        "tags": list(record.tags),
        "backup_format": record.backup_format,
        "backup_mode": record.backup_mode.value,
        # File names are stored relative to the manifest so archives can move.
        "file_name": record.file_path.name,
        "file_size": record.file_size,
        "checksum_sha256": record.checksum_sha256,
        "status": record.status.value,
        "tool_versions": dict(record.tool_versions),
        "schema_snapshot": _rel(record.schema_snapshot_path, base),
        "comparison_snapshot": _rel(record.comparison_snapshot_path, base),
    }


def write_manifest(record: BackupRecord, path: Path) -> None:
    data = record_to_manifest(record)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, path)


def read_manifest(path: Path) -> BackupRecord:
    """Rebuild a ``BackupRecord`` from a manifest, resolving paths next to it."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise InvalidManifest(f"{path}: {exc}") from exc

    version = data.get("format_version")
    if not isinstance(version, int) or version > MANIFEST_FORMAT_VERSION:
        raise InvalidManifest(f"{path}: unsupported manifest version {version!r}")

    base = path.parent
    try:
        ref = data.get("reference_date")
        schema = data.get("schema_snapshot")
        comparison = data.get("comparison_snapshot")
        return BackupRecord(
            backup_id=data["backup_id"],
            batch_id=data.get("batch_id"),
            connection_id=data["connection_id"],
            engine=Engine(data["engine"]),
            engine_version=data.get("engine_version", ""),
            server_label=data["server_label"],
            database_name=data["database_name"],
            created_at_utc=datetime.fromisoformat(data["created_at_utc"]),
            reference_date=datetime.fromisoformat(ref) if ref else None,
            description=data.get("description", ""),
            tags=list(data.get("tags", [])),
            backup_format=data["backup_format"],
            backup_mode=BackupMode(data.get("backup_mode", BackupMode.SINGLE.value)),
            file_path=base / data["file_name"],
            file_size=data.get("file_size"),
            checksum_sha256=data.get("checksum_sha256"),
            status=BackupStatus(data["status"]),
            tool_versions=dict(data.get("tool_versions", {})),
            schema_snapshot_path=base / schema if schema else None,
            comparison_snapshot_path=base / comparison if comparison else None,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidManifest(f"{path}: {exc}") from exc


def scan_manifests(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(root.rglob(f"*{MANIFEST_SUFFIX}"))
