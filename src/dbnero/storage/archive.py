"""Backup archive layout, file naming and atomic writes.

Layout::

    <root>/<engine>/<server>/<database>/<YYYY>/<MM>/
        <YYYY-MM-DD_HHMMSS>_<engine>_<database>_<short_id>.<ext>
        <same stem>.manifest.json
        <same stem>.schema.json

Backups are first written to ``<final name>.partial`` and only renamed to the
final name after the tool succeeded and the checksum was computed, so a
half-written file never looks like a finished backup.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dbnero.core.models import Engine

PARTIAL_SUFFIX = ".partial"
MANIFEST_SUFFIX = ".manifest.json"
SCHEMA_SUFFIX = ".schema.json"

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_MAX_COMPONENT = 80


def sanitize_component(value: str) -> str:
    """Make ``value`` safe as a single path component on every platform."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    cleaned = _UNSAFE_CHARS.sub("_", normalized).strip("._ ")
    cleaned = re.sub(r"_+", "_", cleaned)[:_MAX_COMPONENT].rstrip("._ ")
    if not cleaned:
        # Fall back to a stable hash so distinct non-ASCII names stay distinct.
        cleaned = "x" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


@dataclass(slots=True, frozen=True)
class BackupPaths:
    backup: Path
    manifest: Path
    schema: Path

    @property
    def partial(self) -> Path:
        return self.backup.with_name(self.backup.name + PARTIAL_SUFFIX)


def backup_paths(
    root: Path,
    engine: Engine,
    server_label: str,
    database: str,
    created_at_utc: datetime,
    short_id: str,
    extension: str,
) -> BackupPaths:
    ts = created_at_utc.astimezone(UTC)
    directory = (
        root
        / engine.value
        / sanitize_component(server_label)
        / sanitize_component(database)
        / f"{ts:%Y}"
        / f"{ts:%m}"
    )
    ext = extension.lstrip(".")
    stem = f"{ts:%Y-%m-%d_%H%M%S}_{engine.value}_{sanitize_component(database)}_{short_id}"
    return BackupPaths(
        backup=directory / f"{stem}.{ext}",
        manifest=directory / f"{stem}{MANIFEST_SUFFIX}",
        schema=directory / f"{stem}{SCHEMA_SUFFIX}",
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def finalize(partial: Path, final: Path) -> None:
    """Atomically move a completed partial file to its final name."""
    if final.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {final}")
    os.replace(partial, final)


@contextmanager
def partial_file(final: Path) -> Iterator[Path]:
    """Yield a ``.partial`` path for ``final``; delete it if the block fails.

    The caller is responsible for calling ``finalize`` on success.
    """
    final.parent.mkdir(parents=True, exist_ok=True)
    partial = final.with_name(final.name + PARTIAL_SUFFIX)
    try:
        yield partial
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def find_stale_partials(root: Path) -> list[Path]:
    """Leftover partial files from crashed runs (to be cleaned up or reported)."""
    if not root.exists():
        return []
    return sorted(root.rglob(f"*{PARTIAL_SUFFIX}"))
