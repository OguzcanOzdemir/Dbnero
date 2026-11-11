"""Restore page: pick a backup and a target, run preflight checks, confirm risks."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupRecord, BackupStatus, JobKind, JobStatus
from dbnero.core.restore_service import (
    PreflightReport,
    RestoreOutcome,
    Severity,
    suggest_target_name,
)
from dbnero.storage.catalog import BackupFilter
from dbnero.ui.common import JobBridge, error_text, fmt_bytes, fmt_local, page_header, run_async

SEVERITY_ICON = {
    Severity.OK: "✔",
    Severity.INFO: "ℹ",
    Severity.WARNING: "⚠",
    Severity.ERROR: "✖",
}


class RestorePage(QWidget):
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._report: PreflightReport | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Geri Yükle",
                "Yedekler yalnızca aynı motora sahip bir sunucuya geri yüklenebilir. Varsayılan "
                "olarak yeni bir veritabanı oluşturulur; mevcut veritabanının üzerine yazmak "
                "açık onay gerektirir.",
            )
        )

        src_box = QGroupBox("1. Yedek")
        sf = QFormLayout(src_box)
        self.backup = QComboBox()
        self.backup.currentIndexChanged.connect(self._on_backup_changed)
        self.backup_info = QLabel()
        self.backup_info.setWordWrap(True)
        sf.addRow("Yedek", self.backup)
        sf.addRow("", self.backup_info)
        layout.addWidget(src_box)

        tgt_box = QGroupBox("2. Hedef")
        tf = QFormLayout(tgt_box)
        self.target_conn = QComboBox()
        self.target_conn.currentIndexChanged.connect(self._invalidate)
        self.target_db = QLineEdit()
        self.target_db.textChanged.connect(self._invalidate)
        self.overwrite = QCheckBox(
            "Hedef veritabanı varsa üzerine yaz (silinip yeniden oluşturulur)"
        )
        self.overwrite.toggled.connect(self._invalidate)
        self.protective = QCheckBox(
            "Üzerine yazmadan önce mevcut veritabanının koruyucu yedeğini al"
        )
        self.protective.setChecked(True)
        self.verify = QCheckBox("Geri yükleme sonrası şemayı yedekle karşılaştırarak doğrula")
        self.verify.setChecked(True)
        tf.addRow("Bağlantı", self.target_conn)
        tf.addRow("Veritabanı adı", self.target_db)
        tf.addRow("", self.overwrite)
        tf.addRow("", self.protective)
        tf.addRow("", self.verify)
        layout.addWidget(tgt_box)

        chk_box = QGroupBox("3. Ön kontroller")
        cl = QVBoxLayout(chk_box)
        self.btn_check = QPushButton("Kontrolleri çalıştır")
        self.btn_check.clicked.connect(self.run_preflight)
        self.checks = QListWidget()
        self.checks.setMinimumHeight(120)
        cl.addWidget(self.btn_check, 0, Qt.AlignmentFlag.AlignLeft)
        cl.addWidget(self.checks)
        self.confirm_label = QLabel()
        self.confirm_label.setWordWrap(True)
        self.confirm_label.setStyleSheet("color: #b3261e; font-weight: 600;")
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText("Onaylamak için hedef veritabanı adını yazın")
        self.confirm.textChanged.connect(self._update_start)
        cl.addWidget(self.confirm_label)
        cl.addWidget(self.confirm)
        layout.addWidget(chk_box)

        run = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.btn_start = QPushButton("Geri yüklemeyi başlat")
        self.btn_cancel = QPushButton("İptal")
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel.clicked.connect(self.cancel)
        run.addWidget(self.status, 1)
        run.addWidget(self.btn_start)
        run.addWidget(self.btn_cancel)
        layout.addLayout(run)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)

        bridge.progress.connect(self._on_progress)
        bridge.logged.connect(self._on_log)
        self.reload()

    # -- data -------------------------------------------------------------

    def reload(self) -> None:
        # Keep the last result message visible across programmatic refreshes.
        message = self.status.text()
        self._reload()
        if message and self._job is None:
            self.status.setText(message)

    def _reload(self) -> None:
        current = self.backup.currentData()
        self.backup.blockSignals(True)
        self.backup.clear()
        for rec in self.app.catalog.list_backups(BackupFilter(limit=500)):
            if rec.status not in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}:
                continue
            label = f"{rec.database_name} — {fmt_local(rec.created_at_utc)} — {rec.server_label}"
            if rec.description:
                label += f" — {rec.description}"
            self.backup.addItem(label, rec.backup_id)
        idx = self.backup.findData(current)
        self.backup.setCurrentIndex(idx if idx >= 0 else 0)
        self.backup.blockSignals(False)
        self._on_backup_changed()

    def select_backup(self, backup_id: str) -> None:
        self._reload()
        idx = self.backup.findData(backup_id)
        if idx >= 0:
            self.backup.setCurrentIndex(idx)

    def _record(self) -> BackupRecord | None:
        backup_id = self.backup.currentData()
        return self.app.catalog.get_backup(backup_id) if backup_id else None

    def _on_backup_changed(self) -> None:
        rec = self._record()
        self.target_conn.blockSignals(True)
        self.target_conn.clear()
        if rec is None:
            self.backup_info.setText("Geri yüklenebilir yedek yok.")
            self.target_conn.blockSignals(False)
            self._invalidate()
            return
        self.backup_info.setText(
            f"{rec.engine.display_name} {rec.engine_version} · {fmt_bytes(rec.file_size)} · "
            f"{rec.backup_format}"
            + (" · ⚠ doğrulanamamış" if rec.status is BackupStatus.UNVERIFIED else "")
        )
        for p in self.app.connections.list_profiles():
            if p.engine is rec.engine:
                self.target_conn.addItem(f"{p.name} ({p.server_label})", p.id)
        idx = self.target_conn.findData(rec.connection_id)
        self.target_conn.setCurrentIndex(idx if idx >= 0 else 0)
        self.target_conn.blockSignals(False)
        self.target_db.setText(suggest_target_name(rec))
        self._invalidate()

    def _invalidate(self) -> None:
        self._report = None
        self.checks.clear()
        self.confirm_label.clear()
        self.confirm.clear()
        self.confirm.setVisible(False)
        self._update_start()

    def _needs_confirmation(self) -> bool:
        return bool(self._report and self._report.target_exists)

    def _update_start(self) -> None:
        ready = self._report is not None and self._report.can_proceed and self._job is None
        if ready and self._needs_confirmation():
            ready = self.confirm.text() == self.target_db.text().strip()
        self.btn_start.setEnabled(ready)
        self.btn_cancel.setEnabled(self._job is not None)
        self.btn_check.setEnabled(self._job is None and self.backup.currentData() is not None)
        if self._report is None and self._job is None:
            self.status.setText("Başlatmadan önce ön kontrolleri çalıştırın.")

    # -- preflight ----------------------------------------------------------

    def run_preflight(self) -> None:
        backup_id = self.backup.currentData()
        conn_id = self.target_conn.currentData()
        target = self.target_db.text().strip()
        if not backup_id or not conn_id:
            self.status.setText("Hedef bağlantı yok: aynı motorda bir bağlantı ekleyin.")
            return
        overwrite = self.overwrite.isChecked()
        self.btn_check.setEnabled(False)
        self.status.setText(
            "Kontroller çalışıyor (SHA-256 doğrulaması büyük dosyalarda sürebilir)…"
        )

        def ok(report: PreflightReport) -> None:
            self._report = report
            self.checks.clear()
            for c in report.checks:
                item = QListWidgetItem(f"{SEVERITY_ICON[c.severity]}  {c.message}")
                if c.severity is Severity.ERROR:
                    item.setForeground(Qt.GlobalColor.red)
                elif c.severity is Severity.WARNING:
                    item.setForeground(Qt.GlobalColor.darkYellow)
                self.checks.addItem(item)
            if report.can_proceed and report.target_exists:
                self.confirm_label.setText(
                    f"DİKKAT: '{report.target_database}' veritabanındaki tüm veriler silinip "
                    "yedektekiyle değiştirilecek. Devam etmek için veritabanı adını yazın."
                )
                self.confirm.setVisible(True)
            self.status.setText(
                "Kontroller tamam."
                if report.can_proceed
                else "Geri yükleme engellendi: hataları giderin."
            )
            self._update_start()

        def fail(exc: BaseException) -> None:
            self.status.setText(f"✖ Kontroller çalıştırılamadı: {error_text(exc)}")
            self._update_start()

        run_async(
            lambda: self.app.restores.preflight(backup_id, conn_id, target, overwrite), ok, fail
        )

    # -- run ------------------------------------------------------------------

    def start(self) -> None:
        report = self._report
        if report is None or not report.can_proceed:
            return
        backup_id = report.backup.backup_id
        conn_id = report.target_connection_id
        target = report.target_database
        overwrite = self.overwrite.isChecked()
        confirmation = self.confirm.text() if report.target_exists else None
        protective = self.protective.isChecked()
        verify = self.verify.isChecked()

        def work(h: JobHandle) -> RestoreOutcome:
            return self.app.restores.restore(
                backup_id,
                conn_id,
                target,
                overwrite,
                confirmation,
                protective,
                verify,
                h.ctx,
                lambda level, msg: h.log(msg, level),
            )

        self.log.clear()
        self._job = self.app.jobs.submit(
            JobKind.RESTORE,
            f"Geri yükleme: {report.backup.database_name} → {target}",
            work,
            conn_id,
        )
        self.bridge.when_done(self._job, self._on_done)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Geri yükleme çalışıyor…")
        self._update_start()

    def cancel(self) -> None:
        if self._job is not None:
            self.app.jobs.cancel(self._job.job_id)

    def _on_progress(self, job: Job, fraction: float | None, message: str) -> None:
        if self._job is None or job.job_id != self._job.job_id:
            return
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(fraction * 100))
        self.progress.setFormat(message[:80] + " %p%")

    def _on_log(self, job: Job, level: str, message: str) -> None:
        if self._job is not None and job.job_id == self._job.job_id:
            prefix = {"WARNING": "⚠ ", "ERROR": "✖ "}.get(level, "")
            self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {prefix}{message}")

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.progress.setVisible(False)
        self._invalidate()
        if job.status is JobStatus.SUCCEEDED:
            outcome: RestoreOutcome = job.result
            text = (
                f"✔ Geri yükleme tamamlandı: {outcome.target_database}. {outcome.verification_note}"
            )
            if outcome.verification and not outcome.verification.identical:
                text += " — ayrıntılar için Karşılaştır ekranını kullanın."
            self.status.setText(text)
        elif job.status is JobStatus.CANCELLED:
            self.status.setText(
                "Geri yükleme iptal edildi; hedef veritabanı eksik kalmış olabilir."
            )
        else:
            self.status.setText(f"✖ Geri yükleme başarısız: {job.error}")
        self.backups_changed.emit()
