"""Job history: running and finished jobs with their persisted logs."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.core.models import JobKind
from dbnero.ui.common import JOB_STATUS_TEXT, STATUS_COLORS, JobBridge, fmt_local, page_header

KIND_TEXT = {
    JobKind.BACKUP: "Yedek",
    JobKind.RESTORE: "Geri yükleme",
    JobKind.COMPARE: "Karşılaştırma",
    JobKind.VALIDATE: "Doğrulama",
}


class JobsPage(QWidget):
    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header("İş Geçmişi", "Devam eden ve tamamlanan işler ile günlükleri.")
        )

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Başlangıç", "Bitiş", "Tür", "İş", "Durum", "Hata"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._show_logs)
        splitter.addWidget(self.table)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        splitter.addWidget(self.logs)
        splitter.setStretchFactor(0, 2)
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        self.btn_cancel = QPushButton("Seçili işi iptal et")
        self.btn_refresh = QPushButton("Yenile")
        self.btn_cancel.clicked.connect(self.cancel_selected)
        self.btn_refresh.clicked.connect(self.reload)
        buttons.addWidget(self.btn_cancel)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_refresh)
        layout.addLayout(buttons)

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
                fmt_local(job.finished_at_utc, seconds=True) if job.finished_at_utc else "",
                KIND_TEXT.get(job.kind, job.kind.value),
                job.title,
                JOB_STATUS_TEXT[job.status],
                job.error_message or "",
            ]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                item.setData(Qt.ItemDataRole.UserRole, job.job_id)
                if c == 4:
                    item.setForeground(STATUS_COLORS.get(job.status.value, STATUS_COLORS["queued"]))
                self.table.setItem(r, c, item)
            if job.job_id == selected:
                self.table.selectRow(r)
        self.table.resizeColumnsToContents()
        self._update_buttons()

    def _update_buttons(self) -> None:
        job_id = self._selected_job_id()
        live = self.app.jobs.get(job_id) if job_id else None
        self.btn_cancel.setEnabled(live is not None and not live.done)

    def _show_logs(self) -> None:
        self._update_buttons()
        job_id = self._selected_job_id()
        if job_id is None:
            self.logs.clear()
            return
        lines = [
            f"{fmt_local(ts, seconds=True)}  {level:<7}  {msg}"
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
