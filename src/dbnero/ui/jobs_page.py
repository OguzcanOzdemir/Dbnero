"""Activity: running and finished jobs with their logs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.core.models import JobKind
from dbnero.ui.common import (
    JOB_STATUS_TEXT,
    STATUS_TONE,
    JobBridge,
    Page,
    StatusDelegate,
    button,
    fmt_local,
)

KIND_TEXT = {
    JobKind.BACKUP: "Backup",
    JobKind.RESTORE: "Restore",
    JobKind.COMPARE: "Compare",
    JobKind.VALIDATE: "Verify",
}
STATUS_COL = 3


class JobsPage(Page):
    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__("Activity", "Everything dbnero has done, with full logs.", parent)
        self.app = app
        self.btn_cancel = button("Cancel job", "danger", self.cancel_selected)
        self.header_actions.addWidget(self.btn_cancel)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Started", "Type", "Job", "Status", "Problem"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setItemDelegateForColumn(STATUS_COL, StatusDelegate(self.table))
        self.table.itemSelectionChanged.connect(self._show_logs)
        splitter.addWidget(self.table)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("Select a job to see its log.")
        splitter.addWidget(self.logs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        self.root.addWidget(splitter, 1)

        bridge.started.connect(lambda _job: self.reload())
        bridge.finished.connect(lambda _job: self.reload())
        bridge.logged.connect(self._on_log)
        self.reload()

    def _selected_job_id(self) -> str | None:
        items = self.table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def reload(self) -> None:
        selected = self._selected_job_id()
        jobs = self.app.catalog.list_jobs(limit=300)
        self.table.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            values = [
                fmt_local(job.started_at_utc, seconds=True),
                KIND_TEXT.get(job.kind, job.kind.value),
                job.title,
                JOB_STATUS_TEXT[job.status],
                job.error_message or "",
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.ItemDataRole.UserRole, job.job_id)
                if c == STATUS_COL:
                    item.setData(StatusDelegate.TONE_ROLE, STATUS_TONE.get(job.status.value))
                if c == 4:
                    item.setToolTip(v)
                self.table.setItem(r, c, item)
            if job.job_id == selected:
                self.table.selectRow(r)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(STATUS_COL, max(self.table.columnWidth(STATUS_COL), 100))
        self._update_buttons()

    def _update_buttons(self) -> None:
        job_id = self._selected_job_id()
        live = self.app.jobs.get(job_id) if job_id else None
        self.btn_cancel.setVisible(live is not None and not live.done)

    def _show_logs(self) -> None:
        self._update_buttons()
        job_id = self._selected_job_id()
        if job_id is None:
            self.logs.clear()
            return
        lines = [
            f"{fmt_local(ts, seconds=True)}  {level.lower():<7}  {msg}"
            for ts, level, msg in self.app.catalog.job_logs(job_id)
        ]
        self.logs.setPlainText("\n".join(lines))
        self.logs.verticalScrollBar().setValue(self.logs.verticalScrollBar().maximum())

    def _on_log(self, job: Job, level: str, message: str) -> None:
        if job.job_id == self._selected_job_id():
            self._show_logs()

    def cancel_selected(self) -> None:
        job_id = self._selected_job_id()
        if job_id:
            self.app.jobs.cancel(job_id)
