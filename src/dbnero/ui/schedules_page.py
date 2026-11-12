"""Schedules: back up databases automatically, every day or every few hours."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSpinBox,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.jobs import Job
from dbnero.core.models import JobKind, JobStatus, Schedule, ScheduleFrequency, ScheduleRunStatus
from dbnero.core.schedule_service import WEEKDAY_NAMES, describe, next_run
from dbnero.ui.backup_page import parse_tags
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    JobBridge,
    JobProgress,
    Page,
    button,
    error_text,
    fmt_local,
    muted,
    run_async,
    set_tone,
    show_error,
)

# Repeat modes shown to the user; "days" is DAILY with a subset of weekdays.
REPEAT_DAILY = "daily"
REPEAT_DAYS = "days"
REPEAT_HOURS = "hours"

STATUS_TEXT = {
    ScheduleRunStatus.SUCCEEDED: ("Last run OK", "success"),
    ScheduleRunStatus.PARTIAL: ("Last run partly failed", "warning"),
    ScheduleRunStatus.FAILED: ("Last run failed", "danger"),
    ScheduleRunStatus.RUNNING: ("Running now", "info"),
}


def schedule_summary(sched: Schedule) -> str:
    if not sched.enabled:
        return f"{describe(sched)} · paused"
    nxt = next_run(sched)
    return f"{describe(sched)} · next {fmt_local(nxt)}" if nxt else describe(sched)


class SchedulesPage(Page):
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(
            "Schedules",
            "Back up databases automatically. Old scheduled backups are removed for you.",
            parent,
        )
        self.app = app
        self.bridge = bridge
        self._current: Schedule | None = None
        self._job: Job | None = None
        self._loaded_conn: str | None = None
        self._wanted_dbs: set[str] = set()
        self.header_actions.addWidget(button("New schedule", "primary", self.new_schedule))

        body = QHBoxLayout()
        body.setSpacing(16)
        self.root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(12)
        left_box = QWidget()
        left_box.setFixedWidth(300)
        left_box.setLayout(left)
        left.setContentsMargins(0, 0, 0, 0)
        body.addWidget(left_box)
        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(self._on_select)
        left.addWidget(self.list, 1)

        right = QVBoxLayout()
        right.setSpacing(12)
        body.addLayout(right, 1)

        card = Card()
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Nightly production")
        self.connection = QComboBox()
        self.connection.currentIndexChanged.connect(lambda _i: self._load_databases())
        self.all_dbs = QCheckBox("All databases on this server, including new ones")
        self.all_dbs.toggled.connect(self._update_fields)
        self.db_list = QListWidget()
        self.db_list.setFixedHeight(130)

        self.repeat = QComboBox()
        self.repeat.addItem("Every day", REPEAT_DAILY)
        self.repeat.addItem("On chosen days", REPEAT_DAYS)
        self.repeat.addItem("Every few hours", REPEAT_HOURS)
        self.repeat.currentIndexChanged.connect(self._update_fields)
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm")
        self.time.setFixedWidth(110)
        self.days_row = QWidget()
        days_layout = QHBoxLayout(self.days_row)
        days_layout.setContentsMargins(0, 0, 0, 0)
        self.day_boxes = [QCheckBox(name) for name in WEEKDAY_NAMES]
        for box in self.day_boxes:
            days_layout.addWidget(box)
        days_layout.addStretch(1)
        self.hours = QSpinBox()
        self.hours.setRange(1, 168)
        self.hours.setSuffix(" hours")
        self.hours.setFixedWidth(130)
        self.keep = QSpinBox()
        self.keep.setRange(0, 999)
        self.keep.setSpecialValueText("Keep all")
        self.keep.setSuffix(" per database")
        self.keep.setFixedWidth(170)
        self.enabled = QCheckBox("On")

        form.addRow("Name", self.name)
        form.addRow("Server", self.connection)
        form.addRow("Databases", self.all_dbs)
        form.addRow("", self.db_list)
        form.addRow("Repeat", self.repeat)
        self._time_label = "At"
        form.addRow(self._time_label, self.time)
        form.addRow("Days", self.days_row)
        form.addRow("Every", self.hours)
        form.addRow("Keep last", self.keep)
        form.addRow("", self.enabled)
        self._form = form
        card.body.addLayout(form)

        self.advanced = Collapsible("Advanced options")
        adv = QFormLayout()
        adv.setHorizontalSpacing(16)
        self.description = QLineEdit()
        self.description.setPlaceholderText("Shown on each backup (default: Scheduled: name)")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("Comma separated; “scheduled” is always added")
        adv.addRow("Description", self.description)
        adv.addRow("Tags", self.tags)
        self.advanced.content_layout.addLayout(adv)
        card.body.addWidget(self.advanced)

        actions = QHBoxLayout()
        self.btn_run = button("Run now", on_click=self.run_now)
        self.btn_delete = button("Delete", "danger", self.delete_schedule)
        self.btn_save = button("Save", "primary", self.save_schedule)
        actions.addWidget(self.btn_run)
        actions.addStretch(1)
        actions.addWidget(self.btn_delete)
        actions.addWidget(self.btn_save)
        card.body.addLayout(actions)
        right.addWidget(card)

        self.status_banner = Banner()
        self.status = self.status_banner.label
        right.addWidget(self.status_banner)

        self.last_run = muted()
        right.addWidget(self.last_run)
        self.progress = JobProgress(bridge)
        right.addWidget(self.progress)

        bg = Card("When dbnero is closed")
        self.background = QCheckBox("Keep running schedules")
        self.background.toggled.connect(self._on_background_toggled)
        self.background_hint = muted()
        bg.body.addWidget(self.background)
        bg.body.addWidget(self.background_hint)
        left.addWidget(bg)
        right.addStretch(1)

        self.reload_connections()
        self.reload()
        self._refresh_background()

    # -- data ---------------------------------------------------------------

    def reload_connections(self) -> None:
        current = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        for p in self.app.connections.list_profiles():
            self.connection.addItem(f"{p.name} · {p.server_label}", p.id)
        idx = self.connection.findData(current)
        self.connection.setCurrentIndex(max(idx, 0))
        self.connection.blockSignals(False)
        self._loaded_conn = None
        self._load_databases()

    def reload(self, select_id: str | None = None) -> None:
        select_id = select_id or (self._current.id if self._current else None)
        self.list.blockSignals(True)
        self.list.clear()
        selected = None
        for s in self.app.schedules.list_all():
            item = QListWidgetItem(f"{s.name}\n{schedule_summary(s)}")
            item.setData(Qt.ItemDataRole.UserRole, s.id)
            self.list.addItem(item)
            if s.id == select_id:
                selected = item
        self.list.blockSignals(False)
        if selected is not None:
            self.list.setCurrentItem(selected)
        elif self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.new_schedule()

    def refresh_status(self) -> None:
        """Update list texts and the last-run line; never touches the form."""
        by_id = {s.id: s for s in self.app.schedules.list_all()}
        for i in range(self.list.count()):
            item = self.list.item(i)
            sched = by_id.get(item.data(Qt.ItemDataRole.UserRole))
            if sched is not None:
                item.setText(f"{sched.name}\n{schedule_summary(sched)}")
        if self._current is not None and self._current.id in by_id:
            self._show_last_run(by_id[self._current.id])

    def _on_select(self, item: QListWidgetItem | None, _prev: QListWidgetItem | None) -> None:
        if item is None:
            return
        try:
            self._load(self.app.schedules.get(item.data(Qt.ItemDataRole.UserRole)))
        except Exception as exc:
            show_error(self, "Could not open the schedule", exc)

    def new_schedule(self) -> None:
        self.list.clearSelection()
        conn = self.connection.currentData() or ""
        self._load(Schedule(name="", connection_id=conn))
        self._current = None
        self.btn_delete.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.last_run.setText("")
        self.name.setFocus()

    def _load(self, s: Schedule) -> None:
        self._current = s
        self.status_banner.hide()
        self.name.setText(s.name)
        idx = self.connection.findData(s.connection_id)
        if idx >= 0 and idx != self.connection.currentIndex():
            self.connection.blockSignals(True)
            self.connection.setCurrentIndex(idx)
            self.connection.blockSignals(False)
        self.all_dbs.setChecked(s.all_databases)
        self._wanted_dbs = set(s.databases)
        self._loaded_conn = None
        self._load_databases()
        days = set(s.weekdays)
        if s.frequency is ScheduleFrequency.HOURLY:
            mode = REPEAT_HOURS
        elif days == set(range(7)):
            mode = REPEAT_DAILY
        else:
            mode = REPEAT_DAYS
        self.repeat.setCurrentIndex(self.repeat.findData(mode))
        hour, _, minute = s.time_of_day.partition(":")
        try:
            self.time.setTime(QTime(int(hour), int(minute)))
        except ValueError:
            self.time.setTime(QTime(2, 0))
        for i, box in enumerate(self.day_boxes):
            box.setChecked(i in days)
        self.hours.setValue(max(1, s.every_hours))
        self.keep.setValue(s.keep_last)
        self.enabled.setChecked(s.enabled)
        self.description.setText(s.description)
        self.tags.setText(", ".join(s.tags))
        saved = any(x.id == s.id for x in self.app.schedules.list_all())
        self.btn_delete.setEnabled(saved)
        self.btn_run.setEnabled(saved and self._job is None)
        self._show_last_run(s if saved else None)
        self._update_fields()

    def _show_last_run(self, s: Schedule | None) -> None:
        if s is None:
            self.last_run.setText("")
            return
        parts = []
        if s.last_run_utc:
            text, tone = STATUS_TEXT.get(s.last_status, ("Last run", "muted"))
            parts.append(f"{text} · {fmt_local(s.last_run_utc)}")
            if s.last_message:
                parts.append(s.last_message)
            set_tone(self.last_run, tone)
        else:
            parts.append("Not run yet")
            set_tone(self.last_run, "muted")
        nxt = next_run(s)
        parts.append(f"Next run: {fmt_local(nxt)}" if nxt else "Paused")
        self.last_run.setText("\n".join(parts))

    def _update_fields(self) -> None:
        mode = self.repeat.currentData()
        hourly = mode == REPEAT_HOURS
        for widget, visible in (
            (self.time, not hourly),
            (self.days_row, mode == REPEAT_DAYS),
            (self.hours, hourly),
        ):
            widget.setVisible(visible)
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)
        self.db_list.setVisible(not self.all_dbs.isChecked())

    def _load_databases(self) -> None:
        conn_id = self.connection.currentData()
        if not conn_id or conn_id == self._loaded_conn:
            return
        self._loaded_conn = conn_id
        self.db_list.clear()
        self.db_list.addItem("Loading databases…")

        def ok(names: list[str]) -> None:
            if self.connection.currentData() != conn_id:
                return
            self.db_list.clear()
            # Keep chosen databases visible even if they are gone right now.
            for name in sorted(set(names) | self._wanted_dbs, key=str.casefold):
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                checked = name in self._wanted_dbs
                item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                self.db_list.addItem(item)

        def fail(exc: BaseException) -> None:
            if self.connection.currentData() != conn_id:
                return
            self._loaded_conn = None
            ok([])
            self.status_banner.show_message(
                f"Could not list databases: {error_text(exc)}", "warning"
            )

        run_async(lambda: self.app.connections.list_databases(conn_id), ok, fail)

    def _checked_dbs(self) -> list[str]:
        out = []
        for i in range(self.db_list.count()):
            item = self.db_list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and (
                item.checkState() == Qt.CheckState.Checked
            ):
                out.append(item.text())
        return out

    def _form_schedule(self) -> Schedule:
        base = self._current or Schedule(name="", connection_id="")
        mode = self.repeat.currentData()
        if mode == REPEAT_DAILY:
            weekdays = list(range(7))
        else:
            weekdays = [i for i, box in enumerate(self.day_boxes) if box.isChecked()]
        base.name = self.name.text().strip()
        base.connection_id = self.connection.currentData() or ""
        base.all_databases = self.all_dbs.isChecked()
        base.databases = [] if base.all_databases else self._checked_dbs()
        base.frequency = (
            ScheduleFrequency.HOURLY if mode == REPEAT_HOURS else (ScheduleFrequency.DAILY)
        )
        base.time_of_day = self.time.time().toString("HH:mm")
        base.weekdays = weekdays if mode != REPEAT_HOURS else base.weekdays or list(range(7))
        base.every_hours = self.hours.value()
        base.keep_last = self.keep.value()
        base.enabled = self.enabled.isChecked()
        base.description = self.description.text().strip()
        base.tags = parse_tags(self.tags.text())
        return base

    # -- actions ------------------------------------------------------------------

    def save_schedule(self) -> None:
        sched = self._form_schedule()
        try:
            self.app.schedules.save(sched)
        except Exception as exc:
            self.status_banner.show_message(error_text(exc), "danger")
            return
        self._current = sched
        self.reload(select_id=sched.id)
        self.status_banner.show_message(f"Saved “{sched.name}”. {schedule_summary(sched)}.", "info")
        self._ensure_background()

    def delete_schedule(self) -> None:
        if self._current is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete schedule",
            f"Delete “{self._current.name}”? Backups it already made stay in Backups.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.app.schedules.delete(self._current.id)
        self._current = None
        self.reload()
        self.status_banner.show_message("Schedule deleted.", "info")
        self._ensure_background()

    def run_now(self) -> None:
        if self._current is None or self._job is not None:
            return
        sched_id = self._current.id
        lock = self.app.scheduler_lock_path
        job = self.app.jobs.submit(
            JobKind.BACKUP,
            f"Scheduled: {self._current.name}",
            lambda h: self.app.schedules.run_locked(
                lock, sched_id, h.ctx, lambda lvl, msg: h.log(msg, lvl)
            ),
            connection_id=self._current.connection_id,
        )
        self._job = job
        self.btn_run.setEnabled(False)
        self.progress.track(job)
        self.bridge.when_done(job, self._on_done)

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.progress.finish()
        self.btn_run.setEnabled(self._current is not None)
        self.refresh_status()
        if job.status is JobStatus.FAILED:
            self.status_banner.show_message(job.error or "The run failed", "danger")
        elif job.status is JobStatus.CANCELLED:
            self.status_banner.show_message("Run cancelled", "warning")
        else:
            runs = job.result or []
            msg = runs[0].message if runs else "Finished"
            tone = "info" if runs and runs[0].status is ScheduleRunStatus.SUCCEEDED else "warning"
            self.status_banner.show_message(msg, tone)
        self.backups_changed.emit()

    # -- background runs ----------------------------------------------------------

    def _refresh_background(self) -> None:
        osch = self.app.os_scheduler
        self.background.blockSignals(True)
        self.background.setChecked(self.app.settings.background_schedules)
        self.background.blockSignals(False)
        if not osch.supported:
            self.background.setEnabled(False)
            self.background_hint.setText(
                "This system has no scheduler dbnero can use. Keep dbnero open (it can be "
                "minimized) for scheduled backups to run."
            )
            return
        self.background_hint.setText(
            f"Uses {osch.description} to start dbnero briefly every few minutes while you "
            "are signed in. If the computer was off, missed backups run when it is back on."
        )

    def _on_background_toggled(self, on: bool) -> None:
        self.app.settings.background_schedules = on
        self.app.save_settings()
        self._ensure_background()

    def _ensure_background(self) -> None:
        """Register with the OS scheduler while there are schedules (and the
        user wants it); remove the registration otherwise."""
        osch = self.app.os_scheduler
        if not osch.supported:
            return
        want = self.app.settings.background_schedules and bool(self.app.schedules.list_all())

        def apply() -> bool:
            if want:
                osch.install()
            else:
                osch.uninstall()
            return want

        def ok(installed: bool) -> None:
            warning = osch.warning() if installed else None
            if warning:
                self.status_banner.show_message(warning, "warning")

        def fail(exc: BaseException) -> None:
            self.status_banner.show_message(
                "Could not set up background runs: "
                f"{error_text(exc)}. Scheduled backups still run while dbnero is open.",
                "warning",
            )

        run_async(apply, ok, fail)
