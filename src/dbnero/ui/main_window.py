"""Main window: an ink navigation rail beside a stack of pages."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dbnero import APP_NAME, __version__
from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.ui import icons, theme
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
    icon: str


PAGES: tuple[PageSpec, ...] = (
    PageSpec("overview", "Home", "home"),
    PageSpec("new_backup", "Back up", "backup"),
    PageSpec("archive", "Backups", "archive"),
    PageSpec("restore", "Restore", "restore"),
    PageSpec("compare", "Compare", "compare"),
    PageSpec("connections", "Connections", "connections"),
    PageSpec("jobs", "Activity", "activity"),
    PageSpec("settings", "Settings", "settings"),
)
# A visual gap is drawn before these entries to group the rail.
GROUP_STARTS = {"connections"}


class MainWindow(QMainWindow):
    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(APP_NAME)
        self.resize(1280, 820)
        self.setMinimumSize(1000, 640)
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

        rail = QWidget()
        rail.setObjectName("rail")
        rail.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rail.setFixedWidth(212)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 6, 0, 6)
        rail_layout.setSpacing(0)
        brand = QLabel(APP_NAME)
        brand.setObjectName("brand")
        sub = QLabel("Database backups")
        sub.setObjectName("brandSub")
        rail_layout.addWidget(brand)
        rail_layout.addWidget(sub)

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.stack = QStackedWidget()
        self._page_index: dict[str, int] = {}
        self._row_for_key: dict[str, int] = {}
        self._key_for_row: dict[int, str] = {}
        for spec in PAGES:
            if spec.key in GROUP_STARTS:
                spacer = QListWidgetItem("")
                spacer.setFlags(Qt.ItemFlag.NoItemFlags)
                spacer.setSizeHint(QSize(10, 18))
                self.nav.addItem(spacer)
            item = QListWidgetItem(spec.title)
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.nav.addItem(item)
            row = self.nav.count() - 1
            self._row_for_key[spec.key] = row
            self._key_for_row[row] = spec.key
            self._page_index[spec.key] = self.stack.addWidget(widgets[spec.key])
        self._paint_icons()
        self.nav.currentRowChanged.connect(self._on_nav)
        rail_layout.addWidget(self.nav, 1)
        self.job_status = QLabel()
        self.job_status.setObjectName("railFooter")
        footer = QLabel(f"Version {__version__}")
        footer.setObjectName("railFooter")
        rail_layout.addWidget(self.job_status)
        rail_layout.addWidget(footer)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(rail)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.nav.setCurrentRow(self._row_for_key["overview"])

        # Cross-page wiring.
        for refresh in (self.backup.reload_connections, self.restore.reload, self.compare.reload):
            self.connections.connections_changed.connect(refresh)
        self.connections.connections_changed.connect(self.overview.reload)
        for page in (self.backup, self.archive, self.restore):
            page.backups_changed.connect(self._on_backups_changed)
        self.archive.restore_requested.connect(self._open_restore)
        self.archive.compare_requested.connect(self._open_compare)
        self.overview.navigate.connect(self.show_page)
        self.settings_page.theme_changed.connect(self._on_theme_changed)
        self.bridge.started.connect(self._update_job_status)
        self.bridge.finished.connect(self._on_job_finished)

    def _paint_icons(self) -> None:
        t = theme.tokens()
        for row, key in self._key_for_row.items():
            spec = next(p for p in PAGES if p.key == key)
            self.nav.item(row).setIcon(icons.icon(spec.icon, t.rail_text, "#FFFFFF"))

    def _on_theme_changed(self, mode: str) -> None:
        app = QApplication.instance()
        if isinstance(app, QApplication):
            theme.apply_theme(app, theme.ThemeMode(mode))
            self._paint_icons()

    def _on_nav(self, row: int) -> None:
        key = self._key_for_row.get(row)
        if key is None:
            return
        self.stack.setCurrentIndex(self._page_index[key])
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
        active = len(self.app.jobs.active_jobs())
        self.job_status.setText(
            f"● {active} job{'s' if active != 1 else ''} running" if active else ""
        )

    def _on_job_finished(self, job: Job) -> None:
        self._update_job_status()
        if not self.app.jobs.active_jobs():
            self.job_status.setText(f"Last job: {JOB_STATUS_TEXT[job.status].lower()}")
            self.job_status.setToolTip(job.title)

    def show_page(self, key: str) -> None:
        self.nav.setCurrentRow(self._row_for_key[key])

    def current_page_key(self) -> str:
        return self._key_for_row[self.nav.currentRow()]

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
                "Jobs are running",
                f"{len(active)} job(s) are still running and will be cancelled. An interrupted "
                "restore can leave the target database incomplete. Quit anyway?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.bridge.detach()
        event.accept()
