"""Shared UI helpers: background calls, job signal bridge, formatting."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.jobs import Job, JobEvent, JobEventType, JobManager
from dbnero.core.logging_setup import mask_secrets
from dbnero.core.models import BackupStatus, JobStatus

# -- formatting ---------------------------------------------------------------


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_local(dt: datetime | None, seconds: bool = False) -> str:
    """Show a UTC timestamp in the user's local time zone."""
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M")


BACKUP_STATUS_TEXT = {
    BackupStatus.PENDING: "Bekliyor",
    BackupStatus.RUNNING: "Çalışıyor",
    BackupStatus.SUCCEEDED: "Başarılı",
    BackupStatus.FAILED: "Başarısız",
    BackupStatus.UNVERIFIED: "Doğrulanamadı",
    BackupStatus.CANCELLED: "İptal",
}

JOB_STATUS_TEXT = {
    JobStatus.QUEUED: "Sırada",
    JobStatus.RUNNING: "Çalışıyor",
    JobStatus.SUCCEEDED: "Başarılı",
    JobStatus.FAILED: "Başarısız",
    JobStatus.CANCELLED: "İptal",
}

STATUS_COLORS = {
    "succeeded": QColor("#1b7f3b"),
    "failed": QColor("#b3261e"),
    "unverified": QColor("#a15c00"),
    "cancelled": QColor("#6b6b6b"),
    "running": QColor("#1f5fbf"),
    "queued": QColor("#6b6b6b"),
    "pending": QColor("#6b6b6b"),
}


def error_text(exc: BaseException) -> str:
    return mask_secrets(str(exc)) or type(exc).__name__


def show_error(parent: QWidget | None, title: str, exc: BaseException | str) -> None:
    text = exc if isinstance(exc, str) else error_text(exc)
    QMessageBox.critical(parent, title, text)


def page_header(title: str, subtitle: str = "") -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 8)
    t = QLabel(title)
    t.setObjectName("pageTitle")
    t.setStyleSheet("font-size: 20px; font-weight: 600;")
    layout.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setWordWrap(True)
        s.setStyleSheet("color: palette(mid);")
        layout.addWidget(s)
    return box


def hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def row(*widgets: QWidget, stretch_last: bool = False) -> QWidget:
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    if not stretch_last:
        layout.addStretch(1)
    return box


# -- background calls (not recorded as jobs) ---------------------------------


class _CallSignals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)


class _Call(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _CallSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # delivered to the GUI thread
            exc.__dict__.setdefault("_tb", traceback.format_exc())
            self.signals.failed.emit(exc)
        else:
            self.signals.succeeded.emit(result)


_active_calls: set[_Call] = set()


def run_async(
    fn: Callable[[], Any],
    on_success: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Run a short blocking call (connection test, listing databases) on the
    global QThreadPool and deliver the result on the GUI thread."""
    call = _Call(fn)
    call.setAutoDelete(False)
    _active_calls.add(call)

    def finish() -> None:
        _active_calls.discard(call)

    def ok(result: Any) -> None:
        finish()
        if on_success:
            on_success(result)

    def err(exc: BaseException) -> None:
        finish()
        if on_error:
            on_error(exc)

    conn_type = Qt.ConnectionType.QueuedConnection
    call.signals.succeeded.connect(ok, conn_type)
    call.signals.failed.connect(err, conn_type)
    QThreadPool.globalInstance().start(call)


# -- job bridge ---------------------------------------------------------------


class JobBridge(QObject):
    """Re-emits JobManager events (worker threads) as Qt signals (GUI thread)."""

    started = Signal(object)  # Job
    progress = Signal(object, object, str)  # Job, fraction|None, message
    logged = Signal(object, str, str)  # Job, level, message
    finished = Signal(object)  # Job

    def __init__(self, manager: JobManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self._callbacks: dict[str, Callable[[Job], None]] = {}
        manager.add_listener(self._on_event)
        self.finished.connect(self._run_callback)

    def _on_event(self, event: JobEvent) -> None:
        if event.type is JobEventType.STARTED:
            self.started.emit(event.job)
        elif event.type is JobEventType.PROGRESS:
            self.progress.emit(event.job, event.job.progress, event.message)
        elif event.type is JobEventType.LOG:
            self.logged.emit(event.job, event.level, event.message)
        else:
            self.finished.emit(event.job)

    def when_done(self, job: Job, callback: Callable[[Job], None]) -> None:
        if job.done:
            callback(job)
        else:
            self._callbacks[job.job_id] = callback

    @Slot(object)
    def _run_callback(self, job: Job) -> None:
        cb = self._callbacks.pop(job.job_id, None)
        if cb is not None:
            cb(job)

    def detach(self) -> None:
        self.manager.remove_listener(self._on_event)
