"""Home: what needs attention, recent backups and quick actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.ui.common import (
    BACKUP_STATUS_TEXT,
    STATUS_TONE,
    Banner,
    Card,
    JobBridge,
    Page,
    StatusDelegate,
    button,
    fmt_bytes,
    fmt_local,
    muted,
    set_tone,
)


class StatCard(Card):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self.body.setSpacing(2)
        self.value = QLabel("–")
        self.value.setObjectName("statValue")
        caption = QLabel(label)
        caption.setObjectName("muted")
        self.body.addWidget(self.value)
        self.body.addWidget(caption)

    def set(self, text: str, tone: str = "") -> None:
        self.value.setText(text)
        set_tone(self.value, tone)


class OverviewPage(Page):
    navigate = Signal(str)

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__("Home", "", parent)
        self.app = app
        self.header_actions.addWidget(
            button("Restore…", on_click=lambda: self.navigate.emit("restore"))
        )
        self.header_actions.addWidget(
            button("Back up now", "primary", lambda: self.navigate.emit("new_backup"))
        )

        self.attention = Banner("warning")
        self.btn_cleanup = button("Remove leftovers", "ghost", self.cleanup_partials)
        self.attention.layout().addWidget(self.btn_cleanup)  # type: ignore[union-attr]
        self.root.addWidget(self.attention)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.card_total = StatCard("Backups")
        self.card_failed = StatCard("Failed in the last 7 days")
        self.card_size = StatCard("Archive size")
        self.card_disk = StatCard("Archive disk used")
        for i, card in enumerate(
            [self.card_total, self.card_failed, self.card_size, self.card_disk]
        ):
            grid.addWidget(card, 0, i)
        self.root.addLayout(grid)

        lists = QHBoxLayout()
        lists.setSpacing(16)
        recent_card = Card("Recent backups")
        self.recent = self._table(["Created", "Database", "Server", "Size", "Status"], 4)
        self.recent_empty = muted("No backups yet. Add a connection, then back up a database.")
        recent_card.body.addWidget(self.recent, 1)
        recent_card.body.addWidget(self.recent_empty)
        recent_card.body.addStretch(0)
        lists.addWidget(recent_card, 3)
        failed_card = Card("Recent problems")
        self.failed = self._table(["Started", "Job", "Problem"], -1)
        self.failed_empty = muted("Nothing has failed recently.")
        failed_card.body.addWidget(self.failed_empty)
        failed_card.body.addWidget(self.failed, 1)
        lists.addWidget(failed_card, 2)
        box = QVBoxLayout()
        box.addLayout(lists)
        self.root.addLayout(box, 1)

        bridge.finished.connect(lambda _job: self.reload())
        self.reload()

    @staticmethod
    def _table(headers: list[str], status_col: int) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setShowGrid(False)
        table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.horizontalHeader().setStretchLastSection(True)
        table.setObjectName("flat")
        if status_col >= 0:
            table.setItemDelegateForColumn(status_col, StatusDelegate(table))
        return table

    def reload(self) -> None:
        stats = self.app.catalog_service.overview(self.app.backups.archive_root)
        self.card_total.set(str(stats.total_backups))
        self.card_failed.set(
            str(stats.failed_last_7_days), "danger" if stats.failed_last_7_days else ""
        )
        self.card_size.set(fmt_bytes(stats.archive_bytes))
        pct = stats.disk_used_percent
        warn_pct = self.app.settings.disk_warning_percent
        self.card_disk.set(f"{pct:.0f}%", "danger" if pct >= warn_pct else "")

        notes = []
        if not self.app.connections.list_profiles():
            notes.append("Add a connection to start backing up.")
        if stats.unverified:
            notes.append(f"{stats.unverified} backups failed verification.")
        if pct >= warn_pct:
            notes.append(
                f"The archive disk is {pct:.0f}% full ({fmt_bytes(stats.disk_free)} free). "
                "Consider cleaning up old backups."
            )
        if stats.stale_partials:
            notes.append(
                f"{len(stats.stale_partials)} unfinished backup files were left by an "
                "interrupted run."
            )
        self.attention.show_message("\n".join(notes))
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
                    item.setData(StatusDelegate.TONE_ROLE, STATUS_TONE[rec.status.value])
                self.recent.setItem(r, c, item)
        self.recent.resizeColumnsToContents()
        self.recent.setVisible(bool(stats.recent))
        self.recent_empty.setVisible(not stats.recent)

        self.failed.setRowCount(len(stats.recent_failed_jobs))
        for r, job in enumerate(stats.recent_failed_jobs):
            for c, v in enumerate(
                [fmt_local(job.started_at_utc), job.title, job.error_message or ""]
            ):
                item = QTableWidgetItem(v)
                item.setToolTip(v)
                self.failed.setItem(r, c, item)
        self.failed.resizeColumnsToContents()
        self.failed.setVisible(bool(stats.recent_failed_jobs))
        self.failed_empty.setVisible(not stats.recent_failed_jobs)
        self.failed_empty.setAlignment(Qt.AlignmentFlag.AlignTop)

    def cleanup_partials(self) -> None:
        if self.app.jobs.active_jobs():
            QMessageBox.information(self, "Clean up", "Wait until running jobs have finished.")
            return
        removed = self.app.catalog_service.cleanup_stale_partials(self.app.backups.archive_root)
        QMessageBox.information(self, "Clean up", f"Removed {removed} unfinished files.")
        self.reload()
