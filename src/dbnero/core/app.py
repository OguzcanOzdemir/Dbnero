"""Composition root: builds and owns all services for one application run."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dbnero.core.backup_service import BackupService
from dbnero.core.catalog_service import CatalogService
from dbnero.core.compare_service import CompareService
from dbnero.core.connection_service import ConnectionService
from dbnero.core.jobs import JobManager
from dbnero.core.os_scheduler import OsScheduler
from dbnero.core.restore_service import RestoreService
from dbnero.core.schedule_service import ScheduleService, scheduler_lock
from dbnero.core.settings import Settings, config_dir, save_settings
from dbnero.security.credentials import CredentialStore, default_credential_store
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
    schedules: ScheduleService
    os_scheduler: OsScheduler
    settings_path: Path | None = None

    @property
    def scheduler_lock_path(self) -> Path:
        return lock_path(self.catalog)

    @classmethod
    def create(
        cls,
        settings: Settings,
        catalog: Catalog | None = None,
        credentials: CredentialStore | None = None,
        settings_path: Path | None = None,
        recover: bool = True,
        os_scheduler: OsScheduler | None = None,
    ) -> AppContext:
        """``recover`` marks work left unfinished by a closed or crashed session
        as failed; skipped while another dbnero process runs scheduled backups."""
        catalog = catalog or Catalog(settings.resolved_catalog_path())
        credentials = credentials or default_credential_store(config_dir())
        connections = ConnectionService(catalog, credentials, settings)
        backups = BackupService(catalog, connections, settings)
        jobs = JobManager(catalog, max_workers=max(2, settings.max_parallel_jobs))
        if recover:
            with scheduler_lock(lock_path(catalog)) as alone:
                if alone:
                    jobs.recover_interrupted()
                    catalog.mark_interrupted_backups()
                    catalog.mark_interrupted_schedules()
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
            schedules=ScheduleService(catalog, connections, backups),
            os_scheduler=os_scheduler or OsScheduler(),
            settings_path=settings_path,
        )

    def save_settings(self) -> None:
        save_settings(self.settings, self.settings_path)

    def close(self) -> None:
        self.jobs.shutdown()
        self.catalog.close()


def lock_path(catalog: Catalog) -> Path:
    """Shared by every dbnero process that uses the same catalog."""
    if catalog.path is None:  # in-memory catalog (tests): one per process
        return Path(tempfile.gettempdir()) / f"dbnero-scheduler-{os.getpid()}.lock"
    return catalog.path.with_name(catalog.path.name + ".scheduler.lock")
