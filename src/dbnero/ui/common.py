"""Shared UI building blocks: background calls, job bridge, formatting and
small themed widgets (page header, card, status pill, collapsible section)."""

from __future__ import annotations

import html
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QHelpEvent, QMouseEvent, QPainter, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.jobs import Job, JobEvent, JobEventType, JobManager
from dbnero.core.logging_setup import mask_secrets
from dbnero.core.models import BackupStatus, JobStatus
from dbnero.ui import theme

# -- formatting ---------------------------------------------------------------


def fmt_bytes(value: int | None) -> str:
    if value is None:
        return "–"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def fmt_local(dt: datetime | None, seconds: bool = False) -> str:
    """Show a UTC timestamp in the user's local time zone."""
    if dt is None:
        return "–"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")


def backup_choice_label(rec: Any) -> str:
    """One-line label for picking a backup: database, time, server, description, tags."""
    parts = [rec.database_name, fmt_local(rec.created_at_utc), rec.server_label]
    if rec.description:
        parts.append(rec.description)
    if rec.tags:
        parts.append(" ".join(f"#{t}" for t in rec.tags))
    return " · ".join(parts)


BACKUP_STATUS_TEXT = {
    BackupStatus.PENDING: "Pending",
    BackupStatus.RUNNING: "Running",
    BackupStatus.SUCCEEDED: "OK",
    BackupStatus.FAILED: "Failed",
    BackupStatus.UNVERIFIED: "Unverified",
    BackupStatus.CANCELLED: "Cancelled",
}

JOB_STATUS_TEXT = {
    JobStatus.QUEUED: "Queued",
    JobStatus.RUNNING: "Running",
    JobStatus.SUCCEEDED: "Done",
    JobStatus.FAILED: "Failed",
    JobStatus.CANCELLED: "Cancelled",
}

# status value -> tone used by StatusDelegate / labels
STATUS_TONE = {
    "succeeded": "success",
    "failed": "danger",
    "unverified": "warning",
    "cancelled": "neutral",
    "running": "accent",
    "queued": "neutral",
    "pending": "neutral",
}


def details_html(title: str, subtitle: str, rows: list[tuple[str, str]], problem: str = "") -> str:
    """A small label/value sheet for detail panels (themed, wraps long values)."""
    t = theme.tokens()
    esc = html.escape
    parts = [
        f"<div style='font-size:12pt; font-weight:600'>{esc(title)}</div>",
        f"<div style='color:{t.text_muted}; margin-bottom:10px'>{esc(subtitle)}</div>",
        "<table cellspacing='0' cellpadding='3' width='100%'>",
    ]
    for label, value in rows:
        if not label:
            continue
        parts.append(
            f"<tr><td style='color:{t.text_muted}; white-space:nowrap; padding-right:12px' "
            f"valign='top'>{esc(label)}</td><td>{esc(value or '–')}</td></tr>"
        )
    parts.append("</table>")
    if problem:
        parts.append(
            f"<p style='color:{t.danger}; margin-top:10px'><b>Problem:</b> {esc(problem)}</p>"
        )
    return "".join(parts)


def tone_colors(tone: str) -> tuple[QColor, QColor]:
    t = theme.tokens()
    fg, bg = {
        "success": (t.success, t.success_soft),
        "danger": (t.danger, t.danger_soft),
        "warning": (t.warning, t.warning_soft),
        "accent": (t.accent, t.accent_soft),
    }.get(tone, (t.text_muted, t.surface_alt))
    return QColor(fg), QColor(bg)


def plural(n: int, word: str, plural_word: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural_word or word + 's')}"


def error_text(exc: BaseException) -> str:
    return mask_secrets(str(exc)) or type(exc).__name__


def show_error(parent: QWidget | None, title: str, exc: BaseException | str) -> None:
    text = exc if isinstance(exc, str) else error_text(exc)
    QMessageBox.critical(parent, title, text)


# -- widgets ------------------------------------------------------------------


def button(text: str, kind: str = "", on_click: Callable[[], Any] | None = None) -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setProperty("kind", kind)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click is not None:
        btn.clicked.connect(lambda _checked=False: on_click())
    return btn


def combo(min_chars: int = 14) -> QComboBox:
    """A combo box that does not demand the width of its longest item, so a
    long backup label can never squeeze its form label or neighbours away.
    The popup is widened to show items in full."""
    box = QComboBox()
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(min_chars)
    view = box.view()
    if view is not None:
        view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        view.setMinimumWidth(320)
    return box


