"""Catalog-level operations that span the catalog and the archive."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from dbnero.storage.catalog import Catalog, NotFound
from dbnero.storage.manifests import InvalidManifest, read_manifest, scan_manifests

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ImportReport:
    imported: int = 0
    skipped_existing: int = 0
    invalid: list[str] = field(default_factory=list)


class CatalogService:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def import_from_archive(self, root: Path) -> ImportReport:
        """Re-register backups found via manifest files (e.g. after catalog loss)."""
        report = ImportReport()
        for manifest in scan_manifests(root):
            try:
                record = read_manifest(manifest)
            except InvalidManifest as exc:
                log.warning("Skipping invalid manifest: %s", exc)
                report.invalid.append(str(manifest))
                continue
            try:
                self.catalog.get_backup(record.backup_id)
            except NotFound:
                self.catalog.add_backup(record)
                report.imported += 1
            else:
                report.skipped_existing += 1
        return report
