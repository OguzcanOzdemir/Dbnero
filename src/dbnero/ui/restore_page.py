"""Restore: pick a backup and a target, review the checks, confirm risky steps."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
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
)
from dbnero.storage.catalog import BackupFilter
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    JobBridge,
    JobProgress,
    Page,
    backup_choice_label,
    button,
    combo,
    error_text,
    fmt_bytes,
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
            "Restore a backup into its database, or into any database you pick or name. "
            "Replacing an existing database asks for confirmation first.",
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
        self.backup = combo()
        self.backup.currentIndexChanged.connect(self._on_backup_changed)
        self.backup_info = muted()
        src.body.addWidget(self.backup)
        src.body.addWidget(self.backup_info)
        left.addWidget(src)

        tgt = Card("Restore to")
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.target_conn = combo()
        self.target_conn.currentIndexChanged.connect(self._on_target_server_changed)
        form.addRow("Server", self.target_conn)
        tgt.body.addLayout(form)

        # One field: pick the database to replace (the backup's own database is
        # preselected) or type a name that does not exist yet to create it.
        self.target_db = combo()
        self.target_db.setEditable(True)
        self.target_db.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if (edit := self.target_db.lineEdit()) is not None:
            edit.setPlaceholderText("Database to restore into")
        self.target_db.currentTextChanged.connect(self._on_name_changed)
        self.name_hint = muted()
        form.addRow("Database", self.target_db)
        form.addRow("", self.name_hint)
        self._target_dbs: list[str] = []
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
        self.confirm = QCheckBox("Yes, replace this database")
        self.confirm.toggled.connect(self._update_buttons)
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
            label = backup_choice_label(rec)
            self.backup.addItem(label, rec.backup_id)
            self.backup.setItemData(self.backup.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
        idx = self.backup.findData(current)
        self.backup.setCurrentIndex(idx if idx >= 0 else 0)
        self.backup.blockSignals(False)
        # Keep the user's server and database choice when the same backup is
        # still selected (the list is refreshed whenever any job finishes).
        if self.backup.currentData() != current or self.target_conn.count() == 0:
            self._on_backup_changed()

    def select_backup(self, backup_id: str) -> None:
        self._reload()
        idx = self.backup.findData(backup_id)
        if idx < 0:
            return
        if idx == self.backup.currentIndex():
            self._on_backup_changed()  # start from the backup's own server again
        else:
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

    def _on_name_changed(self) -> None:
        self._update_name_hint()
        self._invalidate()

    def _update_name_hint(self) -> None:
        name = self.target_db.currentText().strip()
        if not name or self._loading:
            text = ""
        elif name in self._target_dbs:
            text = f"“{name}” exists on this server and will be replaced."
        else:
            text = f"“{name}” does not exist yet and will be created."
        self.name_hint.setText(text)
        self.name_hint.setVisible(bool(text))

    def _on_target_server_changed(self) -> None:
        conn_id = self.target_conn.currentData()
        rec = self._record()
        self._target_dbs = []
        self.target_db.blockSignals(True)
        self.target_db.clear()
        self.target_db.setEditText(rec.database_name if rec else "")
        self.target_db.blockSignals(False)
        self._invalidate()
        if not conn_id:
            return
        self._loading = True
        self._update_name_hint()

        def ok(names: list[str]) -> None:
            if self.target_conn.currentData() != conn_id:
                return
            self._loading = False
            self._target_dbs = names
            wanted = self.target_db.currentText().strip() or (rec.database_name if rec else "")
            self.target_db.blockSignals(True)
            self.target_db.clear()
            self.target_db.addItems(names)
            self.target_db.setEditText(wanted)
            self.target_db.blockSignals(False)
            self._on_name_changed()

        def fail(exc: BaseException) -> None:
            if self.target_conn.currentData() != conn_id:
                return
            self._loading = False
            self.status_banner.show_message(
                f"Could not list databases on this server: {error_text(exc)}", "warning"
            )
            self._update_name_hint()

        run_async(lambda: self.app.connections.list_databases(conn_id), ok, fail)

    def _target_name(self) -> str:
        return self.target_db.currentText().strip()

    def _invalidate(self) -> None:
        self._report = None
        self.checks.clear()
        self.confirm_banner.show_message("")
        self.confirm.setChecked(False)
        self.confirm.setVisible(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        idle = self._job is None
        ready = self._report is not None and self._report.can_proceed and idle
        if ready and self._report is not None and self._report.target_exists:
            ready = self.confirm.isChecked()
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
        overwrite = True  # this page restores into the chosen database
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
                    "Confirm below to continue."
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
        overwrite = report.target_exists
        confirmation = target if report.target_exists and self.confirm.isChecked() else None
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
