"""Back up: pick databases, describe the backup, run it."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDateTimeEdit,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.backup_service import BackupRequest, BatchResult
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupRecord, Engine, JobKind, JobStatus
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    JobBridge,
    JobProgress,
    Page,
    button,
    combo,
    error_text,
    fmt_bytes,
    muted,
    run_async,
)


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime")
    return value.astimezone(UTC)


def parse_tags(text: str) -> list[str]:
    return [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]


class BackupPage(Page):
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(
            "Back up",
            "Each selected database is written to its own file with its own archive entry.",
            parent,
        )
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._preselect: set[str] = set()

        columns = QHBoxLayout()
        columns.setSpacing(16)
        self.root.addLayout(columns, 1)

        source = Card("Databases")
        self.connection = combo()
        self.connection.currentIndexChanged.connect(self.load_databases)
        source.body.addWidget(self.connection)
        self.db_list = QListWidget()
        self.db_list.itemChanged.connect(self._update_summary)
        source.body.addWidget(self.db_list, 1)
        sel = QHBoxLayout()
        sel.addWidget(button("Select all", "ghost", lambda: self._check_all(True)))
        sel.addWidget(button("Clear", "ghost", lambda: self._check_all(False)))
        sel.addStretch(1)
        sel.addWidget(button("Refresh", "ghost", self.load_databases))
        source.body.addLayout(sel)
        columns.addWidget(source, 1)

        right = QVBoxLayout()
        right.setSpacing(12)
        details = Card("Details")
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.description = QLineEdit()
        self.description.setPlaceholderText("e.g. Before release 2.4")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("Comma separated, e.g. prod, weekly")
        form.addRow("Description", self.description)
        form.addRow("Tags", self.tags)
        details.body.addLayout(form)

        self.advanced = Collapsible("Advanced options")
        adv = QFormLayout()
        adv.setHorizontalSpacing(16)
        self.use_ref_date = QCheckBox("Reference date")
        self.ref_date = QDateTimeEdit(QDateTime.currentDateTime())
        self.ref_date.setCalendarPopup(True)
        self.ref_date.setEnabled(False)
        self.use_ref_date.toggled.connect(self.ref_date.setEnabled)
        self.compress = QCheckBox("Compress MySQL dumps (.sql.gz)")
        self.compress.setChecked(True)
        self.mssql_compress = QCheckBox("SQL Server backup compression (not in Express)")
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(self.app.settings.max_parallel_jobs)
        adv.addRow(self.use_ref_date, self.ref_date)
        adv.addRow("", self.compress)
        adv.addRow("", self.mssql_compress)
        adv.addRow("Parallel backups", self.parallel)
        adv.addRow(
            muted(
                "The real creation time is always recorded in UTC and cannot be changed. "
                "Backups of different databases are not taken at one shared point in time."
            )
        )
        self.advanced.content_layout.addLayout(adv)
        details.body.addWidget(self.advanced)
        details.body.addStretch(1)

        run_row = QHBoxLayout()
        self.summary = muted(wrap=False)
        self.btn_start = button("Back up", "primary", self.start)
        run_row.addWidget(self.summary, 1)
        run_row.addWidget(self.btn_start)
        details.body.addLayout(run_row)
        right.addWidget(details)

        self.result = Banner()
        right.addWidget(self.result)
        self.progress = JobProgress(bridge)
        self.log = self.progress.log
        right.addWidget(self.progress, 1)
        columns.addLayout(right, 1)

        self.reload_connections()

    # -- connections / databases -----------------------------------------

    def reload_connections(self) -> None:
        current = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        for p in self.app.connections.list_profiles():
            self.connection.addItem(f"{p.name} · {p.engine.display_name}", p.id)
        idx = self.connection.findData(current)
        self.connection.setCurrentIndex(idx if idx >= 0 else 0)
        self.connection.blockSignals(False)
        self.load_databases()

    def select(self, connection_id: str, databases: list[str] | None = None) -> None:
        self._preselect = set(databases or [])
        idx = self.connection.findData(connection_id)
        if idx >= 0:
            self.connection.setCurrentIndex(idx)

    def load_databases(self) -> None:
        conn_id = self.connection.currentData()
        self.db_list.clear()
        if not conn_id:
            self.summary.setText("Add a connection first.")
            self.btn_start.setEnabled(False)
            return
        profile = self.app.connections.get(conn_id)
        self.compress.setVisible(profile.engine is Engine.MYSQL)
        self.mssql_compress.setVisible(profile.engine is Engine.MSSQL)
        self.summary.setText("Loading databases…")

        def ok(names: list[str]) -> None:
            if self.connection.currentData() != conn_id:
                return
            self.db_list.blockSignals(True)
            self.db_list.clear()
            for name in names:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if name in self._preselect else Qt.CheckState.Unchecked
                )
                self.db_list.addItem(item)
            self.db_list.blockSignals(False)
            self._preselect = set()
            self._update_summary()

        def fail(exc: BaseException) -> None:
            self.summary.setText("Could not list databases.")
            self.result.show_message(error_text(exc), "danger")

        run_async(lambda: self.app.connections.list_databases(conn_id), ok, fail)

    def _check_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.db_list.count()):
            self.db_list.item(i).setCheckState(state)

    def selected_databases(self) -> list[str]:
        return [
            self.db_list.item(i).text()
            for i in range(self.db_list.count())
            if self.db_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _update_summary(self) -> None:
        n = len(self.selected_databases())
        self.btn_start.setEnabled(n > 0 and self._job is None)
        if n == 0:
            self.summary.setText("Select the databases to back up.")
            self.btn_start.setText("Back up")
        elif n == 1:
            self.summary.setText("1 database selected")
            self.btn_start.setText("Back up 1 database")
        else:
            self.summary.setText(f"{n} databases, {n} separate files")
            self.btn_start.setText(f"Back up {n} databases")

    # -- run --------------------------------------------------------------

    def start(self) -> None:
        conn_id = self.connection.currentData()
        dbs = self.selected_databases()
        if not conn_id or not dbs:
            self._update_summary()
            return
        description = self.description.text().strip()
        tags = parse_tags(self.tags.text())
        ref = (
            _as_utc(self.ref_date.dateTime().toPython()) if self.use_ref_date.isChecked() else None
        )
        options = {
            "compress": self.compress.isChecked(),
            "compression": self.mssql_compress.isChecked(),
        }
        parallel = self.parallel.value()
        profile = self.app.connections.get(conn_id)

        def work(h: JobHandle) -> BackupRecord | BatchResult:
            def log(level: str, msg: str) -> None:
                h.log(msg, level)

            if len(dbs) == 1:
                return self.app.backups.backup_database(
                    BackupRequest(conn_id, dbs[0], description, tags, ref, options), h.ctx, log
                )
            return self.app.backups.backup_batch(
                conn_id, dbs, description, tags, ref, options, h.ctx, log, parallel
            )

        title = (
            f"Back up {profile.name}/{dbs[0]}"
            if len(dbs) == 1
            else f"Back up {len(dbs)} databases on {profile.name}"
        )
        self.result.show_message("")
        self._job = self.app.jobs.submit(JobKind.BACKUP, title, work, conn_id)
        self.progress.track(self._job)
        self.bridge.when_done(self._job, self._on_done)
        self._update_summary()

    def cancel(self) -> None:
        self.progress.cancel()

    def _on_done(self, job: Job) -> None:
        self._job = None
        self.progress.finish()
        self._update_summary()
        if job.status is JobStatus.SUCCEEDED:
            result = job.result
            if isinstance(result, BatchResult):
                lines = [
                    f"✓ {i.database}  {fmt_bytes(i.record.file_size if i.record else None)}"
                    if i.ok
                    else f"✗ {i.database}  {i.error or 'failed'}"
                    for i in result.items
                ]
                self.log.appendPlainText("\nBatch report\n" + "\n".join(lines))
                tone = "info" if not result.failed else "warning"
                self.result.show_message(
                    f"{len(result.succeeded)} succeeded, {len(result.failed)} failed.", tone
                )
            elif isinstance(result, BackupRecord):
                self.result.show_message(
                    f"Backup finished: {result.file_path.name} ({fmt_bytes(result.file_size)})",
                    "info",
                )
        elif job.status is JobStatus.CANCELLED:
            self.result.show_message("Backup cancelled.", "warning")
        else:
            self.result.show_message(f"Backup failed: {job.error}", "danger")
        self.backups_changed.emit()
