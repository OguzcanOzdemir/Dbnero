"""Restore: pick a backup and a target, review the checks, confirm risky steps."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
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
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    JobBridge,
    JobProgress,
    Page,
    button,
    error_text,
    fmt_bytes,
    fmt_local,
    muted,
    run_async,
    tone_colors,
)

SEVERITY_MARK = {
    Severity.OK: ("✓", "success"),
    Severity.INFO: ("i", "neutral"),
    Severity.WARNING: ("!", "warning"),
    Severity.ERROR: ("✗", "danger"),
}


class RestorePage(Page):
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(
            "Restore",
            "Restores go into a new database by default. Replacing an existing one needs "
            "your explicit confirmation.",
            parent,
        )
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._report: PreflightReport | None = None

        columns = QHBoxLayout()
        columns.setSpacing(16)
        self.root.addLayout(columns, 1)
        left = QVBoxLayout()
        left.setSpacing(12)
        columns.addLayout(left, 1)

        src = Card("Backup")
        self.backup = QComboBox()
        self.backup.currentIndexChanged.connect(self._on_backup_changed)
        self.backup_info = muted()
        src.body.addWidget(self.backup)
        src.body.addWidget(self.backup_info)
        left.addWidget(src)

        tgt = Card("Restore to")
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.target_conn = QComboBox()
        self.target_conn.currentIndexChanged.connect(self._on_target_server_changed)
        form.addRow("Server", self.target_conn)
        tgt.body.addLayout(form)

        # Restore as a new database (default) or replace one that exists.
        self.as_new = QRadioButton("As a new database")
        self.overwrite = QRadioButton("Replace an existing database")
        self.as_new.setChecked(True)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.as_new)
        self.mode_group.addButton(self.overwrite)
        self.target_db = QLineEdit()
        self.target_db.setPlaceholderText("Name of the new database")
        self.target_db.textChanged.connect(self._on_name_changed)
        self.target_db.textEdited.connect(self._on_name_edited)
        self.name_hint = muted()
        self.replace_db = QComboBox()
        self.replace_db.setPlaceholderText("Choose the database to replace")
        self.replace_db.currentIndexChanged.connect(self._invalidate)
        new_row = QFormLayout()
        new_row.setContentsMargins(26, 0, 0, 0)
        new_row.addRow("Name", self.target_db)
        new_row.addRow("", self.name_hint)
        replace_row = QFormLayout()
        replace_row.setContentsMargins(26, 0, 0, 0)
        replace_row.addRow("Database", self.replace_db)
        tgt.body.addWidget(self.as_new)
        tgt.body.addLayout(new_row)
        tgt.body.addWidget(self.overwrite)
        tgt.body.addLayout(replace_row)
        self.as_new.toggled.connect(self._on_mode_changed)
        self._target_dbs: list[str] = []
        self._name_touched = False
        self._loading = False
        self.advanced = Collapsible("Advanced options")
        self.protective = QCheckBox("Back up the existing database before replacing it")
        self.protective.setChecked(True)
        self.verify = QCheckBox("Compare the restored schema with the backup afterwards")
        self.verify.setChecked(True)
        self.advanced.content_layout.addWidget(self.protective)
        self.advanced.content_layout.addWidget(self.verify)
        tgt.body.addWidget(self.advanced)
        left.addWidget(tgt)
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(12)
        columns.addLayout(right, 1)
        checks = Card("Checks")
        self.checks = QListWidget()
        self.checks.setMinimumHeight(150)
        self.checks.setWordWrap(True)
        self.checks.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        checks.body.addWidget(self.checks, 1)
        self.confirm_banner = Banner("danger")
        checks.body.addWidget(self.confirm_banner)
        self.confirm = QLineEdit()
        self.confirm.setPlaceholderText("Type the database name to confirm")
        self.confirm.textChanged.connect(self._update_buttons)
        checks.body.addWidget(self.confirm)
        row = QHBoxLayout()
        self.btn_check = button("Run checks", on_click=self.run_preflight)
        self.btn_start = button("Restore", "primary", self.start)
        row.addWidget(self.btn_check)
        row.addStretch(1)
        row.addWidget(self.btn_start)
        checks.body.addLayout(row)
        right.addWidget(checks)

        self.status_banner = Banner()
        self.status = self.status_banner.label
        right.addWidget(self.status_banner)
        self.progress = JobProgress(bridge)
        self.log = self.progress.log
        right.addWidget(self.progress, 1)

        self.reload()

    # -- data -------------------------------------------------------------

    def reload(self) -> None:
        message = self.status.text() if self.status_banner.isVisibleTo(self) else ""
        self._reload()
        if message and self._job is None:
            self.status_banner.show_message(message)

    def _reload(self) -> None:
        current = self.backup.currentData()
        self.backup.blockSignals(True)
        self.backup.clear()
        for rec in self.app.catalog.list_backups(BackupFilter(limit=500)):
            if rec.status not in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}:
                continue
            label = f"{rec.database_name} · {fmt_local(rec.created_at_utc)} · {rec.server_label}"
            if rec.description:
                label += f" · {rec.description}"
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
            self.backup_info.setText("There are no backups to restore yet.")
            self.target_conn.blockSignals(False)
            self._invalidate()
            return
        info = f"{rec.engine.display_name} {rec.engine_version} · {fmt_bytes(rec.file_size)}"
        if rec.status is BackupStatus.UNVERIFIED:
            info += " · failed its last verification"
        self.backup_info.setText(info)
        for p in self.app.connections.list_profiles():
            if p.engine is rec.engine:
                self.target_conn.addItem(f"{p.name} · {p.server_label}", p.id)
        idx = self.target_conn.findData(rec.connection_id)
        self.target_conn.setCurrentIndex(idx if idx >= 0 else 0)
        self.target_conn.blockSignals(False)
        self._name_touched = False
        self._on_target_server_changed()

    # -- target selection -------------------------------------------------

    def _on_mode_changed(self) -> None:
        new = self.as_new.isChecked()
        self.target_db.setEnabled(new)
        self.replace_db.setEnabled(not new and bool(self._target_dbs))
        self.protective.setEnabled(not new)
        self._invalidate()

    def _on_name_edited(self) -> None:
        self._name_touched = True

    def _on_name_changed(self) -> None:
        self._update_name_hint()
        self._invalidate()

    def _update_name_hint(self) -> None:
        name = self.target_db.text().strip()
        if name and name in self._target_dbs:
            self.name_hint.setText(
                f"“{name}” already exists on this server. Choose another name, or "
                "select “Replace an existing database”."
            )
        else:
            self.name_hint.setText("")
        self.name_hint.setVisible(bool(self.name_hint.text()))

    def _default_new_name(self) -> str:
        rec = self._record()
        if rec is None:
            return ""
        if rec.database_name not in self._target_dbs:
            return rec.database_name
        return suggest_target_name(rec)

    def _on_target_server_changed(self) -> None:
        conn_id = self.target_conn.currentData()
        self._target_dbs = []
        self.replace_db.clear()
        self.replace_db.setEnabled(False)
        self._invalidate()
        if not conn_id:
            return
        self.replace_db.setPlaceholderText("Loading databases…")
        self._loading = True

        def ok(names: list[str]) -> None:
            if self.target_conn.currentData() != conn_id:
                return
            self._loading = False
            self._target_dbs = names
            self.replace_db.clear()
            self.replace_db.addItems(names)
            self.replace_db.setPlaceholderText(
                "Choose the database to replace" if names else "No databases on this server"
            )
            rec = self._record()
            if rec is not None and rec.database_name in names:
                self.replace_db.setCurrentText(rec.database_name)
            else:
                self.replace_db.setCurrentIndex(-1)
            if not self._name_touched:
                self.target_db.blockSignals(True)
                self.target_db.setText(self._default_new_name())
                self.target_db.blockSignals(False)
            self._update_name_hint()
            self._on_mode_changed()

        def fail(exc: BaseException) -> None:
            if self.target_conn.currentData() != conn_id:
                return
            self._loading = False
            self.replace_db.setPlaceholderText("Could not connect to this server")
            self.status_banner.show_message(
                f"Could not list databases on this server: {error_text(exc)}", "warning"
            )
            if not self._name_touched:
                self.target_db.setText(self._default_new_name())

        run_async(lambda: self.app.connections.list_databases(conn_id), ok, fail)

    def _target_name(self) -> str:
        if self.overwrite.isChecked():
            return self.replace_db.currentText().strip()
        return self.target_db.text().strip()

    def _invalidate(self) -> None:
        self._report = None
        self.checks.clear()
        self.confirm_banner.show_message("")
        self.confirm.clear()
        self.confirm.setVisible(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = self._job is None
        ready = self._report is not None and self._report.can_proceed and idle
        if ready and self._report is not None and self._report.target_exists:
            ready = self.confirm.text() == self._report.target_database
        self.btn_start.setEnabled(ready)
        self.btn_check.setEnabled(idle and self.backup.currentData() is not None)

    # -- preflight ----------------------------------------------------------

    def run_preflight(self) -> None:
        backup_id = self.backup.currentData()
        conn_id = self.target_conn.currentData()
        target = self._target_name()
        if not backup_id or not conn_id:
            self.status_banner.show_message(
                "Add a connection for this database engine first.", "warning"
            )
            return
        overwrite = self.overwrite.isChecked()
        self.btn_check.setEnabled(False)
        self.status_banner.show_message("Running checks…", "info")

        def ok(report: PreflightReport) -> None:
            self._report = report
            self.checks.clear()
            for c in report.checks:
                mark, tone = SEVERITY_MARK[c.severity]
                item = QListWidgetItem(f"{mark}   {c.message}")
                if tone != "neutral":
                    fg: QColor = tone_colors(tone)[0]
                    item.setForeground(fg)
                self.checks.addItem(item)
            if report.can_proceed and report.target_exists:
                self.confirm_banner.show_message(
                    f"All data in “{report.target_database}” will be replaced by the backup. "
                    "Type the database name below to continue."
                )
                self.confirm.setVisible(True)
            if report.can_proceed:
                self.status_banner.show_message("")
            else:
                self.status_banner.show_message("Fix the problems above to continue.", "danger")
            self._update_buttons()

        def fail(exc: BaseException) -> None:
            self.status_banner.show_message(f"Checks could not run: {error_text(exc)}", "danger")
            self._update_buttons()

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

        self._job = self.app.jobs.submit(
            JobKind.RESTORE, f"Restore {report.backup.database_name} → {target}", work, conn_id
        )
        self.progress.track(self._job)
        self.bridge.when_done(self._job, self._on_done)
        self.status_banner.show_message("")
        self._update_buttons()

    def cancel(self) -> None:
        self.progress.cancel()

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.progress.finish()
        self._invalidate()
        if job.status is JobStatus.SUCCEEDED:
            outcome: RestoreOutcome = job.result
            tone = "info"
            text = f"Restore finished: {outcome.target_database}. {outcome.verification_note}."
            if outcome.verification and not outcome.verification.identical:
                tone = "warning"
                text += " Open Compare for the details."
            self.status_banner.show_message(text, tone)
        elif job.status is JobStatus.CANCELLED:
            self.status_banner.show_message(
                "Restore cancelled. The target database may be incomplete.", "warning"
            )
        else:
            self.status_banner.show_message(f"Restore failed: {job.error}", "danger")
        self.backups_changed.emit()
