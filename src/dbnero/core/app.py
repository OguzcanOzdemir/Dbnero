"""Composition root: builds and owns all services for one application run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dbnero.core.backup_service import BackupService
from dbnero.core.catalog_service import CatalogService
from dbnero.core.compare_service import CompareService
from dbnero.core.connection_service import ConnectionService
from dbnero.core.jobs import JobManager
from dbnero.core.restore_service import RestoreService
from dbnero.core.settings import Settings, save_settings
from dbnero.security.credentials import CredentialStore, SessionFallbackCredentialStore
from dbnero.storage.catalog import Catalog


@dataclass
class AppContext:
    settings: Settings
    catalog: Catalog
    credentials: CredentialStore
    connections: ConnectionService
    backups: BackupService
    restores: RestoreService
    compares: CompareService
    catalog_service: CatalogService
    jobs: JobManager
    settings_path: Path | None = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        catalog: Catalog | None = None,
        credentials: CredentialStore | None = None,
        settings_path: Path | None = None,
    ) -> AppContext:
        catalog = catalog or Catalog(settings.resolved_catalog_path())
        credentials = credentials or SessionFallbackCredentialStore()
        connections = ConnectionService(catalog, credentials, settings)
        backups = BackupService(catalog, connections, settings)
        jobs = JobManager(catalog, max_workers=max(2, settings.max_parallel_jobs))
        jobs.recover_interrupted()
        catalog.mark_interrupted_backups()
        return cls(
            settings=settings,
            catalog=catalog,
            credentials=credentials,
            connections=connections,
            backups=backups,
            restores=RestoreService(catalog, connections, backups),
            compares=CompareService(catalog, connections, backups),
            catalog_service=CatalogService(catalog),
            jobs=jobs,
            settings_path=settings_path,
        )

    def save_settings(self) -> None:
        save_settings(self.settings, self.settings_path)

    def close(self) -> None:
        self.jobs.shutdown()
        self.catalog.close()
