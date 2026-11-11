"""Overview: recent backups, failures, archive usage and warnings."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.ui.common import (
    BACKUP_STATUS_TEXT,
    STATUS_COLORS,
    JobBridge,
    fmt_bytes,
    fmt_local,
    page_header,
)


class StatCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        self.value = QLabel("-")
        self.value.setStyleSheet("font-size: 22px; font-weight: 600;")
        label = QLabel(title)
        label.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.value)
        layout.addWidget(label)

    def set(self, text: str, color: str | None = None) -> None:
        self.value.setText(text)
        style = "font-size: 22px; font-weight: 600;"
        if color:
            style += f" color: {color};"
        self.value.setStyleSheet(style)


class OverviewPage(QWidget):
    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        layout = QVBoxLayout(self)
        layout.addWidget(page_header("Genel Bakış"))

        grid = QGridLayout()
        self.card_total = StatCard("Toplam yedek")
        self.card_failed = StatCard("Son 7 günde başarısız")
        self.card_unverified = StatCard("Doğrulanamayan")
        self.card_size = StatCard("Arşiv boyutu")
        self.card_disk = StatCard("Arşiv diski doluluk")
        self.card_active = StatCard("Çalışan iş")
        for i, card in enumerate(
            [
                self.card_total,
                self.card_failed,
                self.card_unverified,
                self.card_size,
                self.card_disk,
                self.card_active,
            ]
        ):
            grid.addWidget(card, i // 3, i % 3)
        layout.addLayout(grid)

        self.warnings = QLabel()
        self.warnings.setWordWrap(True)
        self.warnings.setStyleSheet("color: #a15c00;")
        warn_row = QHBoxLayout()
        warn_row.addWidget(self.warnings, 1)
        self.btn_cleanup = QPushButton("Yarım dosyaları temizle")
        self.btn_cleanup.clicked.connect(self.cleanup_partials)
        warn_row.addWidget(self.btn_cleanup)
        layout.addLayout(warn_row)

        layout.addWidget(QLabel("<b>Son yedekler</b>"))
        self.recent = QTableWidget(0, 5)
        self.recent.setHorizontalHeaderLabels(["Tarih", "Veritabanı", "Sunucu", "Boyut", "Durum"])
        self._setup_table(self.recent)
        layout.addWidget(self.recent, 2)
        layout.addWidget(QLabel("<b>Son başarısız işler</b>"))
        self.failed = QTableWidget(0, 3)
        self.failed.setHorizontalHeaderLabels(["Tarih", "İş", "Hata"])
        self._setup_table(self.failed)
        layout.addWidget(self.failed, 1)

        bridge.finished.connect(lambda _job: self.reload())
        bridge.started.connect(lambda _job: self._update_active())
        self.reload()

    @staticmethod
    def _setup_table(table: QTableWidget) -> None:
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)

    def _update_active(self) -> None:
        self.card_active.set(str(len(self.app.jobs.active_jobs())))

    def reload(self) -> None:
        stats = self.app.catalog_service.overview(self.app.backups.archive_root)
        self.card_total.set(str(stats.total_backups))
        self.card_failed.set(
            str(stats.failed_last_7_days), "#b3261e" if stats.failed_last_7_days else None
        )
        self.card_unverified.set(str(stats.unverified), "#a15c00" if stats.unverified else None)
        self.card_size.set(fmt_bytes(stats.archive_bytes))
        pct = stats.disk_used_percent
        warn_pct = self.app.settings.disk_warning_percent
        self.card_disk.set(f"%{pct:.0f}", "#b3261e" if pct >= warn_pct else None)
        self._update_active()

        warnings = []
        if pct >= warn_pct:
            warnings.append(
                f"Arşiv diski %{pct:.0f} dolu (boş: {fmt_bytes(stats.disk_free)}). "
                "Saklama politikasını uygulamayı düşünün."
            )
        if stats.stale_partials:
            warnings.append(
                f"Önceki oturumlardan kalmış {len(stats.stale_partials)} yarım yedek dosyası var."
            )
        if not self.app.connections.list_profiles():
            warnings.append("Henüz bağlantı yok. 'Bağlantılar' ekranından ekleyin.")
        self.warnings.setText("\n".join(warnings))
        self.btn_cleanup.setVisible(bool(stats.stale_partials))

        self.recent.setRowCount(len(stats.recent))
        for r, rec in enumerate(stats.recent):
            values = [
                fmt_local(rec.created_at_utc),
                rec.database_name,
                rec.server_label,
                fmt_bytes(rec.file_size),
                BACKUP_STATUS_TEXT[rec.status],
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 4:
                    item.setForeground(STATUS_COLORS.get(rec.status.value, STATUS_COLORS["queued"]))
                self.recent.setItem(r, c, item)
        self.recent.resizeColumnsToContents()
        self.failed.setRowCount(len(stats.recent_failed_jobs))
        for r, job in enumerate(stats.recent_failed_jobs):
            for c, v in enumerate(
                [fmt_local(job.started_at_utc), job.title, job.error_message or ""]
            ):
                item = QTableWidgetItem(v)
                item.setToolTip(v)
                item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.failed.setItem(r, c, item)
        self.failed.resizeColumnsToContents()

    def cleanup_partials(self) -> None:
        if self.app.jobs.active_jobs():
            QMessageBox.information(
                self, "Temizlik", "Çalışan işler varken yarım dosyalar temizlenemez."
            )
            return
        removed = self.app.catalog_service.cleanup_stale_partials(self.app.backups.archive_root)
        QMessageBox.information(self, "Temizlik", f"{removed} yarım dosya silindi.")
        self.reload()