def muted(text: str = "", wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("muted")
    label.setWordWrap(wrap)
    return label


def set_tone(label: QLabel, tone: str) -> None:
    label.setProperty("tone", tone)
    label.style().unpolish(label)
    label.style().polish(label)


class Page(QWidget):
    """A page with a title row (title, subtitle, right-aligned actions)."""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(32, 26, 32, 22)
        self.root.setSpacing(16)
        header = QHBoxLayout()
        header.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("pageTitle")
        text.addWidget(self.title_label)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("pageSubtitle")
            sub.setWordWrap(True)
            text.addWidget(sub)
        header.addLayout(text, 1)
        self.header_actions = QHBoxLayout()
        self.header_actions.setSpacing(8)
        header.addLayout(self.header_actions)
        self.root.addLayout(header)


class Card(QFrame):
    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 16, 18, 16)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("sectionTitle")
            self.body.addWidget(label)


class Banner(QFrame):
    """A tinted message strip (info / warning / danger)."""

    def __init__(self, tone: str = "info", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        self.setProperty("tone", tone)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.label, 1)
        self.setVisible(False)

    def show_message(self, text: str, tone: str | None = None) -> None:
        if tone:
            self.setProperty("tone", tone)
            self.style().unpolish(self)
            self.style().polish(self)
        self.label.setText(text)
        self.setVisible(bool(text))


