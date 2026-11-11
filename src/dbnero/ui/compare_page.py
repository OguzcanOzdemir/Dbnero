"""Compare: schema (and optionally data) differences between backups and live DBs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from dbnero.compare.data_diff import DataDiff, DataDiffOptions
from dbnero.compare.report import SENSITIVE_WARNING, CompareReport, export_report
from dbnero.compare.schema_diff import ChangeKind, SchemaDiff
from dbnero.core.app import AppContext
from dbnero.core.compare_service import LiveSide
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupStatus, JobKind, JobStatus, utc_now
from dbnero.storage.catalog import BackupFilter
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    JobBridge,
    JobProgress,
    Page,
    StatusDelegate,
    button,
    error_text,
    fmt_local,
    plural,
    run_async,
)

MODE_BACKUP_LIVE = "backup_live"
MODE_BACKUP_BACKUP = "backup_backup"
MODE_LIVE_LIVE = "live_live"

KIND_TEXT = {
    ChangeKind.ADDED: "Added",
    ChangeKind.REMOVED: "Removed",
    ChangeKind.CHANGED: "Changed",
}
KIND_TONE = {
    ChangeKind.ADDED: "success",
    ChangeKind.REMOVED: "danger",
    ChangeKind.CHANGED: "warning",
}
METHOD_TEXT = {"keyed": "By key", "fingerprint": "Fingerprint", "count_only": "Row count"}
CATEGORY_TEXT = {
    "table": "Tables",
    "column": "Columns",
    "primary_key": "Primary keys",
    "index": "Indexes",
    "foreign_key": "Foreign keys",
    "view": "Views",
    "trigger": "Triggers",
    "procedure": "Procedures",
    "function": "Functions",
}


@dataclass(slots=True)
class CompareResult:
    schema: SchemaDiff | None
    data: DataDiff | None


class SourcePicker(Card):
    """Chooses either a backup or a live (connection, database) pair."""

    def __init__(self, title: str, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.app = app
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.backup = QComboBox()
        self.connection = QComboBox()
        self.database = QComboBox()
        self.database.setEditable(True)
        self.connection.currentIndexChanged.connect(self._load_databases)
        self.backup_label = QLabel("Backup")
        self.conn_label = QLabel("Server")
        self.db_label = QLabel("Database")
        form.addRow(self.backup_label, self.backup)
        form.addRow(self.conn_label, self.connection)
        form.addRow(self.db_label, self.database)
        self.body.addLayout(form)
        self.set_live(False)

    def set_live(self, live: bool) -> None:
        for w in (self.backup, self.backup_label):
            w.setVisible(not live)
        for w in (self.connection, self.conn_label, self.database, self.db_label):
            w.setVisible(live)

    def reload(self) -> None:
        current_backup = self.backup.currentData()
        self.backup.clear()
        for rec in self.app.catalog.list_backups(BackupFilter(limit=500)):
            if rec.status not in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}:
                continue
            snap = "" if rec.schema_snapshot_path else " (no schema snapshot)"
            self.backup.addItem(
                f"{rec.database_name} · {fmt_local(rec.created_at_utc)} · {rec.server_label}{snap}",
                rec.backup_id,
            )
        self.backup.setCurrentIndex(max(self.backup.findData(current_backup), 0))
        current_conn = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        for p in self.app.connections.list_profiles():
            self.connection.addItem(f"{p.name} · {p.engine.display_name}", p.id)
        self.connection.setCurrentIndex(max(self.connection.findData(current_conn), 0))
        self.connection.blockSignals(False)
        self._load_databases()

    def _load_databases(self) -> None:
        conn_id = self.connection.currentData()
        if not conn_id or self.connection.isHidden():
            return
        current = self.database.currentText()

        def ok(names: list[str]) -> None:
            if self.connection.currentData() != conn_id:
                return
            self.database.clear()
            self.database.addItems(names)
            if current in names:
                self.database.setCurrentText(current)

        run_async(lambda: self.app.connections.list_databases(conn_id), ok, lambda e: None)

    def live_side(self) -> LiveSide | None:
        conn_id = self.connection.currentData()
        db = self.database.currentText().strip()
        return LiveSide(conn_id, db) if conn_id and db else None

    def preselect_live(self, connection_id: str, database: str) -> None:
        idx = self.connection.findData(connection_id)
        if idx >= 0:
            self.connection.setCurrentIndex(idx)
        self.database.setCurrentText(database)


class ComparePage(Page):
    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(
            "Compare",
            "Find schema and data differences between a backup and a live database, two "
            "backups, or two live databases.",
            parent,
        )
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._result: CompareResult | None = None
        self.btn_export = button("Export report…", on_click=self.export)
        self.btn_export.setEnabled(False)
        self.header_actions.addWidget(self.btn_export)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        self.mode_group = QButtonGroup(self)
        for i, (key, label) in enumerate(
            [
                (MODE_BACKUP_LIVE, "Backup ↔ live database"),
                (MODE_BACKUP_BACKUP, "Backup ↔ backup"),
                (MODE_LIVE_LIVE, "Live ↔ live"),
            ]
        ):
            seg = QPushButton(label)
            seg.setCheckable(True)
            seg.setProperty("mode", key)
            seg.setCursor(Qt.CursorShape.PointingHandCursor)
            self.mode_group.addButton(seg, i)
            mode_row.addWidget(seg)
            if i == 0:
                seg.setChecked(True)
        mode_row.addStretch(1)
        self.mode_group.idToggled.connect(self._on_mode_toggled)
        self.root.addLayout(mode_row)

        sides = QHBoxLayout()
        sides.setSpacing(16)
        self.left = SourcePicker("From", app)
        self.right = SourcePicker("To", app)
        sides.addWidget(self.left)
        sides.addWidget(self.right)
        self.root.addLayout(sides)

        run_row = QHBoxLayout()
        self.include_data = QCheckBox("Also compare data")
        self.include_data.toggled.connect(self._apply_mode)
        self.btn_run = button("Compare", "primary", self.run)
        run_row.addWidget(self.include_data)
        run_row.addStretch(1)
        run_row.addWidget(self.btn_run)
        self.root.addLayout(run_row)

        self.data_options = Collapsible("Data comparison options")
        dopt = QFormLayout()
        dopt.setHorizontalSpacing(16)
        self.count_only = QCheckBox("Only compare row counts")
        self.samples = QSpinBox()
        self.samples.setRange(0, 1000)
        self.samples.setValue(20)
        self.tables = QLineEdit()
        self.tables.setPlaceholderText("All tables, or e.g. public.orders, public.customers")
        self.scratch = QComboBox()
        self.scratch_label = QLabel("Restore backups on")
        dopt.addRow("", self.count_only)
        dopt.addRow("Sample rows per table", self.samples)
        dopt.addRow("Tables", self.tables)
        dopt.addRow(self.scratch_label, self.scratch)
        self.data_options.content_layout.addLayout(dopt)
        self.root.addWidget(self.data_options)

        self.status_banner = Banner()
        self.status = self.status_banner.label
        self.root.addWidget(self.status_banner)
        self.progress = JobProgress(bridge)
        self.progress.log.setVisible(False)
        self.root.addWidget(self.progress)

        self.tabs = QTabWidget()
        schema_split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Object", "Change", "Detail"])
        self.tree.setItemDelegateForColumn(1, StatusDelegate(self.tree))
        self.tree.itemSelectionChanged.connect(self._show_item)
        self.item_detail = QPlainTextEdit()
        self.item_detail.setReadOnly(True)
        self.item_detail.setPlaceholderText("Select a difference to see both versions.")
        schema_split.addWidget(self.tree)
        schema_split.addWidget(self.item_detail)
        schema_split.setStretchFactor(0, 3)
        schema_split.setStretchFactor(1, 2)
        self.tabs.addTab(schema_split, "Schema")
        self.data_table = QTableWidget(0, 9)
        self.data_table.setHorizontalHeaderLabels(
            [
                "Table",
                "Method",
                "From rows",
                "To rows",
                "Added",
                "Removed",
                "Changed",
                "Result",
                "Note",
            ]
        )
        self.data_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.data_table.setShowGrid(False)
        self.data_table.verticalHeader().setVisible(False)
        self.data_table.verticalHeader().setDefaultSectionSize(36)
        self.data_table.horizontalHeader().setStretchLastSection(True)
        self.data_table.setItemDelegateForColumn(7, StatusDelegate(self.data_table))
        self.tabs.addTab(self.data_table, "Data")
        self.root.addWidget(self.tabs, 1)

        self.reload()

    # -- setup --------------------------------------------------------------

    def _on_mode_toggled(self, _id: int, checked: bool) -> None:
        if checked:
            self._apply_mode()

    def mode(self) -> str:
        btn = self.mode_group.checkedButton()
        return str(btn.property("mode")) if btn else MODE_BACKUP_LIVE

    def _apply_mode(self) -> None:
        mode = self.mode()
        for b in self.mode_group.buttons():
            b.setProperty("kind", "primary" if b.isChecked() else "")
            b.style().unpolish(b)
            b.style().polish(b)
        self.left.set_live(mode == MODE_LIVE_LIVE)
        self.right.set_live(mode != MODE_BACKUP_BACKUP)
        data = self.include_data.isChecked()
        self.data_options.setVisible(data)
        needs_scratch = data and mode != MODE_LIVE_LIVE
        self.scratch.setVisible(needs_scratch)
        self.scratch_label.setVisible(needs_scratch)
        self.left._load_databases()
        self.right._load_databases()

    def reload(self) -> None:
        self.left.reload()
        self.right.reload()
        current = self.scratch.currentData()
        self.scratch.clear()
        self.scratch.addItem("The live database's server", None)
        for p in self.app.connections.list_profiles():
            self.scratch.addItem(f"{p.name} · {p.engine.display_name}", p.id)
        self.scratch.setCurrentIndex(max(self.scratch.findData(current), 0))
        self._apply_mode()

    def select_backup(self, backup_id: str) -> None:
        self.reload()
        self.mode_group.button(0).setChecked(True)
        idx = self.left.backup.findData(backup_id)
        if idx >= 0:
            self.left.backup.setCurrentIndex(idx)
            rec = self.app.catalog.get_backup(backup_id)
            self.right.preselect_live(rec.connection_id, rec.database_name)

    # -- run ------------------------------------------------------------------

    def run(self) -> None:
        mode = self.mode()
        include_data = self.include_data.isChecked()
        wanted = [t.strip() for t in self.tables.text().split(",") if t.strip()]
        data_opts = DataDiffOptions(
            tables=wanted or None,
            count_only=self.count_only.isChecked(),
            max_samples=self.samples.value(),
        )
        scratch = self.scratch.currentData()
        left_backup = self.left.backup.currentData()
        right_backup = self.right.backup.currentData()
        left_live = self.left.live_side()
        right_live = self.right.live_side()
        compares = self.app.compares

        problem = None
        if mode == MODE_BACKUP_LIVE and not (left_backup and right_live):
            problem = "Choose a backup and a live database."
        elif mode == MODE_BACKUP_BACKUP and not (left_backup and right_backup):
            problem = "Choose two backups."
        elif mode == MODE_LIVE_LIVE and not (left_live and right_live):
            problem = "Choose two live databases."
        elif mode == MODE_BACKUP_BACKUP and include_data and not scratch:
            problem = "Choose a server to restore the backups on for the data comparison."
        if problem:
            self.status_banner.show_message(problem, "warning")
            return

        def work(h: JobHandle) -> CompareResult:
            def log(level: str, msg: str) -> None:
                h.log(msg, level)

            schema: SchemaDiff | None = None
            data: DataDiff | None = None
            h.log("Comparing schema")
            if mode == MODE_BACKUP_LIVE:
                assert left_backup and right_live  # noqa: S101
                try:
                    schema = compares.schema_backup_vs_live(left_backup, right_live)
                except Exception as exc:
                    h.warning(str(exc))
                if include_data:
                    data = compares.data_backup_vs_live(
                        left_backup, right_live, scratch, data_opts, h.ctx, log
                    )
            elif mode == MODE_BACKUP_BACKUP:
                assert left_backup and right_backup  # noqa: S101
                try:
                    schema = compares.schema_backup_vs_backup(left_backup, right_backup)
                except Exception as exc:
                    h.warning(str(exc))
                if include_data:
                    data = compares.data_backup_vs_backup(
                        left_backup, right_backup, scratch, data_opts, h.ctx, log
                    )
            else:
                assert left_live and right_live  # noqa: S101
                schema = compares.schema_live_vs_live(left_live, right_live)
                if include_data:
                    data = compares.data_live_vs_live(left_live, right_live, data_opts, h.ctx)
            if schema is None and data is None:
                raise RuntimeError(
                    "Nothing could be compared. The backup may have no schema snapshot; "
                    "turn on data comparison instead."
                )
            return CompareResult(schema, data)

        self._job = self.app.jobs.submit(JobKind.COMPARE, "Compare", work)
        self.progress.track(self._job)
        self.bridge.when_done(self._job, self._on_done)
        self.btn_run.setEnabled(False)
        self.status_banner.show_message("")

    def cancel(self) -> None:
        self.progress.cancel()

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.progress.finish()
        self.btn_run.setEnabled(True)
        if job.status is not JobStatus.SUCCEEDED:
            if job.status is JobStatus.CANCELLED:
                self.status_banner.show_message("Comparison cancelled.", "warning")
            else:
                self.status_banner.show_message(f"Comparison failed: {job.error}", "danger")
            return
        self.show_result(job.result)

    def show_result(self, result: CompareResult) -> None:
        self._result = result
        self.btn_export.setEnabled(True)
        parts = []
        self.tree.clear()
        if result.schema is not None:
            s = result.schema
            parts.append(
                "schemas are identical"
                if s.identical
                else plural(len(s.items), "schema difference")
            )
            for category, items in s.by_category().items():
                label = CATEGORY_TEXT.get(category.value, category.value)
                parent = QTreeWidgetItem([f"{label} ({len(items)})", "", ""])
                self.tree.addTopLevelItem(parent)
                for item in items:
                    child = QTreeWidgetItem([item.object_name, KIND_TEXT[item.kind], item.detail])
                    child.setData(0, Qt.ItemDataRole.UserRole, item)
                    child.setData(1, StatusDelegate.TONE_ROLE, KIND_TONE[item.kind])
                    parent.addChild(child)
                parent.setExpanded(True)
            self.tree.resizeColumnToContents(0)
            self.tree.setColumnWidth(0, max(self.tree.columnWidth(0) + 24, 260))
            self.tree.setColumnWidth(1, 110)
        self.data_table.setRowCount(0)
        if result.data is not None:
            d = result.data
            parts.append("data is identical" if d.identical else "data differs")
            self.data_table.setRowCount(len(d.tables))
            for r, t in enumerate(d.tables):
                verdict = {True: "Identical", False: "Different", None: "Unknown"}[t.identical]
                values = [
                    t.table,
                    METHOD_TEXT.get(t.method.value, t.method.value),
                    t.left_rows,
                    t.right_rows,
                    t.added,
                    t.removed,
                    t.changed,
                    verdict,
                    t.note,
                ]
                for c, v in enumerate(values):
                    cell = QTableWidgetItem(str(v))
                    if c == 7:
                        tone = {True: "success", False: "danger", None: "neutral"}[t.identical]
                        cell.setData(StatusDelegate.TONE_ROLE, tone)
                    if 2 <= c <= 6:
                        cell.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                        )
                    self.data_table.setItem(r, c, cell)
            self.data_table.resizeColumnsToContents()
            if d.skipped:
                parts.append(f"{plural(len(d.skipped), 'table')} on one side only")
        identical = (result.schema is None or result.schema.identical) and (
            result.data is None or result.data.identical
        )
        self.status_banner.show_message(
            parts[0][0].upper() + ", ".join(parts)[1:] + ".", "info" if identical else "warning"
        )
        self.tabs.setCurrentIndex(0 if result.schema is not None else 1)

    def _show_item(self) -> None:
        items = self.tree.selectedItems()
        diff_item = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        if diff_item is None:
            self.item_detail.clear()
            return
        self.item_detail.setPlainText(
            f"{diff_item.object_name}\n{KIND_TEXT[diff_item.kind]} {diff_item.detail}\n\n"
            f"From\n{diff_item.left or '–'}\n\nTo\n{diff_item.right or '–'}"
        )

    # -- export ---------------------------------------------------------------

    def export(self) -> None:
        if self._result is None:
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save report",
            f"dbnero-compare-{utc_now():%Y%m%d-%H%M}.html",
            "HTML (*.html);;JSON (*.json);;CSV (*.csv)",
        )
        if not path_str:
            return
        include_rows = False
        if self._result.data is not None and any(t.samples for t in self._result.data.tables):
            answer = QMessageBox.question(
                self,
                "Sensitive data",
                SENSITIVE_WARNING + "\n\nInclude sample rows in the report?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            include_rows = answer == QMessageBox.StandardButton.Yes
        report = CompareReport(
            "dbnero comparison report", self._result.schema, self._result.data, utc_now()
        )
        path = Path(path_str)
        if not path.suffix:
            path = path.with_suffix(".html")
        try:
            export_report(report, path, include_rows)
        except Exception as exc:
            self.status_banner.show_message(
                f"Could not write the report: {error_text(exc)}", "danger"
            )
            return
        self.status_banner.show_message(f"Report saved to {path}", "info")
