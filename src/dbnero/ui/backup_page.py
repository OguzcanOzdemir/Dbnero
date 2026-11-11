"""New backup page: single or batch backup of selected databases."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QDateTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.backup_service import BackupRequest, BatchResult
from dbnero.core.jobs import Job, JobHandle
from dbnero.core.models import BackupRecord, Engine, JobKind, JobStatus
from dbnero.ui.common import JobBridge, error_text, fmt_bytes, page_header, run_async


def _as_utc(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("Expected a datetime")
    return value.astimezone(UTC)


def parse_tags(text: str) -> list[str]:
    return [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]


class BackupPage(QWidget):
    backups_changed = Signal()

    def __init__(self, app: AppContext, bridge: JobBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.bridge = bridge
        self._job: Job | None = None
        self._preselect: set[str] = set()

        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Yeni Yedek",
                "Bir veya birden fazla veritabanı seçin. Her veritabanı ayrı bir dosyaya ve "
                "ayrı bir katalog kaydına yedeklenir.",
            )
        )

        top = QHBoxLayout()
        self.connection = QComboBox()
        self.connection.currentIndexChanged.connect(self.load_databases)
        self.btn_refresh = QPushButton("Yenile")
        self.btn_refresh.clicked.connect(self.load_databases)
        top.addWidget(QLabel("Bağlantı:"))
        top.addWidget(self.connection, 1)
        top.addWidget(self.btn_refresh)
        layout.addLayout(top)

        body = QHBoxLayout()
        db_box = QGroupBox("Veritabanları")
        dbl = QVBoxLayout(db_box)
        self.db_list = QListWidget()
        self.db_list.itemChanged.connect(self._update_summary)
        sel = QHBoxLayout()
        b_all = QPushButton("Tümünü seç")
        b_none = QPushButton("Seçimi kaldır")
        b_all.clicked.connect(lambda: self._check_all(True))
        b_none.clicked.connect(lambda: self._check_all(False))
        sel.addWidget(b_all)
        sel.addWidget(b_none)
        sel.addStretch(1)
        dbl.addWidget(self.db_list, 1)
        dbl.addLayout(sel)
        body.addWidget(db_box, 1)

        opt_box = QGroupBox("Ayrıntılar")
        form = QFormLayout(opt_box)
        self.description = QLineEdit()
        self.description.setPlaceholderText("ör. Sürüm 2.4 öncesi")
        self.tags = QLineEdit()
        self.tags.setPlaceholderText("virgülle ayırın, ör. prod, haftalık")
        self.use_ref_date = QCheckBox("Referans tarihi ekle")
        self.ref_date = QDateTimeEdit(QDateTime.currentDateTime())
        self.ref_date.setCalendarPopup(True)
        self.ref_date.setEnabled(False)
        self.use_ref_date.toggled.connect(self.ref_date.setEnabled)
        self.compress = QCheckBox("Sıkıştır (MySQL .sql.gz)")
        self.compress.setChecked(True)
        self.mssql_compress = QCheckBox("SQL Server yedek sıkıştırması (Express desteklemez)")
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(self.app.settings.max_parallel_jobs)
        form.addRow("Açıklama", self.description)
        form.addRow("Etiketler", self.tags)
        form.addRow(self.use_ref_date, self.ref_date)
        form.addRow("", self.compress)
        form.addRow("", self.mssql_compress)
        form.addRow("Eşzamanlı yedek", self.parallel)
        note = QLabel(
            "Gerçek oluşturma zamanı sistem tarafından UTC olarak kaydedilir ve değiştirilemez. "
            "Referans tarihi yalnızca ek bilgi içindir.\n\n"
            "Not: Farklı veritabanlarının toplu yedekleri arasında ortak bir tutarlılık anı "
            "garanti edilmez."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        form.addRow(note)
        body.addWidget(opt_box, 1)
        layout.addLayout(body, 1)

        actions = QHBoxLayout()
        self.summary = QLabel()
        self.btn_start = QPushButton("Yedeklemeyi başlat")
        self.btn_start.setDefault(True)
        self.btn_cancel = QPushButton("İptal")
        self.btn_cancel.setEnabled(False)
        self.btn_start.clicked.connect(self.start)
        self.btn_cancel.clicked.connect(self.cancel)
        actions.addWidget(self.summary, 1)
        actions.addWidget(self.btn_start)
        actions.addWidget(self.btn_cancel)
        layout.addLayout(actions)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setPlaceholderText("İşlem günlüğü")
        layout.addWidget(self.log, 1)

        bridge.progress.connect(self._on_progress)
        bridge.logged.connect(self._on_log)
        self.reload_connections()

    # -- connections / databases -----------------------------------------

    def reload_connections(self) -> None:
        current = self.connection.currentData()
        self.connection.blockSignals(True)
        self.connection.clear()
        for p in self.app.connections.list_profiles():
            self.connection.addItem(f"{p.name} ({p.engine.display_name})", p.id)
        idx = self.connection.findData(current)
        self.connection.setCurrentIndex(idx if idx >= 0 else 0)
        self.connection.blockSignals(False)
        self.load_databases()

    def select(self, connection_id: str, databases: list[str] | None = None) -> None:
        idx = self.connection.findData(connection_id)
        if idx >= 0:
            self.connection.setCurrentIndex(idx)
        self._preselect = set(databases or [])

    def load_databases(self) -> None:
        conn_id = self.connection.currentData()
        self.db_list.clear()
        if not conn_id:
            self.summary.setText("Önce bir bağlantı ekleyin.")
            return
        profile = self.app.connections.get(conn_id)
        self.compress.setVisible(profile.engine is Engine.MYSQL)
        self.mssql_compress.setVisible(profile.engine is Engine.MSSQL)
        self.summary.setText("Veritabanları yükleniyor…")

        def ok(names: list[str]) -> None:
            if self.connection.currentData() != conn_id:
                return
            preselect = self._preselect
            self.db_list.blockSignals(True)
            self.db_list.clear()
            for name in names:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if name in preselect else Qt.CheckState.Unchecked
                )
                self.db_list.addItem(item)
            self.db_list.blockSignals(False)
            self._preselect = set()
            self._update_summary()

        def fail(exc: BaseException) -> None:
            self.summary.setText(f"✖ Veritabanları listelenemedi: {error_text(exc)}")

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
        if n == 0:
            self.summary.setText("Yedeklenecek veritabanı seçin.")
        elif n == 1:
            self.summary.setText("Tekil yedek: 1 veritabanı")
        else:
            self.summary.setText(f"Toplu yedek: {n} veritabanı, {n} ayrı dosya")

    # -- run --------------------------------------------------------------

    def _options(self) -> dict[str, object]:
        return {
            "compress": self.compress.isChecked(),
            "compression": self.mssql_compress.isChecked(),
        }

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
        options = self._options()
        parallel = self.parallel.value()
        profile = self.app.connections.get(conn_id)

        def work(h: JobHandle) -> BackupRecord | BatchResult:
            log = lambda level, msg: h.log(msg, level)  # noqa: E731
            if len(dbs) == 1:
                return self.app.backups.backup_database(
                    BackupRequest(conn_id, dbs[0], description, tags, ref, options), h.ctx, log
                )
            return self.app.backups.backup_batch(
                conn_id, dbs, description, tags, ref, options, h.ctx, log, parallel
            )

        title = (
            f"Yedek: {profile.name}/{dbs[0]}"
            if len(dbs) == 1
            else f"Toplu yedek: {profile.name} ({len(dbs)} veritabanı)"
        )
        self.log.clear()
        self._job = self.app.jobs.submit(JobKind.BACKUP, title, work, conn_id)
        self.bridge.when_done(self._job, self._on_done)
        self._set_running(True)

    def cancel(self) -> None:
        if self._job is not None:
            self.app.jobs.cancel(self._job.job_id)
            self.btn_cancel.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.progress.setVisible(running)
        self.progress.setRange(0, 0)

    def _on_progress(self, job: Job, fraction: float | None, message: str) -> None:
        if self._job is None or job.job_id != self._job.job_id:
            return
        if fraction is None:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(fraction * 100))
        self.progress.setFormat(message[:80] + " %p%")

    def _on_log(self, job: Job, level: str, message: str) -> None:
        if self._job is not None and job.job_id == self._job.job_id:
            prefix = {"WARNING": "⚠ ", "ERROR": "✖ "}.get(level, "")
            stamp = datetime.now().strftime("%H:%M:%S")
            self.log.appendPlainText(f"{stamp}  {prefix}{message}")

    def _on_done(self, job: Job) -> None:
        self._set_running(False)
        self._job = None
        if job.status is JobStatus.SUCCEEDED:
            result = job.result
            if isinstance(result, BatchResult):
                lines = [
                    f"✔ {i.database}: {fmt_bytes(i.record.file_size if i.record else None)}"
                    if i.ok
                    else f"✖ {i.database}: {i.error or 'başarısız'}"
                    for i in result.items
                ]
                self.log.appendPlainText("\nToplu yedek raporu:\n" + "\n".join(lines))
                self.summary.setText(
                    f"{len(result.succeeded)} başarılı, {len(result.failed)} başarısız"
                )
            elif isinstance(result, BackupRecord):
                self.summary.setText(
                    f"✔ Yedek tamamlandı: {result.file_path.name} ({fmt_bytes(result.file_size)})"
                )
        elif job.status is JobStatus.CANCELLED:
            self.summary.setText("İşlem iptal edildi.")
        else:
            self.summary.setText(f"✖ Yedekleme başarısız: {job.error}")
        self.backups_changed.emit()
