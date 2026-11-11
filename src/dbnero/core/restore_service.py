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
            report.add(Severity.ERROR, f"Yedek durumu '{record.status.value}', geri yüklenemez")
        elif record.status is BackupStatus.UNVERIFIED:
            report.add(Severity.WARNING, "Yedek doğrulanamamış durumda")
        if not record.file_path.is_file():
            report.add(Severity.ERROR, f"Yedek dosyası bulunamadı: {record.file_path}")
        elif verify_checksum and record.checksum_sha256:
            if sha256_file(record.file_path) != record.checksum_sha256:
                report.add(Severity.ERROR, "SHA-256 uyuşmuyor: yedek dosyası bozulmuş")
            else:
                report.add(Severity.OK, "Yedek dosyası ve SHA-256 özeti doğru")

        # Target connection and engine.
        try:
            profile = self.connections.get(target_connection_id)
        except LookupError:
            report.add(Severity.ERROR, "Hedef bağlantı bulunamadı")
            return report
        if profile.engine is not record.engine:
            report.add(
                Severity.ERROR,
                f"Motor uyuşmuyor: yedek {record.engine.display_name}, hedef "
                f"{profile.engine.display_name}. Motorlar arası geri yükleme desteklenmez.",
            )
            return report
        if not target_database:
            report.add(Severity.ERROR, "Hedef veritabanı adı boş olamaz")
            return report

        for tool in RESTORE_TOOLS[profile.engine]:
            if find_tool(tool, self.connections.settings.tool_paths.get(tool)) is None:
                report.add(Severity.ERROR, f"Gerekli araç bulunamadı: {tool}")

        adapter = self.connections.adapter_for(profile)
        try:
            report.target_version = adapter.test_connection()
            existing = adapter.list_databases(include_system=True)
            system = set(existing) - set(adapter.list_databases())
        except AdapterError as exc:
            report.add(Severity.ERROR, f"Hedef sunucuya bağlanılamadı: {exc}")
            return report
        report.add(Severity.OK, f"Hedef sunucu erişilebilir ({report.target_version})")

        if target_database in system:
            report.add(Severity.ERROR, "Sistem veritabanına geri yükleme yapılamaz")
        report.target_exists = target_database in existing
        if report.target_exists and not overwrite:
            report.add(
                Severity.ERROR,
                f"'{target_database}' zaten var. Yeni bir ad seçin veya üzerine yazmayı "
                "açıkça etkinleştirin.",
            )
        elif report.target_exists:
            report.add(
                Severity.WARNING,
                f"'{target_database}' SİLİNECEK ve yedekten yeniden oluşturulacak. "
                "Bu işlem geri alınamaz.",
            )
        else:
            report.add(Severity.OK, f"'{target_database}' yeni veritabanı olarak oluşturulacak")

        self._version_checks(record, report)

        if profile.engine is Engine.MSSQL:
            self._mssql_checks(record, profile.options, report)

        report.add(
            Severity.INFO,
            "Geri yükleme bir transaction değildir; yarıda kalırsa hedef veritabanı "
            "eksik kalabilir.",
        )
        return report

    @staticmethod
    def _version_checks(record: BackupRecord, report: PreflightReport) -> None:
        src, dst = _major(record.engine_version), _major(report.target_version)
        if src is None or dst is None:
            report.add(Severity.WARNING, "Sürüm uyumluluğu belirlenemedi")
        elif dst < src:
            severity = Severity.ERROR if record.engine is Engine.MSSQL else Severity.WARNING
            report.add(
                severity,
                f"Yedek daha yeni bir sunucudan ({record.engine_version}) alınmış; hedef "
                f"{report.target_version}. Daha eski sürüme geri yükleme "
                + ("desteklenmez." if severity is Severity.ERROR else "sorun çıkarabilir."),
            )
        else:
            report.add(Severity.OK, "Sunucu sürümü uyumlu")

    @staticmethod
    def _mssql_checks(
        record: BackupRecord, options: dict[str, str], report: PreflightReport
    ) -> None:
        if options.get("local_backup_dir"):
            report.add(Severity.OK, "Yedek, paylaşılan klasör üzerinden sunucuya kopyalanacak")
        elif record.extra.get("server_file_path"):
            report.add(
                Severity.WARNING,
                "Paylaşılan klasör yok; sunucudaki kopya kullanılacak "
                f"({record.extra['server_file_path']}), SHA-256 ile doğrulanacak",
            )
        else:
            report.add(
                Severity.ERROR,
                "SQL Server dosyayı okuyamaz: hedef bağlantıda paylaşılan yedek klasörü "
                "(server_backup_dir + local_backup_dir) tanımlayın",
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
            raise ConfirmationRequired(
                "Var olan veritabanının üzerine yazmak için adını onay olarak yazmalısınız"
            )

        record = report.backup
        outcome = RestoreOutcome(target_database)
        if report.target_exists and protective_backup:
            emit("INFO", f"'{target_database}' için koruyucu yedek alınıyor")
            outcome.protective_backup = self.backups.backup_database(
                BackupRequest(
                    target_connection_id,
                    target_database,
                    description=f"Geri yükleme öncesi koruyucu yedek ({record.short_id})",
                    tags=["pre-restore"],
                ),
                ctx,
                emit,
            )

        adapter = self.connections.adapter_for(target_connection_id)
        emit("INFO", f"Geri yükleme başlıyor: {record.file_path.name} → {target_database}")
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
        emit("INFO", "Geri yükleme tamamlandı")

        if verify:
            snapshot = self.backups.load_snapshot(record)
            if snapshot is None:
                outcome.verification_note = (
                    "Yedeğin şema anlık görüntüsü yok; şema doğrulaması atlandı"
                )
                emit("WARNING", outcome.verification_note)
            else:
                if ctx:
                    ctx.progress(None, "Geri yüklenen şema doğrulanıyor")
                live = adapter.inspect_schema(target_database)
                outcome.verification = compare_schemas(
                    snapshot, live, left_label="yedek", right_label=target_database
                )
                if outcome.verification.identical:
                    outcome.verification_note = "Şema yedekle birebir aynı"
                    emit("INFO", outcome.verification_note)
                else:
                    outcome.verification_note = (
                        f"Şemada {len(outcome.verification.items)} fark bulundu"
                    )
                    emit("WARNING", outcome.verification_note)
        return outcome
