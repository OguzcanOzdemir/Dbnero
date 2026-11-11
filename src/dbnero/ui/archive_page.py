"""Backup archive: search, filter, validate, edit metadata, delete, re-import."""

from __future__ import annotations

from datetime import UTC, datetime, time

from PySide6.QtCore import QDate, Qt, QUrl, Signal, SignalInstance
from PySide6.QtGui import QDesktopServices
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
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupRecord, BackupStatus, Engine, JobKind, JobStatus
from dbnero.storage.catalog import BackupFilter
from dbnero.ui.backup_page import parse_tags
from dbnero.ui.common import (
    BACKUP_STATUS_TEXT,
    STATUS_COLORS,
    JobBridge,
    fmt_bytes,
    fmt_local,
    page_header,
    show_error,
)

COLUMNS = [
    "Tarih",
    "Motor",
    "Sunucu",
    "Veritabanı",
    "Açıklama",
    "Etiketler",
    "Boyut",
    "Durum",
    "Tür",
]


class EditMetadataDialog(QDialog):
    def __init__(self, record: BackupRecord, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Yedek bilgilerini düzenle")
        form = QFormLayout(self)
        self.description = QLineEdit(record.description)
        self.tags = QLineEdit(", ".join(record.tags))
        created = QLabel(fmt_local(record.created_at_utc, seconds=True) + "  (değiştirilemez)")
        form.addRow("Oluşturma zamanı", created)
        form.addRow("Açıklama", self.description)
        form.addRow("Etiketler", self.tags)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class ArchivePage(QWidget):
    restore_requested = Signal(str)
    compare_requested = Signal(str)
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.bridge = bridge
        self._records: list[BackupRecord] = []

        layout = QVBoxLayout(self)
        layout.addWidget(page_header("Yedek Arşivi", "Tüm yedekler, meta verileri ve durumları."))

        filters = QHBoxLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("Veritabanı veya açıklamada ara")
        self.text.returnPressed.connect(self.reload)
        self.engine = QComboBox()
        self.engine.addItem("Tüm motorlar", None)
        for e in Engine:
            self.engine.addItem(e.display_name, e)
        self.status = QComboBox()
        self.status.addItem("Tüm durumlar", None)
        for s, label in BACKUP_STATUS_TEXT.items():
            self.status.addItem(label, s)
        self.tag = QLineEdit()
        self.tag.setPlaceholderText("Etiket")
        self.tag.setMaximumWidth(120)
        self.tag.returnPressed.connect(self.reload)
        self.use_dates = QCheckBox("Tarih:")
        self.date_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self.date_to = QDateEdit(QDate.currentDate())
        for d in (self.date_from, self.date_to):
            d.setCalendarPopup(True)
            d.setEnabled(False)
            self.use_dates.toggled.connect(d.setEnabled)
        btn_filter = QPushButton("Filtrele")
        btn_filter.clicked.connect(self.reload)
        for w in (
            self.text,
            self.engine,
            self.status,
            self.tag,
            self.use_dates,
            self.date_from,
            QLabel("–"),
            self.date_to,
            btn_filter,
        ):
            filters.addWidget(w)
        filters.setStretch(0, 1)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.doubleClicked.connect(lambda _: self._show_details())
        splitter.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        layout.addWidget(splitter, 1)

        actions = QHBoxLayout()
        self.btn_validate = QPushButton("Doğrula")
        self.btn_restore = QPushButton("Geri yükle…")
        self.btn_compare = QPushButton("Karşılaştır…")
        self.btn_folder = QPushButton("Klasörü aç")
        self.btn_edit = QPushButton("Düzenle…")
        self.btn_delete = QPushButton("Sil…")
        self.btn_import = QPushButton("Arşivden içe aktar…")
        self.btn_retention = QPushButton("Saklama politikası…")
        self.btn_validate.clicked.connect(self.validate_selected)
        self.btn_restore.clicked.connect(lambda: self._emit_selected(self.restore_requested))
        self.btn_compare.clicked.connect(lambda: self._emit_selected(self.compare_requested))
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_import.clicked.connect(self.import_archive)
        self.btn_retention.clicked.connect(self.apply_retention)
        for b in (
            self.btn_validate,
            self.btn_restore,
            self.btn_compare,
            self.btn_folder,
            self.btn_edit,
            self.btn_delete,
        ):
            actions.addWidget(b)
        actions.addStretch(1)
        actions.addWidget(self.btn_import)
        actions.addWidget(self.btn_retention)
        layout.addLayout(actions)
        self.count_label = QLabel()
        layout.addWidget(self.count_label)

        self.reload()

    # -- data -------------------------------------------------------------

    def current_filter(self) -> BackupFilter:
        flt = BackupFilter(
            text=self.text.text().strip() or None,
            engine=Engine(e) if (e := self.engine.currentData()) else None,
            status=BackupStatus(st) if (st := self.status.currentData()) else None,
            tag=self.tag.text().strip() or None,
        )
        if self.use_dates.isChecked():
            start = datetime.combine(self.date_from.date().toPython(), time.min).astimezone()  # type: ignore[arg-type]
            end = datetime.combine(self.date_to.date().toPython(), time.max).astimezone()  # type: ignore[arg-type]
            flt.created_from = start.astimezone(UTC)
            flt.created_to = end.astimezone(UTC)
        return flt

    def reload(self) -> None:
        self._records = self.app.catalog.list_backups(self.current_filter())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._records))
        total = 0
        for r, rec in enumerate(self._records):
            total += rec.file_size or 0
            values = [
                fmt_local(rec.created_at_utc),
                rec.engine.display_name,
                rec.server_label,
                rec.database_name,
                rec.description,
                ", ".join(rec.tags),
                fmt_bytes(rec.file_size),
                BACKUP_STATUS_TEXT[rec.status],
                "Toplu" if rec.batch_id else "Tekil",
            ]
            for c, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, rec.backup_id)
                if c == 0:
                    item.setData(Qt.ItemDataRole.UserRole + 1, rec.created_at_utc.isoformat())
                if c == 7:
                    item.setForeground(STATUS_COLORS.get(rec.status.value, STATUS_COLORS["queued"]))
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        self.table.resizeColumnsToContents()
        self.count_label.setText(f"{len(self._records)} yedek · toplam {fmt_bytes(total)}")
        self._update_buttons()

    def selected(self) -> BackupRecord | None:
        items = self.table.selectedItems()
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
        has = rec is not None
        usable = has and rec.status in {BackupStatus.SUCCEEDED, BackupStatus.UNVERIFIED}  # type: ignore[union-attr]
        for b in (self.btn_validate, self.btn_restore, self.btn_compare):
            b.setEnabled(bool(usable))
        for b in (self.btn_folder, self.btn_edit, self.btn_delete):
            b.setEnabled(has)

    def _show_details(self) -> None:
        self._update_buttons()
        rec = self.selected()
        if rec is None:
            self.details.clear()
            return
        lines = [
            f"Kimlik: {rec.backup_id}",
            f"Toplu iş: {rec.batch_id or '-'}",
            f"Oluşturma (yerel): {fmt_local(rec.created_at_utc, seconds=True)}   "
            f"UTC: {rec.created_at_utc:%Y-%m-%d %H:%M:%S}",
            f"Referans tarihi: {fmt_local(rec.reference_date) if rec.reference_date else '-'}",
            f"Motor: {rec.engine.display_name} {rec.engine_version}",
            f"Sunucu: {rec.server_label}   Veritabanı: {rec.database_name}",
            f"Format: {rec.backup_format}   Boyut: {fmt_bytes(rec.file_size)}",
            f"SHA-256: {rec.checksum_sha256 or '-'}",
            f"Dosya: {rec.file_path}",
            f"Şema anlık görüntüsü: {rec.schema_snapshot_path or 'yok'}",
            f"Araçlar: {', '.join(f'{k} {v}' for k, v in rec.tool_versions.items()) or '-'}",
            f"Durum: {BACKUP_STATUS_TEXT[rec.status]}",
        ]
        if rec.extra:
            lines.append("Ek: " + ", ".join(f"{k}={v}" for k, v in rec.extra.items()))
        if rec.error_message:
            lines.append(f"Hata / uyarı: {rec.error_message}")
        self.details.setPlainText("\n".join(lines))

    # -- actions ----------------------------------------------------------

    def _emit_selected(self, signal: SignalInstance) -> None:
        rec = self.selected()
        if rec is not None:
            signal.emit(rec.backup_id)

    def validate_selected(self) -> None:
        rec = self.selected()
        if rec is None:
            return

        def work(h: JobHandle) -> object:
            h.log(f"Doğrulanıyor: {rec.file_path.name}")
            result = self.app.backups.validate(rec.backup_id)
            for c in result.checks:
                h.log(c)
            for e in result.errors:
                h.error(e)
            return result

        job = self.app.jobs.submit(JobKind.VALIDATE, f"Doğrulama: {rec.database_name}", work)
        self.btn_validate.setEnabled(False)

        def done(j: Job) -> None:
            self.reload()
            self.select_backup(rec.backup_id)
            if j.status is not JobStatus.SUCCEEDED:
                show_error(self, "Doğrulama", j.error or "Başarısız")
                return
            res = j.result
            text = "\n".join(["✔ " + c for c in res.checks] + ["✖ " + e for e in res.errors])
            if res.ok:
                QMessageBox.information(self, "Doğrulama başarılı", text)
            else:
                QMessageBox.warning(self, "Yedek doğrulanamadı", text)
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
        box.setWindowTitle("Yedeği sil")
        box.setText(f"{rec.database_name} ({fmt_local(rec.created_at_utc)}) yedeği silinsin mi?")
        box.setInformativeText(
            "Yedek dosyası, manifest ve şema anlık görüntüsü diskten kalıcı olarak silinir."
        )
        only_catalog = QCheckBox("Dosyaları silme, yalnızca katalogdan kaldır")
        box.setCheckBox(only_catalog)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        try:
            self.app.backups.delete_backup(rec.backup_id, delete_files=not only_catalog.isChecked())
        except Exception as exc:
            show_error(self, "Silinemedi", exc)
        self.reload()
        self.backups_changed.emit()

    def import_archive(self) -> None:
        start = str(self.app.backups.archive_root)
        path = QFileDialog.getExistingDirectory(
            self, "Manifest dosyalarının aranacağı klasör", start
        )
        if not path:
            return
        from pathlib import Path

        report = self.app.catalog_service.import_from_archive(Path(path))
        QMessageBox.information(
            self,
            "İçe aktarma",
            f"{report.imported} yedek içe aktarıldı, {report.skipped_existing} zaten katalogdaydı, "
            f"{len(report.invalid)} geçersiz manifest atlandı.",
        )
        self.reload()
        self.backups_changed.emit()

    def apply_retention(self) -> None:
        s = self.app.settings
        if s.retention_keep_last <= 0 and s.retention_max_age_days <= 0:
            QMessageBox.information(
                self,
                "Saklama politikası",
                "Saklama politikası kapalı. Ayarlar ekranından 'son N yedeği sakla' veya "
                "'en fazla gün' değerini belirleyin.",
            )
            return
        plan = self.app.backups.retention_plan()
        if not plan:
            QMessageBox.information(self, "Saklama politikası", "Silinecek yedek yok.")
            return
        listing = "\n".join(
            f"• {r.database_name} — {fmt_local(r.created_at_utc)} — {fmt_bytes(r.file_size)}"
            for r in plan[:30]
        )
        more = f"\n… ve {len(plan) - 30} yedek daha" if len(plan) > 30 else ""
        answer = QMessageBox.warning(
            self,
            "Saklama politikasını uygula",
            f"{len(plan)} yedek kalıcı olarak silinecek "
            "(her veritabanının en yeni başarılı yedeği korunur):\n\n" + listing + more,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.app.backups.apply_retention([r.backup_id for r in plan])
            self.reload()
            self.backups_changed.emit()