class Collapsible(QWidget):
    """A section that starts collapsed behind a "Show …" toggle."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.toggle = QToolButton()
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setStyleSheet("QToolButton { border: none; font-weight: 600; }")
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content.setVisible(False)
        self.toggle.toggled.connect(self._on_toggle)
        layout.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.content)

    def _on_toggle(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.content.setVisible(on)

    def expand(self) -> None:
        self.toggle.setChecked(True)


CELL_PADDING = 12


class CellDelegate(QStyledItemDelegate):
    """Draws a table cell the dbnero way: full-row hover and selection colors,
    a hairline divider, padded and elided text, and a tooltip with the full
    text when it does not fit. Item foreground, font and alignment are kept."""

    def _background(self, painter: QPainter, option: QStyleOptionViewItem, row: int) -> None:
        t = theme.tokens()
        view = self.parent()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = isinstance(view, DataTable) and view.hover_row == row
        if selected:
            painter.fillRect(option.rect, QColor(t.accent_soft))
        elif hovered:
            painter.fillRect(option.rect, QColor(t.surface_alt))
        painter.setPen(QPen(QColor(t.surface_alt if not t.dark else t.border), 1))
        r = option.rect
        painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        self._background(painter, option, index.row())
        text = str(index.data() or "")
        if text:
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            color = fg.color() if hasattr(fg, "color") else QColor(theme.tokens().text)
            font = index.data(Qt.ItemDataRole.FontRole)
            if font is not None:
                painter.setFont(font)
            align = index.data(Qt.ItemDataRole.TextAlignmentRole)
            flags = (
                Qt.AlignmentFlag(int(align))
                if align
                else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            area = option.rect.adjusted(CELL_PADDING, 0, -CELL_PADDING, 0)
            fm = painter.fontMetrics()
            shown = fm.elidedText(text.splitlines()[0], Qt.TextElideMode.ElideRight, area.width())
            painter.setPen(color)
            painter.drawText(area, int(flags | Qt.AlignmentFlag.AlignVCenter), shown)
        painter.restore()

    def helpEvent(
        self,
        event: QHelpEvent,
        view: QAbstractItemView,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if event is None or not index.isValid():
            return False
        tip = index.data(Qt.ItemDataRole.ToolTipRole)
        text = str(index.data() or "")
        too_wide = (
            option.fontMetrics.horizontalAdvance(text) > option.rect.width() - 2 * CELL_PADDING
        )
        if tip or (text and (too_wide or "\n" in text)):
            QToolTip.showText(event.globalPos(), str(tip or text), view)
            return True
        QToolTip.hideText()
        return True


class StatusDelegate(CellDelegate):
    """Paints the cell text as a rounded pill. The tone comes from the item's
    ``Qt.UserRole + 2`` data ("success", "danger", "warning", "accent", ...)."""

    TONE_ROLE = Qt.ItemDataRole.UserRole + 2

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        self._background(painter, option, index.row())
        painter.restore()
        text = str(index.data() or "")
        if not text:
            return
        fg, bg = tone_colors(str(index.data(self.TONE_ROLE) or "neutral"))
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = option.fontMetrics
        width = fm.horizontalAdvance(text) + 18
        height = fm.height() + 6
        rect: QRect = option.rect
        pill = QRect(rect.x() + CELL_PADDING - 2, rect.center().y() - height // 2, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(pill, height / 2, height / 2)
        painter.setPen(fg)
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()


class DataTable(QTableWidget):
    """The table used across the app: no grid, comfortable rows, left-aligned
    headers, whole-row hover/selection and columns that fit the width.

    ``stretch_col`` takes the remaining width; other columns size to their
    contents up to ``max_col_width`` (longer text is elided with a tooltip)."""

    def __init__(
        self,
        headers: list[str],
        status_col: int = -1,
        stretch_col: int | None = None,
        selectable: bool = True,
        max_col_width: int = 280,
        keep_cols: tuple[int, ...] = (),
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(0, len(headers), parent)
        self.keep_cols = set(keep_cols)  # never narrowed (dates, sizes, status)
        self.setObjectName("data")
        self.hover_row = -1
        self.stretch_col = len(headers) - 1 if stretch_col is None else stretch_col
        self.max_col_width = max_col_width
        self.setHorizontalHeaderLabels(headers)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        if selectable:
            self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        else:
            self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setMouseTracking(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        vh = self.verticalHeader()
        vh.setVisible(False)
        vh.setDefaultSectionSize(40)
        hh = self.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hh.setHighlightSections(False)
        hh.setStretchLastSection(False)
        hh.setMinimumSectionSize(60)
        self.setItemDelegate(CellDelegate(self))
        if status_col >= 0:
            self.setItemDelegateForColumn(status_col, StatusDelegate(self))

    def fit_columns(self) -> None:
        """Size columns to their contents (capped), then fit them into the
        visible width: the stretch column takes what is left, and if space is
        short the widest columns give way first (their text is elided)."""
        hh = self.horizontalHeader()
        for col in range(self.columnCount()):
            hh.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
        self.resizeColumnsToContents()
        self._natural = [
            min(self.columnWidth(c) + 2 * CELL_PADDING, self.max_col_width)
            for c in range(self.columnCount())
        ]
        self._apply_widths()

    def _apply_widths(self) -> None:
        natural = getattr(self, "_natural", None)
        if not natural:
            return
        available = self.viewport().width()
        widths = list(natural)
        stretch = self.stretch_col if 0 <= self.stretch_col < len(widths) else -1
        min_stretch = 140 if stretch >= 0 else 0
        others = [c for c in range(len(widths)) if c != stretch]
        shrinkable = [c for c in others if c not in self.keep_cols]
        excess = sum(widths[c] for c in others) + min_stretch - available
        # Shrink the widest columns first, never below a readable minimum.
        while excess > 0 and shrinkable:
            col = max(shrinkable, key=lambda c: widths[c])
            floor = 100
            if widths[col] <= floor:
                break
            cut = min(excess, widths[col] - floor, 24)
            widths[col] -= cut
            excess -= cut
        for c in others:
            self.setColumnWidth(c, widths[c])
        if stretch >= 0:
            rest = available - sum(widths[c] for c in others)
            self.setColumnWidth(stretch, max(rest, min(natural[stretch], 140), 60))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._apply_widths()

    def _set_hover(self, row: int) -> None:
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._set_hover(self.rowAt(int(event.position().y())))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._set_hover(-1)
        super().leaveEvent(event)


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

    def ok(result: Any) -> None:
        _active_calls.discard(call)
        if on_success:
            on_success(result)

    def err(exc: BaseException) -> None:
        _active_calls.discard(call)
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


class JobProgress(QWidget):
    """Progress bar + cancel button + log for one running job."""

    def __init__(self, bridge: JobBridge, parent: QWidget | None = None) -> None:
        from PySide6.QtWidgets import QPlainTextEdit, QProgressBar

        super().__init__(parent)
        self.bridge = bridge
        self.job: Job | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        row = QHBoxLayout()
        self.message = muted(wrap=False)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.cancel_button = button("Cancel", on_click=self.cancel)
        row.addWidget(self.bar, 1)
        row.addWidget(self.cancel_button)
        layout.addWidget(self.message)
        layout.addLayout(row)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(3000)
        self.log.setPlaceholderText("Job log")
        layout.addWidget(self.log, 1)
        self._set_active(False)
        bridge.progress.connect(self._on_progress)
        bridge.logged.connect(self._on_log)

    def track(self, job: Job) -> None:
        self.job = job
        self.log.clear()
        self.message.setText("Starting…")
        self._set_active(True)

    def finish(self) -> None:
        self.job = None
        self._set_active(False)

    def cancel(self) -> None:
        if self.job is not None:
            self.bridge.manager.cancel(self.job.job_id)
            self.cancel_button.setEnabled(False)

    def _set_active(self, active: bool) -> None:
        self.bar.setVisible(active)
        self.cancel_button.setVisible(active)
        self.cancel_button.setEnabled(active)
        self.message.setVisible(active)
        self.bar.setRange(0, 0)

    def _on_progress(self, job: Job, fraction: float | None, message: str) -> None:
        if self.job is None or job.job_id != self.job.job_id:
            return
        if fraction is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(fraction * 100))
        self.message.setText(message)

    def _on_log(self, job: Job, level: str, message: str) -> None:
        if self.job is not None and job.job_id == self.job.job_id:
            prefix = {"WARNING": "warning: ", "ERROR": "error: "}.get(level, "")
            self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {prefix}{message}")


def hbox(*widgets: QWidget, stretch: bool = True, spacing: int = 8) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for w in widgets:
        layout.addWidget(w)
    if stretch:
        layout.addStretch(1)
    return layout
