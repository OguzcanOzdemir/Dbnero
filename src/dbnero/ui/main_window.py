"""Main window: an ink navigation rail beside a stack of pages."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from dbnero import APP_NAME, __version__
from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.core.models import JobKind, Notification, NotificationLevel
from dbnero.ui import icons, theme
from dbnero.ui.archive_page import ArchivePage
from dbnero.ui.backup_page import BackupPage
from dbnero.ui.common import JOB_STATUS_TEXT, JobBridge
from dbnero.ui.compare_page import ComparePage
from dbnero.ui.connections_page import ConnectionsPage
from dbnero.ui.jobs_page import JobsPage
from dbnero.ui.overview_page import OverviewPage
from dbnero.ui.restore_page import RestorePage
from dbnero.ui.schedules_page import SchedulesPage
from dbnero.ui.settings_page import SettingsPage, ToolCheckDialog


@dataclass(frozen=True, slots=True)
class PageSpec:
    key: str
    title: str
    icon: str


PAGES: tuple[PageSpec, ...] = (
    PageSpec("overview", "Home", "home"),
    PageSpec("new_backup", "Back up", "backup"),
    PageSpec("schedules", "Schedules", "schedule"),
    PageSpec("archive", "Backups", "archive"),
    PageSpec("restore", "Restore", "restore"),
    PageSpec("compare", "Compare", "compare"),
    PageSpec("connections", "Connections", "connections"),
    PageSpec("jobs", "Activity", "activity"),
    PageSpec("settings", "Settings", "settings"),
)
log = logging.getLogger(__name__)

# How often the open app looks for due schedules.
SCHEDULE_CHECK_MS = 60_000

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
        self.schedules = SchedulesPage(app, self.bridge)
        self.archive = ArchivePage(app, self.bridge)
        self.restore = RestorePage(app, self.bridge)
        self.compare = ComparePage(app, self.bridge)
        self.jobs = JobsPage(app, self.bridge)
        self.settings_page = SettingsPage(app)
        widgets: dict[str, QWidget] = {
            "overview": self.overview,
            "connections": self.connections,
            "new_backup": self.backup,
            "schedules": self.schedules,
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
        for refresh in (
            self.backup.reload_connections,
            self.schedules.reload_connections,
            self.restore.reload,
            self.compare.reload,
        ):
            self.connections.connections_changed.connect(refresh)
        self.connections.connections_changed.connect(self.overview.reload)
        for page in (self.backup, self.archive, self.restore, self.schedules):
            page.backups_changed.connect(self._on_backups_changed)
        self.archive.restore_requested.connect(self._open_restore)
        self.archive.compare_requested.connect(self._open_compare)
        self.overview.navigate.connect(self.show_page)
        self.settings_page.theme_changed.connect(self._on_theme_changed)
        self.bridge.started.connect(self._update_job_status)
        self.bridge.finished.connect(self._on_job_finished)

        # Scheduled backups run while the app is open, too.
        self._schedule_job: Job | None = None
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(SCHEDULE_CHECK_MS)
        self.schedule_timer.timeout.connect(self.run_due_schedules)
        self.schedule_timer.start()
        QTimer.singleShot(5_000, self.run_due_schedules)

        # Notifications (missed or failed scheduled backups) show on Home and,
        # where the desktop supports it, as a short system notification.
        self._seen_notes: set[str] = set()
        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(str(icons.app_icon_path())), self)
            self.tray.setToolTip(APP_NAME)
            self.tray.activated.connect(lambda _reason: self._bring_to_front())
            self.tray.messageClicked.connect(self._bring_to_front)
            self.tray.show()
        self.overview.notifications_changed.connect(self.check_notifications)
        self.schedule_timer.timeout.connect(self.check_notifications)
        self.bridge.finished.connect(lambda _job: self.check_notifications())
        QTimer.singleShot(1_000, self.check_notifications)

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
        self.schedules.refresh_status()
        self.archive.reload()
        self.restore.reload()
        self.compare.reload()
        self.overview.reload()

    def run_due_schedules(self) -> Job | None:
        """Start a job for the schedules that are due, unless one is running
        here or another dbnero process (the OS task) is handling them."""
        if self._schedule_job is not None and not self._schedule_job.done:
            return None
        try:
            due = self.app.schedules.due()
            if not due or self.app.schedules.is_busy(self.app.scheduler_lock_path):
                return None
        except Exception:
            log.exception("Could not check scheduled backups")
            return None
        lock = self.app.scheduler_lock_path
        job = self.app.jobs.submit(
            JobKind.BACKUP,
            "Scheduled: " + ", ".join(s.name for s in due),
            lambda h: self.app.schedules.run_locked(
                lock, None, h.ctx, lambda lvl, msg: h.log(msg, lvl)
            ),
        )
        self._schedule_job = job
        # A missed run is announced as soon as its run starts.
        QTimer.singleShot(1_500, self.check_notifications)
        self.schedules.refresh_status()
        self.bridge.when_done(job, lambda _j: self._on_backups_changed())
        return job

    def check_notifications(self) -> list[Notification]:
        """Refresh the unread notifications; returns the ones not seen before."""
        try:
            notes = self.app.catalog.list_notifications(unread_only=True)
        except Exception:
            log.exception("Could not read notifications")
            return []
        new = [n for n in notes if n.id not in self._seen_notes]
        self._seen_notes = {n.id for n in notes}
        self.overview.reload_notifications()
        home = self.nav.item(self._row_for_key["overview"])
        if home is not None:
            home.setText(f"Home  ({len(notes)})" if notes else "Home")
        if new and self.tray is not None:
            if len(new) == 1:
                title, message = new[0].title, new[0].message
            else:
                title = f"{len(new)} new notifications"
                message = "\n".join(n.title for n in new[:4])
            danger = any(n.level is NotificationLevel.DANGER for n in new)
            icon = (
                QSystemTrayIcon.MessageIcon.Critical
                if danger
                else QSystemTrayIcon.MessageIcon.Warning
            )
            self.tray.showMessage(f"{APP_NAME}: {title}", message, icon, 15_000)
        return new

    def _bring_to_front(self) -> None:
        self.show_page("overview")
        self.showNormal()
        self.raise_()
        self.activateWindow()

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
        self.schedule_timer.stop()
        if self.tray is not None:
            self.tray.hide()
        event.accept()
