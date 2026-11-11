"""Catalog-level operations that span the catalog and the archive."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dbnero.core.models import BackupRecord, BackupStatus, JobStatus, utc_now
from dbnero.storage.archive import find_stale_partials
from dbnero.storage.catalog import BackupFilter, Catalog, JobRecord, NotFound
from dbnero.storage.manifests import InvalidManifest, read_manifest, scan_manifests

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ImportReport:
    imported: int = 0
    skipped_existing: int = 0
    invalid: list[str] = field(default_factory=list)


@dataclass(slots=True)
class OverviewStats:
    total_backups: int = 0
    succeeded: int = 0
    failed_last_7_days: int = 0
    unverified: int = 0
    archive_bytes: int = 0
    disk_total: int = 0
    disk_free: int = 0
    stale_partials: list[Path] = field(default_factory=list)
    recent: list[BackupRecord] = field(default_factory=list)
    recent_failed_jobs: list[JobRecord] = field(default_factory=list)

    @property
    def disk_used_percent(self) -> float:
        return 100.0 * (1 - self.disk_free / self.disk_total) if self.disk_total else 0.0


class CatalogService:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def overview(self, archive_root: Path, now: datetime | None = None) -> OverviewStats:
        now = now or utc_now()
        backups = self.catalog.list_backups(BackupFilter())
        stats = OverviewStats(total_backups=len(backups), recent=backups[:10])
        week_ago = now - timedelta(days=7)
        for b in backups:
            if b.status is BackupStatus.SUCCEEDED:
                stats.succeeded += 1
                stats.archive_bytes += b.file_size or 0
            elif b.status is BackupStatus.UNVERIFIED:
                stats.unverified += 1
                stats.archive_bytes += b.file_size or 0
            elif b.status is BackupStatus.FAILED and b.created_at_utc >= week_ago:
                stats.failed_last_7_days += 1
        stats.recent_failed_jobs = [
            j for j in self.catalog.list_jobs(limit=100) if j.status is JobStatus.FAILED
        ][:10]
        probe = archive_root if archive_root.exists() else archive_root.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        usage = shutil.disk_usage(probe)
        stats.disk_total, stats.disk_free = usage.total, usage.free
        stats.stale_partials = find_stale_partials(archive_root)
        return stats

    def cleanup_stale_partials(self, archive_root: Path) -> int:
        removed = 0
        for path in find_stale_partials(archive_root):
            path.unlink(missing_ok=True)
            removed += 1
        return removed

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
                pass
            else:
                report.skipped_existing += 1
                continue
            try:
                if record.batch_id:
                    self.catalog.ensure_batch(
                        record.batch_id,
                        record.connection_id,
                        record.created_at_utc,
                        record.database_name,
                    )
                self.catalog.add_backup(record)
            except Exception as exc:
                log.warning("Could not import %s: %s", manifest, exc)
                report.invalid.append(str(manifest))
            else:
                report.imported += 1
        return report
