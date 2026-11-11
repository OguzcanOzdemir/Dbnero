"""Main window: navigation sidebar plus a stacked page area."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from dbnero import APP_NAME, __version__
from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.ui.archive_page import ArchivePage
from dbnero.ui.backup_page import BackupPage
from dbnero.ui.common import JOB_STATUS_TEXT, JobBridge
from dbnero.ui.compare_page import ComparePage
from dbnero.ui.connections_page import ConnectionsPage
from dbnero.ui.jobs_page import JobsPage
from dbnero.ui.overview_page import OverviewPage
from dbnero.ui.restore_page import RestorePage
from dbnero.ui.settings_page import SettingsPage, ToolCheckDialog


@dataclass(frozen=True, slots=True)
class PageSpec:
    key: str
    title: str


PAGES: tuple[PageSpec, ...] = (
    PageSpec("overview", "Genel Bakış"),
    PageSpec("connections", "Bağlantılar"),
    PageSpec("new_backup", "Yeni Yedek"),
    PageSpec("archive", "Yedek Arşivi"),
    PageSpec("restore", "Geri Yükle"),
    PageSpec("compare", "Karşılaştır"),
    PageSpec("jobs", "İş Geçmişi"),
    PageSpec("settings", "Ayarlar"),
)


class MainWindow(QMainWindow):
    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1200, 780)
        self.bridge = JobBridge(app.jobs, self)

        self.overview = OverviewPage(app, self.bridge)
        self.connections = ConnectionsPage(app)
        self.backup = BackupPage(app, self.bridge)
        self.archive = ArchivePage(app, self.bridge)
        self.restore = RestorePage(app, self.bridge)
        self.compare = ComparePage(app, self.bridge)
        self.jobs = JobsPage(app, self.bridge)
        self.settings_page = SettingsPage(app)
        widgets: dict[str, QWidget] = {
            "overview": self.overview,
            "connections": self.connections,
            "new_backup": self.backup,
            "archive": self.archive,
            "restore": self.restore,
            "compare": self.compare,
            "jobs": self.jobs,
            "settings": self.settings_page,
        }

        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        self.nav.setFixedWidth(190)
        self.nav.setStyleSheet("QListWidget#navigation::item { padding: 8px 10px; }")
        self.stack = QStackedWidget()
        self._page_index: dict[str, int] = {}
        for spec in PAGES:
            item = QListWidgetItem(spec.title)
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.nav.addItem(item)
            self._page_index[spec.key] = self.stack.addWidget(widgets[spec.key])
        self.nav.currentRowChanged.connect(self._on_nav)
        self.nav.setCurrentRow(0)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.job_status = QLabel()
        self.statusBar().addPermanentWidget(self.job_status)
        self.statusBar().showMessage("Hazır")

        # Cross-page wiring.
        refreshers: list[Callable[[], None]] = [
            self.backup.reload_connections,
            self.restore.reload,
            self.compare.reload,
            self.overview.reload,
        ]
        for r in refreshers[:3]:
            self.connections.connections_changed.connect(r)
        for page in (self.backup, self.archive, self.restore):
            page.backups_changed.connect(self._on_backups_changed)
        self.archive.restore_requested.connect(self._open_restore)
        self.archive.compare_requested.connect(self._open_compare)
        self.bridge.started.connect(self._update_job_status)
        self.bridge.finished.connect(self._on_job_finished)

    def _on_nav(self, row: int) -> None:
        self.stack.setCurrentIndex(row)
        key = PAGES[row].key
        if key == "overview":
            self.overview.reload()
        elif key == "archive":
            self.archive.reload()
        elif key == "jobs":
            self.jobs.reload()

    def _on_backups_changed(self) -> None:
        self.archive.reload()
        self.restore.reload()
        self.compare.reload()
        self.overview.reload()

    def _open_restore(self, backup_id: str) -> None:
        self.restore.select_backup(backup_id)
        self.show_page("restore")

    def _open_compare(self, backup_id: str) -> None:
        self.compare.select_backup(backup_id)
        self.show_page("compare")

    def _update_job_status(self, _job: Job | None = None) -> None:
        active = self.app.jobs.active_jobs()
        self.job_status.setText(f"{len(active)} iş çalışıyor" if active else "")

    def _on_job_finished(self, job: Job) -> None:
        self._update_job_status()
        self.statusBar().showMessage(f"{job.title}: {JOB_STATUS_TEXT[job.status]}", 8000)

    def show_page(self, key: str) -> None:
        self.nav.setCurrentRow(self._page_index[key])

    def current_page_key(self) -> str:
        return PAGES[self.stack.currentIndex()].key

    def maybe_show_first_run(self) -> None:
        if not self.app.settings.first_run_completed:
            ToolCheckDialog(self.app, self).exec()
            self.app.settings.first_run_completed = True
            self.app.save_settings()

    def closeEvent(self, event: QCloseEvent) -> None:
        active = self.app.jobs.active_jobs()
        if active:
            answer = QMessageBox.question(
                self,
                "Çalışan işler",
                f"{len(active)} iş hâlâ çalışıyor. Çıkarsanız iptal edilecekler. "
                "Yarıda kalan geri yüklemeler hedef veritabanını eksik bırakabilir. Çıkılsın mı?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.bridge.detach()
        event.accept()
