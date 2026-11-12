"""Backups: search the archive, inspect, verify, edit, delete and re-import."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

from PySide6.QtCore import QDate, Qt, QUrl, Signal, SignalInstance
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupRecord, BackupStatus, Engine, JobKind, JobStatus
from dbnero.storage.catalog import BackupFilter
from dbnero.ui.backup_page import parse_tags
from dbnero.ui.common import (
    BACKUP_STATUS_TEXT,
    STATUS_TONE,
    Card,
    JobBridge,
    Page,
    StatusDelegate,
    button,
    fmt_bytes,
    fmt_local,
    muted,
    show_error,
)

COLUMNS = ["Created", "Database", "Server", "Size", "Status", "Description"]
STATUS_COL = 4
SIZE_COL = 3


ENGINE_SHORT = {Engine.POSTGRESQL: "PostgreSQL", Engine.MYSQL: "MySQL", Engine.MSSQL: "SQL Server"}


class EditMetadataDialog(QDialog):
    def __init__(self, record: BackupRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit backup details")
        self.setMinimumWidth(420)
        form = QFormLayout(self)
        self.description = QLineEdit(record.description)
        self.tags = QLineEdit(", ".join(record.tags))
        form.addRow("Created", QLabel(fmt_local(record.created_at_utc, seconds=True)))
        form.addRow("Description", self.description)
        form.addRow("Tags", self.tags)
        form.addRow(muted("The creation time is recorded by dbnero and cannot be changed."))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ArchivePage(Page):
    restore_requested = Signal(str)
    compare_requested = Signal(str)
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__("Backups", "Every backup in your archive.", parent)
        self.app = app
        self.bridge = bridge
        self._records: list[BackupRecord] = []

        self.btn_import = button("Import from folder…", on_click=self.import_archive)
        self.btn_retention = button("Clean up…", on_click=self.apply_retention)
        self.header_actions.addWidget(self.btn_import)
        self.header_actions.addWidget(self.btn_retention)

        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Search database or description")
        self.text.setClearButtonEnabled(True)
        self.text.textChanged.connect(self.reload)
        self.engine = QComboBox()
        self.engine.addItem("All engines", "")
        for e in Engine:
            self.engine.addItem(e.display_name, e.value)
        self.status = QComboBox()
        self.status.addItem("Any status", "")
        for s, label in BACKUP_STATUS_TEXT.items():
            self.status.addItem(label, s.value)
        self.tag = QComboBox()
        self.tag.addItem("Any tag", "")
        self.tag.setMinimumWidth(110)
        self.use_dates = QCheckBox("From")
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setDisplayFormat("yyyy-MM-dd")
            d.setEnabled(False)
            d.dateChanged.connect(self.reload)
            self.use_dates.toggled.connect(d.setEnabled)
        self.use_dates.toggled.connect(self.reload)
        for combo in (self.engine, self.status, self.tag):
            combo.currentIndexChanged.connect(self.reload)
        filters.addWidget(self.text, 1)
        for w in (
            self.engine,
            self.status,
            self.tag,
            self.use_dates,
            self.date_from,
            QLabel("to"),
            self.date_to,
        ):
            filters.addWidget(w)
        self.root.addLayout(filters)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.horizontalHeader().setHighlightSections(False)
        self.table.setItemDelegateForColumn(STATUS_COL, StatusDelegate(self.table))
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.doubleClicked.connect(lambda _: self._emit_selected(self.restore_requested))
        body.addWidget(self.table, 3)

        panel = Card("Details")
        panel.setFixedWidth(360)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        mono.setPointSizeF(max(mono.pointSizeF() - 0.5, 8.5))
        self.details.setFont(mono)
        self.details.setPlaceholderText("Select a backup to see its details.")
        panel.body.addWidget(self.details, 1)
        self.btn_restore = button(
            "Restore…", "primary", lambda: self._emit_selected(self.restore_requested)
        )
        self.btn_compare = button(
            "Compare…", on_click=lambda: self._emit_selected(self.compare_requested)
        )
        self.btn_validate = button("Verify", on_click=self.validate_selected)
        self.btn_folder = button("Show in folder", on_click=self.open_folder)
        self.btn_edit = button("Edit…", on_click=self.edit_selected)
        self.btn_delete = button("Delete…", "danger", self.delete_selected)
        row1 = QHBoxLayout()
        row1.addWidget(self.btn_restore)
        row1.addWidget(self.btn_compare)
        row1.addWidget(self.btn_validate)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_folder)
        row2.addWidget(self.btn_edit)
        row2.addStretch(1)
        row2.addWidget(self.btn_delete)
        panel.body.addLayout(row1)
        panel.body.addLayout(row2)
        body.addWidget(panel, 2)
        self.root.addLayout(body, 1)

        self.count_label = muted(wrap=False)
        self.root.addWidget(self.count_label)
        self.reload()

    # -- data -------------------------------------------------------------

    def current_filter(self) -> BackupFilter:
        engine = self.engine.currentData()
        status = self.status.currentData()
        flt = BackupFilter(
            text=self.text.text().strip() or None,
            engine=Engine(engine) if engine else None,
            status=BackupStatus(status) if status else None,
            tag=self.tag.currentData() or None,
        )
        if self.use_dates.isChecked():
            start = datetime.combine(self.date_from.date().toPython(), time.min)  # type: ignore[arg-type]
            end = datetime.combine(self.date_to.date().toPython(), time.max)  # type: ignore[arg-type]
            flt.created_from = start.astimezone().astimezone(UTC)
            flt.created_to = end.astimezone().astimezone(UTC)
        return flt

    def _refresh_tags(self) -> None:
        current = self.tag.currentData()
        self.tag.blockSignals(True)
        self.tag.clear()
        self.tag.addItem("Any tag", "")
        for tag in self.app.catalog.list_tags():
            self.tag.addItem(tag, tag)
        self.tag.setCurrentIndex(max(self.tag.findData(current), 0))
        self.tag.blockSignals(False)

    def reload(self) -> None:
        self._refresh_tags()
        selected = self.selected()
        self._records = self.app.catalog.list_backups(self.current_filter())
        self.table.setSortingEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(self._records))
        total = 0
        for r, rec in enumerate(self._records):
            total += rec.file_size or 0
            values = [
                fmt_local(rec.created_at_utc),
                rec.database_name,
                f"{rec.server_label} · {ENGINE_SHORT[rec.engine]}",
                fmt_bytes(rec.file_size),
                BACKUP_STATUS_TEXT[rec.status],
                rec.description,
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, rec.backup_id)
                if c == STATUS_COL:
                    item.setData(StatusDelegate.TONE_ROLE, STATUS_TONE[rec.status.value])
                if c == SIZE_COL:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(STATUS_COL, max(self.table.columnWidth(STATUS_COL), 110))
        noun = "backup" if len(self._records) == 1 else "backups"
        self.count_label.setText(f"{len(self._records)} {noun} · {fmt_bytes(total)}")
        if selected is not None:
            self.select_backup(selected.backup_id)
        self._show_details()

    def selected(self) -> BackupRecord | None:
        items = self.table.selectedItems() if hasattr(self, "table") else []
        if not items:
            return None
        backup_id = items[0].data(Qt.ItemDataRole.UserRole)
        return next((r for r in self._records if r.backup_id == backup_id), None)

    def select_backup(self, backup_id: str) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == backup_id:
                self.table.selectRow(r)
                return

    def _update_buttons(self) -> None:
        rec = self.selected()
        usable = rec is not None and rec.status in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}
        for b in (self.btn_validate, self.btn_restore, self.btn_compare):
            b.setEnabled(usable)
        for b in (self.btn_folder, self.btn_edit, self.btn_delete):
            b.setEnabled(rec is not None)

    def _show_details(self) -> None:
        self._update_buttons()
        rec = self.selected()
        if rec is None:
            self.details.clear()
            return
        tools = ", ".join(f"{k} {v}" for k, v in rec.tool_versions.items()) or "–"
        lines = [
            f"{rec.database_name}",
            f"{rec.engine.display_name} {rec.engine_version}".strip(),
            "",
            f"Created     {fmt_local(rec.created_at_utc, seconds=True)} "
            f"({rec.created_at_utc:%Y-%m-%d %H:%M:%S} UTC)",
            f"Reference   {fmt_local(rec.reference_date) if rec.reference_date else '–'}",
            f"Server      {rec.server_label}",
            f"Status      {BACKUP_STATUS_TEXT[rec.status]}",
            f"Size        {fmt_bytes(rec.file_size)}",
            f"Format      {rec.backup_format}",
            f"Tags        {', '.join(rec.tags) or '–'}",
            f"Batch       {'yes' if rec.batch_id else 'no'}",
            f"Tools       {tools}",
            f"Snapshot    {'yes' if rec.schema_snapshot_path else 'no'}",
            "",
            f"SHA-256     {rec.checksum_sha256 or '–'}",
            f"File        {rec.file_path}",
            f"ID          {rec.backup_id}",
        ]
        if rec.extra:
            lines.append("Extra       " + ", ".join(f"{k}={v}" for k, v in rec.extra.items()))
        if rec.error_message:
            lines += ["", f"Problem: {rec.error_message}"]
        self.details.setPlainText("\n".join(lines))

    # -- actions ----------------------------------------------------------

    def _emit_selected(self, signal: SignalInstance) -> None:
        rec = self.selected()
        if rec is not None and rec.status in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}:
            signal.emit(rec.backup_id)

    def validate_selected(self) -> None:
        rec = self.selected()
        if rec is None:
            return

        def work(h: JobHandle) -> object:
            h.log(f"Verifying {rec.file_path.name}")
            result = self.app.backups.validate(rec.backup_id, ctx=h.ctx)
            for c in result.checks:
                h.log(c)
            for e in result.errors:
                h.error(e)
            return result

        job = self.app.jobs.submit(JobKind.VALIDATE, f"Verify {rec.database_name}", work)
        self.btn_validate.setEnabled(False)

        def done(j: Job) -> None:
            self.reload()
            self.select_backup(rec.backup_id)
            if j.status is not JobStatus.SUCCEEDED:
                show_error(self, "Verification", j.error or "Failed")
                return
            res = j.result
            text = "\n".join(["✓ " + c for c in res.checks] + ["✗ " + e for e in res.errors])
            if res.ok:
                QMessageBox.information(self, "Backup verified", text)
            else:
                QMessageBox.warning(self, "Backup failed verification", text)
            self.backups_changed.emit()

        self.bridge.when_done(job, done)

    def open_folder(self) -> None:
        rec = self.selected()
        if rec is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(rec.file_path.parent)))

    def edit_selected(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        dlg = EditMetadataDialog(rec, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.app.backups.update_metadata(
                rec.backup_id,
                dlg.description.text(),
                parse_tags(dlg.tags.text()),
                rec.reference_date,
            )
            self.reload()
            self.select_backup(rec.backup_id)

    def delete_selected(self) -> None:
        rec = self.selected()
        if rec is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Delete backup")
        box.setText(
            f"Delete the backup of {rec.database_name} from {fmt_local(rec.created_at_utc)}?"
        )
        box.setInformativeText(
            "The backup file, its manifest and schema snapshot are removed from disk."
        )
        only_catalog = QCheckBox("Keep the files, only remove it from the list")
        box.setCheckBox(only_catalog)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self.app.backups.delete_backup(rec.backup_id, delete_files=not only_catalog.isChecked())
        except Exception as exc:
            show_error(self, "Could not delete the backup", exc)
        self.reload()
        self.backups_changed.emit()

    def import_archive(self) -> None:
        start = str(self.app.backups.archive_root)
        path = QFileDialog.getExistingDirectory(self, "Folder with dbnero backups", start)
        if not path:
            return
        try:
            report = self.app.catalog_service.import_from_archive(Path(path))
        except Exception as exc:
            show_error(self, "Import failed", exc)
            return
        QMessageBox.information(
            self,
            "Import",
            f"Imported {report.imported} backups. {report.skipped_existing} were already listed, "
            f"{len(report.invalid)} manifests could not be read.",
        )
        self.reload()
        self.backups_changed.emit()

    def apply_retention(self) -> None:
        s = self.app.settings
        if s.retention_keep_last <= 0 and s.retention_max_age_days <= 0:
            QMessageBox.information(
                self,
                "Clean up",
                "No retention rule is set. In Settings, choose how many backups to keep per "
                "database or the maximum age.",
            )
            return
        plan = self.app.backups.retention_plan()
        if not plan:
            QMessageBox.information(self, "Clean up", "Nothing to delete.")
            return
        listing = "\n".join(
            f"• {r.database_name} — {fmt_local(r.created_at_utc)} — {fmt_bytes(r.file_size)}"
            for r in plan[:30]
        )
        more = f"\n… and {len(plan) - 30} more" if len(plan) > 30 else ""
        answer = QMessageBox.warning(
            self,
            "Clean up backups",
            f"{len(plan)} backups will be deleted permanently. The newest good backup of every "
            "database is always kept.\n\n" + listing + more,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.app.backups.apply_retention([r.backup_id for r in plan])
            self.reload()
            self.backups_changed.emit()
