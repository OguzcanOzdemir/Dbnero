"""Restore a catalogued backup into a database of the same engine.

Safety rules (see CLAUDE.md):
* same engine only
* overwriting an existing database is off by default and needs the user to
  type the database name as confirmation
* restores are not transactional; an optional protective backup of the
  existing target is taken first
* the restored database is verified against the backup's schema snapshot
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from dbnero.adapters.base import AdapterError, JobContext, ToolNotFound
from dbnero.adapters.tools import find_tool
from dbnero.compare.schema_diff import SchemaDiff, compare_schemas
from dbnero.core.backup_service import BackupRequest, BackupService, LogFn, _noop_log
from dbnero.core.connection_service import ConnectionService
from dbnero.core.models import BackupRecord, BackupStatus, Engine
from dbnero.storage.archive import sha256_file
from dbnero.storage.catalog import Catalog

RESTORE_TOOLS: dict[Engine, tuple[str, ...]] = {
    Engine.POSTGRESQL: ("pg_restore",),
    Engine.MYSQL: ("mysql",),
    Engine.MSSQL: (),
}


class Severity(StrEnum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class Check:
    severity: Severity
    message: str


@dataclass(slots=True)
class PreflightReport:
    backup: BackupRecord
    target_connection_id: str
    target_database: str
    target_exists: bool = False
    target_version: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def errors(self) -> list[Check]:
        return [c for c in self.checks if c.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.severity is Severity.WARNING]

    @property
    def can_proceed(self) -> bool:
        return not self.errors

    def add(self, severity: Severity, message: str) -> None:
        self.checks.append(Check(severity, message))


class RestoreBlocked(Exception):
    def __init__(self, report: PreflightReport) -> None:
        super().__init__("; ".join(c.message for c in report.errors))
        self.report = report


class ConfirmationRequired(Exception):
    pass


@dataclass(slots=True)
class RestoreOutcome:
    target_database: str
    protective_backup: BackupRecord | None = None
    verification: SchemaDiff | None = None
    verification_note: str = ""

    @property
    def verified(self) -> bool:
        return self.verification is not None and self.verification.identical


def suggest_target_name(record: BackupRecord) -> str:
    stamp = record.created_at_utc.strftime("%Y%m%d_%H%M")
    return f"{record.database_name}_restored_{stamp}"


def _major(version: str) -> int | None:
    m = re.match(r"\s*(\d+)", version)
    return int(m.group(1)) if m else None


class RestoreService:
    def __init__(
        self,
        catalog: Catalog,
        connections: ConnectionService,
        backups: BackupService,
    ) -> None:
        self.catalog = catalog
        self.connections = connections
        self.backups = backups

    def preflight(
        self,
        backup_id: str,
        target_connection_id: str,
        target_database: str,
        overwrite: bool = False,
        verify_checksum: bool = True,
    ) -> PreflightReport:
        record = self.catalog.get_backup(backup_id)
        report = PreflightReport(record, target_connection_id, target_database.strip())
        target_database = report.target_database

        # Backup file.
        if record.status not in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}:
            report.add(
                Severity.ERROR, f"Backup status is '{record.status.value}' and cannot be restored"
            )
        elif record.status is BackupStatus.UNVERIFIED:
            report.add(Severity.WARNING, "The backup failed its last validation")
        if not record.file_path.is_file():
            report.add(Severity.ERROR, f"Backup file not found: {record.file_path}")
        elif verify_checksum and record.checksum_sha256:
            if sha256_file(record.file_path) != record.checksum_sha256:
                report.add(Severity.ERROR, "SHA-256 mismatch: the backup file is corrupted")
            else:
                report.add(Severity.OK, "Backup file present and SHA-256 matches")

        # Target connection and engine.
        try:
            profile = self.connections.get(target_connection_id)
        except LookupError:
            report.add(Severity.ERROR, "Target connection not found")
            return report
        if profile.engine is not record.engine:
            report.add(
                Severity.ERROR,
                f"Engine mismatch: backup is {record.engine.display_name}, target is "
                f"{profile.engine.display_name}. Cross-engine restore is not supported.",
            )
            return report
        if not target_database:
            report.add(Severity.ERROR, "Target database name is required")
            return report

        for tool in RESTORE_TOOLS[profile.engine]:
            if find_tool(tool, self.connections.settings.tool_paths.get(tool)) is None:
                report.add(Severity.ERROR, f"Required tool not found: {tool}")

        adapter = self.connections.adapter_for(profile)
        try:
            report.target_version = adapter.test_connection()
            existing = adapter.list_databases(include_system=True)
            system = set(existing) - set(adapter.list_databases())
        except AdapterError as exc:
            report.add(Severity.ERROR, f"Cannot connect to the target server: {exc}")
            return report
        report.add(Severity.OK, f"Target server reachable ({report.target_version})")

        if target_database in system:
            report.add(Severity.ERROR, "Restoring into a system database is not allowed")
        report.target_exists = target_database in existing
        if report.target_exists and not overwrite:
            report.add(
                Severity.ERROR,
                f"'{target_database}' already exists. Choose a new name or explicitly "
                "allow overwriting.",
            )
        elif report.target_exists:
            report.add(
                Severity.WARNING,
                f"'{target_database}' will be DROPPED and recreated from the backup. "
                "This cannot be undone.",
            )
        else:
            report.add(Severity.OK, f"'{target_database}' will be created as a new database")

        self._version_checks(record, report)

        if profile.engine is Engine.MSSQL:
            self._mssql_checks(record, profile.options, report)

        report.add(
            Severity.INFO,
            "A restore is not a transaction; if it stops midway the target database "
            "may be incomplete.",
        )
        return report

    @staticmethod
    def _version_checks(record: BackupRecord, report: PreflightReport) -> None:
        src, dst = _major(record.engine_version), _major(report.target_version)
        if src is None or dst is None:
            report.add(Severity.WARNING, "Could not determine version compatibility")
        elif dst < src:
            severity = Severity.ERROR if record.engine is Engine.MSSQL else Severity.WARNING
            report.add(
                severity,
                f"The backup comes from a newer server ({record.engine_version}); target is "
                f"{report.target_version}. Restoring to an older version "
                + ("is not supported." if severity is Severity.ERROR else "may fail."),
            )
        else:
            report.add(Severity.OK, "Server version is compatible")

    @staticmethod
    def _mssql_checks(
        record: BackupRecord, options: dict[str, str], report: PreflightReport
    ) -> None:
        if options.get("local_backup_dir"):
            report.add(
                Severity.OK, "The backup will be copied to the server through the shared folder"
            )
        elif record.extra.get("server_file_path"):
            report.add(
                Severity.WARNING,
                "No shared folder; the server-side copy "
                f"({record.extra['server_file_path']}) will be used after SHA-256 verification",
            )
        else:
            report.add(
                Severity.ERROR,
                "SQL Server cannot read the file: configure a shared backup folder "
                "on the target connection",
            )

    def restore(
        self,
        backup_id: str,
        target_connection_id: str,
        target_database: str,
        overwrite: bool = False,
        confirmation: str | None = None,
        protective_backup: bool = True,
        verify: bool = True,
        ctx: JobContext | None = None,
        log_fn: LogFn | None = None,
    ) -> RestoreOutcome:
        emit = log_fn or _noop_log
        target_database = target_database.strip()
        report = self.preflight(backup_id, target_connection_id, target_database, overwrite)
        for check in report.checks:
            emit(
                {"ok": "INFO", "info": "INFO", "warning": "WARNING", "error": "ERROR"}[
                    check.severity.value
                ],
                check.message,
            )
        if not report.can_proceed:
            raise RestoreBlocked(report)
        if report.target_exists and confirmation != target_database:
            raise ConfirmationRequired("Type the database name to confirm overwriting it")

        record = report.backup
        outcome = RestoreOutcome(target_database)
        if report.target_exists and protective_backup:
            emit("INFO", f"Taking a safety backup of '{target_database}'")
            outcome.protective_backup = self.backups.backup_database(
                BackupRequest(
                    target_connection_id,
                    target_database,
                    description=f"Safety backup before restore ({record.short_id})",
                    tags=["pre-restore"],
                ),
                ctx,
                emit,
            )

        adapter = self.connections.adapter_for(target_connection_id)
        emit("INFO", f"Restoring {record.file_path.name} → {target_database}")
        try:
            adapter.restore(
                record.file_path,
                target_database,
                {
                    "overwrite": report.target_exists,
                    "server_file_path": record.extra.get("server_file_path"),
                    "checksum_sha256": record.checksum_sha256,
                },
                ctx,
            )
        except ToolNotFound as exc:
            raise AdapterError(str(exc)) from exc
        emit("INFO", "Restore finished")

        if verify:
            snapshot = self.backups.load_snapshot(record)
            if snapshot is None:
                outcome.verification_note = (
                    "The backup has no schema snapshot; schema verification skipped"
                )
                emit("WARNING", outcome.verification_note)
            else:
                if ctx:
                    ctx.progress(None, "Verifying restored schema")
                live = adapter.inspect_schema(target_database)
                outcome.verification = compare_schemas(
                    snapshot, live, left_label="backup", right_label=target_database
                )
                if outcome.verification.identical:
                    outcome.verification_note = "Schema matches the backup"
                    emit("INFO", outcome.verification_note)
                else:
                    outcome.verification_note = (
                        f"{len(outcome.verification.items)} schema differences found"
                    )
                    emit("WARNING", outcome.verification_note)
        return outcome
