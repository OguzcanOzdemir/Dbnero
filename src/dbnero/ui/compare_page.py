"""Compare page: schema (and optional data) differences between backups and live DBs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
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
from dbnero.ui.common import JobBridge, error_text, fmt_local, page_header, run_async

MODE_BACKUP_LIVE = "backup_live"
MODE_BACKUP_BACKUP = "backup_backup"
MODE_LIVE_LIVE = "live_live"

KIND_TEXT = {
    ChangeKind.ADDED: "Eklendi",
    ChangeKind.REMOVED: "Silindi",
    ChangeKind.CHANGED: "Değişti",
}
METHOD_TEXT = {"keyed": "Anahtar", "fingerprint": "Parmak izi", "count_only": "Satır sayısı"}
CATEGORY_TEXT = {
    "table": "Tablolar",
    "column": "Sütunlar",
    "primary_key": "Birincil anahtarlar",
    "index": "İndeksler",
    "foreign_key": "Yabancı anahtarlar",
    "view": "View'lar",
    "trigger": "Trigger'lar",
    "procedure": "Procedure'lar",
    "function": "Function'lar",
}


@dataclass(slots=True)
class CompareResult:
    schema: SchemaDiff | None
    data: DataDiff | None


class SourcePicker(QGroupBox):
    """Chooses either a backup or a live (connection, database) pair."""

    def __init__(self, title: str, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self.app = app
        form = QFormLayout(self)
        self.backup = QComboBox()
        self.connection = QComboBox()
        self.database = QComboBox()
        self.database.setEditable(True)
        self.connection.currentIndexChanged.connect(self._load_databases)
        self.backup_label = QLabel("Yedek")
        self.conn_label = QLabel("Bağlantı")
        self.db_label = QLabel("Veritabanı")
        form.addRow(self.backup_label, self.backup)
        form.addRow(self.conn_label, self.connection)
        form.addRow(self.db_label, self.database)
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
            snap = "" if rec.schema_snapshot_path else " (şema görüntüsü yok)"
            self.backup.addItem(
                f"{rec.database_name} — {fmt_local(rec.created_at_utc)} — {rec.server_label}{snap}",
                rec.backup_id,
            )
        idx = self.backup.findData(current_backup)
        self.backup.setCurrentIndex(max(idx, 0))
        current_conn = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        for p in self.app.connections.list_profiles():
            self.connection.addItem(f"{p.name} ({p.engine.display_name})", p.id)
        idx = self.connection.findData(current_conn)
        self.connection.setCurrentIndex(max(idx, 0))
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


class ComparePage(QWidget):
    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._result: CompareResult | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Karşılaştır",
                "Şema farkları yedek alınırken kaydedilen anlık görüntülerden hesaplanır. "
                "Veri karşılaştırmasında yedek, geçici bir veritabanına geri yüklenir ve "
                "karşılaştırma sonrası silinir.",
            )
        )

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        for i, (key, label) in enumerate(
            [
                (MODE_BACKUP_LIVE, "Yedek ↔ Canlı veritabanı"),
                (MODE_BACKUP_BACKUP, "Yedek ↔ Yedek"),
                (MODE_LIVE_LIVE, "Canlı ↔ Canlı"),
            ]
        ):
            rb = QRadioButton(label)
            rb.setProperty("mode", key)
            self.mode_group.addButton(rb, i)
            mode_row.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        mode_row.addStretch(1)
        self.mode_group.idToggled.connect(self._on_mode_toggled)
        layout.addLayout(mode_row)

        sides = QHBoxLayout()
        self.left = SourcePicker("Sol (kaynak)", app)
        self.right = SourcePicker("Sağ (hedef)", app)
        sides.addWidget(self.left)
        sides.addWidget(self.right)
        layout.addLayout(sides)

        opts = QHBoxLayout()
        self.include_data = QCheckBox("Veri farklarını da karşılaştır")
        self.count_only = QCheckBox("Yalnızca satır sayıları")
        self.samples = QSpinBox()
        self.samples.setRange(0, 1000)
        self.samples.setValue(20)
        self.scratch = QComboBox()
        self.include_data.toggled.connect(self._apply_mode)
        opts.addWidget(self.include_data)
        opts.addWidget(self.count_only)
        opts.addWidget(QLabel("Örnek satır:"))
        opts.addWidget(self.samples)
        self.scratch_label = QLabel("Geçici geri yükleme sunucusu:")
        opts.addWidget(self.scratch_label)
        opts.addWidget(self.scratch, 1)
        layout.addLayout(opts)

        run = QHBoxLayout()
        self.btn_run = QPushButton("Karşılaştır")
        self.btn_cancel = QPushButton("İptal")
        self.btn_cancel.setEnabled(False)
        self.btn_export = QPushButton("Raporu dışa aktar…")
        self.btn_export.setEnabled(False)
        self.btn_run.clicked.connect(self.run)
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_export.clicked.connect(self.export)
        self.status = QLabel()
        self.status.setWordWrap(True)
        run.addWidget(self.btn_run)
        run.addWidget(self.btn_cancel)
        run.addWidget(self.status, 1)
        run.addWidget(self.btn_export)
        layout.addLayout(run)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        schema_split = QSplitter(Qt.Orientation.Vertical)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Nesne", "Değişim", "Ayrıntı"])
        self.tree.itemSelectionChanged.connect(self._show_item)
        self.item_detail = QPlainTextEdit()
        self.item_detail.setReadOnly(True)
        schema_split.addWidget(self.tree)
        schema_split.addWidget(self.item_detail)
        schema_split.setStretchFactor(0, 3)
        self.tabs.addTab(schema_split, "Şema")
        self.data_table = QTableWidget(0, 9)
        self.data_table.setHorizontalHeaderLabels(
            [
                "Tablo",
                "Yöntem",
                "Sol satır",
                "Sağ satır",
                "Eklenen",
                "Silinen",
                "Değişen",
                "Sonuç",
                "Not",
            ]
        )
        self.data_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.data_table.verticalHeader().setVisible(False)
        self.tabs.addTab(self.data_table, "Veri")
        layout.addWidget(self.tabs, 1)

        bridge.progress.connect(self._on_progress)
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
        self.left.set_live(mode == MODE_LIVE_LIVE)
        self.right.set_live(mode != MODE_BACKUP_BACKUP)
        needs_scratch = self.include_data.isChecked() and mode != MODE_LIVE_LIVE
        self.scratch.setVisible(needs_scratch)
        self.scratch_label.setVisible(needs_scratch)
        self.count_only.setEnabled(self.include_data.isChecked())
        self.samples.setEnabled(self.include_data.isChecked())
        self.left._load_databases()
        self.right._load_databases()

    def reload(self) -> None:
        self.left.reload()
        self.right.reload()
        current = self.scratch.currentData()
        self.scratch.clear()
        self.scratch.addItem("(canlı taraftaki bağlantı)", None)
        for p in self.app.connections.list_profiles():
            self.scratch.addItem(f"{p.name} ({p.engine.display_name})", p.id)
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
        data_opts = DataDiffOptions(
            count_only=self.count_only.isChecked(), max_samples=self.samples.value()
        )
        scratch = self.scratch.currentData()
        left_backup = self.left.backup.currentData()
        right_backup = self.right.backup.currentData()
        left_live = self.left.live_side()
        right_live = self.right.live_side()
        compares = self.app.compares

        if mode == MODE_BACKUP_LIVE and not (left_backup and right_live):
            self.status.setText("Bir yedek ve bir canlı veritabanı seçin.")
            return
        if mode == MODE_BACKUP_BACKUP and not (left_backup and right_backup):
            self.status.setText("İki yedek seçin.")
            return
        if mode == MODE_LIVE_LIVE and not (left_live and right_live):
            self.status.setText("İki canlı veritabanı seçin.")
            return
        if mode == MODE_BACKUP_BACKUP and include_data and not scratch:
            self.status.setText("Yedek ↔ yedek veri karşılaştırması için geçici sunucu seçin.")
            return

        def work(h: JobHandle) -> CompareResult:
            log = lambda level, msg: h.log(msg, level)  # noqa: E731
            schema: SchemaDiff | None = None
            data: DataDiff | None = None
            h.log("Şema karşılaştırılıyor")
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
                raise RuntimeError("Karşılaştırma yapılamadı (şema anlık görüntüsü eksik olabilir)")
            return CompareResult(schema, data)

        self._job = self.app.jobs.submit(JobKind.COMPARE, "Karşılaştırma", work)
        self.bridge.when_done(self._job, self._on_done)
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.status.setText("Karşılaştırılıyor…")

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

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.btn_run.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.progress.setVisible(False)
        if job.status is not JobStatus.SUCCEEDED:
            self.status.setText(
                "İptal edildi." if job.status is JobStatus.CANCELLED else f"✖ {job.error}"
            )
            return
        self.show_result(job.result)

    def show_result(self, result: CompareResult) -> None:
        self._result = result
        self.btn_export.setEnabled(True)
        parts = []
        self.tree.clear()
        if result.schema is not None:
            s = result.schema
            parts.append("Şema aynı" if s.identical else f"{len(s.items)} şema farkı")
            for category, items in s.by_category().items():
                parent = QTreeWidgetItem(
                    [f"{CATEGORY_TEXT.get(category.value, category.value)} ({len(items)})", "", ""]
                )
                self.tree.addTopLevelItem(parent)
                for item in items:
                    child = QTreeWidgetItem([item.object_name, KIND_TEXT[item.kind], item.detail])
                    child.setData(0, Qt.ItemDataRole.UserRole, item)
                    color = {"added": "#1b7f3b", "removed": "#b3261e", "changed": "#a15c00"}[
                        item.kind.value
                    ]
                    child.setForeground(1, QColor(color))
                    parent.addChild(child)
                parent.setExpanded(True)
            self.tree.resizeColumnToContents(0)
        self.data_table.setRowCount(0)
        if result.data is not None:
            d = result.data
            parts.append("veri aynı" if d.identical else "veri farkları var")
            self.data_table.setRowCount(len(d.tables))
            for r, t in enumerate(d.tables):
                verdict = {True: "Aynı", False: "Farklı", None: "?"}[t.identical]
                for c, v in enumerate(
                    [
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
                ):
                    self.data_table.setItem(r, c, QTableWidgetItem(str(v)))
            self.data_table.resizeColumnsToContents()
            if d.skipped:
                parts.append(f"{len(d.skipped)} tablo yalnızca bir tarafta")
        self.status.setText("✔ " + ", ".join(parts))
        self.tabs.setCurrentIndex(0 if result.schema is not None else 1)

    def _show_item(self) -> None:
        items = self.tree.selectedItems()
        diff_item = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        if diff_item is None:
            self.item_detail.clear()
            return
        self.item_detail.setPlainText(
            f"{diff_item.object_name} — {KIND_TEXT[diff_item.kind]} {diff_item.detail}\n\n"
            f"Sol:\n{diff_item.left or '-'}\n\nSağ:\n{diff_item.right or '-'}"
        )

    # -- export ---------------------------------------------------------------

    def export(self) -> None:
        if self._result is None:
            return
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Raporu kaydet",
            f"dbnero-karsilastirma-{utc_now():%Y%m%d-%H%M}.html",
            "HTML (*.html);;JSON (*.json);;CSV (*.csv)",
        )
        if not path_str:
            return
        include_rows = False
        if self._result.data is not None and any(t.samples for t in self._result.data.tables):
            answer = QMessageBox.question(
                self,
                "Hassas veri",
                SENSITIVE_WARNING + "\n\nÖrnek satır verileri rapora eklensin mi?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            include_rows = answer == QMessageBox.StandardButton.Yes
        report = CompareReport(
            "dbnero karşılaştırma raporu", self._result.schema, self._result.data, utc_now()
        )
        path = Path(path_str)
        if not path.suffix:
            path = path.with_suffix(".html")
        try:
            export_report(report, path, include_rows)
        except Exception as exc:
            self.status.setText(f"✖ Rapor yazılamadı: {error_text(exc)}")
            return
        self.status.setText(f"Rapor kaydedildi: {path}")
