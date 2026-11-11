"""Connections page: profile list, editor, connection test and DB discovery."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.connection_service import ValidationError
from dbnero.core.models import AuthType, ConnectionProfile, Engine, SslMode
from dbnero.ui.common import error_text, page_header, run_async, show_error

SSL_LABELS = {
    SslMode.DISABLE: "Kapalı",
    SslMode.PREFER: "Varsa kullan",
    SslMode.REQUIRE: "Zorunlu",
    SslMode.VERIFY_FULL: "Zorunlu + sertifika doğrula",
}


class ConnectionsPage(QWidget):
    connections_changed = Signal()

    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self._current: ConnectionProfile | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            page_header(
                "Bağlantılar",
                "Sunucu bağlantı profilleri. Parolalar işletim sisteminin anahtar "
                "kasasında saklanır.",
            )
        )
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        # Left: list
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_select)
        lv.addWidget(self.list, 1)
        btns = QHBoxLayout()
        self.btn_new = QPushButton("Yeni")
        self.btn_delete = QPushButton("Sil")
        self.btn_new.clicked.connect(self.new_profile)
        self.btn_delete.clicked.connect(self.delete_profile)
        btns.addWidget(self.btn_new)
        btns.addWidget(self.btn_delete)
        lv.addLayout(btns)
        splitter.addWidget(left)

        # Right: editor
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(8, 0, 0, 0)
        form_box = QGroupBox("Bağlantı ayrıntıları")
        form = QFormLayout(form_box)
        self.name = QLineEdit()
        self.engine = QComboBox()
        for e in Engine:
            self.engine.addItem(e.display_name, e)
        self.engine.currentIndexChanged.connect(self._on_engine_changed)
        self.host = QLineEdit()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.auth = QComboBox()
        self.auth.addItem("Kullanıcı adı / parola", AuthType.PASSWORD)
        self.auth.addItem("Windows kimlik doğrulaması", AuthType.WINDOWS)
        self.auth.currentIndexChanged.connect(self._update_auth_fields)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_hint = QLabel()
        self.password_hint.setStyleSheet("color: palette(mid);")
        self.ssl = QComboBox()
        for mode, label in SSL_LABELS.items():
            self.ssl.addItem(label, mode)
        self.default_db = QLineEdit()
        self.default_db.setPlaceholderText("İsteğe bağlı")
        form.addRow("Ad", self.name)
        form.addRow("Motor", self.engine)
        form.addRow("Sunucu", self.host)
        form.addRow("Port", self.port)
        form.addRow("Kimlik doğrulama", self.auth)
        form.addRow("Kullanıcı", self.username)
        form.addRow("Parola", self.password)
        form.addRow("", self.password_hint)
        form.addRow("TLS/SSL", self.ssl)
        form.addRow("Varsayılan veritabanı", self.default_db)
        rv.addWidget(form_box)

        # MSSQL-specific options
        self.mssql_box = QGroupBox("SQL Server seçenekleri")
        mf = QFormLayout(self.mssql_box)
        self.odbc_driver = QComboBox()
        self.odbc_driver.setEditable(True)
        self.trust_cert = QCheckBox("Sunucu sertifikasına güven (TrustServerCertificate)")
        self.server_dir = QLineEdit()
        self.server_dir.setPlaceholderText(
            "Sunucunun gördüğü yol, ör. D:\\Backups veya /var/opt/mssql/backup"
        )
        self.local_dir = QLineEdit()
        self.local_dir.setPlaceholderText(
            "Aynı klasörün bu bilgisayardaki yolu, ör. \\\\sunucu\\Backups"
        )
        browse = QPushButton("Gözat…")
        browse.clicked.connect(self._browse_local_dir)
        local_row = QHBoxLayout()
        local_row.addWidget(self.local_dir, 1)
        local_row.addWidget(browse)
        self.transfer = QComboBox()
        self.transfer.addItem("Otomatik", "auto")
        self.transfer.addItem("Paylaşılan klasör", "share")
        self.transfer.addItem("SQL üzerinden (OPENROWSET)", "sql")
        mf.addRow("ODBC sürücüsü", self.odbc_driver)
        mf.addRow("", self.trust_cert)
        mf.addRow("Sunucu yedek klasörü", self.server_dir)
        mf.addRow("Yerel erişim yolu", local_row)
        mf.addRow("Aktarım", self.transfer)
        note = QLabel(
            "BACKUP DATABASE dosyayı SQL Server'ın diskine yazar. Yerel arşive almak ve "
            "geri yüklemek için sunucu klasörünün bu bilgisayardan erişilebilir olması önerilir."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        mf.addRow(note)
        rv.addWidget(self.mssql_box)

        actions = QHBoxLayout()
        self.btn_save = QPushButton("Kaydet")
        self.btn_test = QPushButton("Bağlantıyı test et")
        self.btn_discover = QPushButton("Veritabanlarını listele")
        self.btn_save.clicked.connect(self.save_profile)
        self.btn_test.clicked.connect(self.test_connection)
        self.btn_discover.clicked.connect(self.discover)
        actions.addWidget(self.btn_save)
        actions.addWidget(self.btn_test)
        actions.addWidget(self.btn_discover)
        actions.addStretch(1)
        rv.addLayout(actions)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rv.addWidget(self.status)
        self.databases = QListWidget()
        self.show_system = QCheckBox("Sistem veritabanlarını göster")
        self.show_system.toggled.connect(lambda _: self.discover())
        rv.addWidget(self.show_system)
        rv.addWidget(self.databases, 1)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 3)

        self._load_drivers()
        self.reload()
        if self.list.count() == 0:
            self.new_profile()

    # -- data -----------------------------------------------------------

    def _load_drivers(self) -> None:
        report = self.app.connections.check_tools(Engine.MSSQL)
        self.odbc_driver.clear()
        self.odbc_driver.addItem("(otomatik)", "")
        for d in report.odbc_drivers:
            self.odbc_driver.addItem(d, d)

    def reload(self, select_id: str | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for p in self.app.connections.list_profiles():
            item = QListWidgetItem(f"{p.name}\n{p.engine.display_name} · {p.server_label}")
            item.setData(Qt.ItemDataRole.UserRole, p.id)
            self.list.addItem(item)
            if p.id == select_id:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        if select_id is None and self.list.count():
            self.list.setCurrentRow(0)
            self._on_select(self.list.currentItem(), None)

    def _on_select(self, item: QListWidgetItem | None, _prev: object) -> None:
        if item is None:
            return
        profile = self.app.connections.get(item.data(Qt.ItemDataRole.UserRole))
        self._load(profile)

    def _load(self, p: ConnectionProfile) -> None:
        self._current = p
        self.name.setText(p.name)
        self.engine.blockSignals(True)
        self.engine.setCurrentIndex(self.engine.findData(p.engine))
        self.engine.blockSignals(False)
        self.host.setText(p.host)
        self.port.setValue(p.port)
        self.auth.setCurrentIndex(self.auth.findData(p.auth_type))
        self.username.setText(p.username)
        self.password.clear()
        has_pw = self.app.connections.has_password(p.id)
        hint = "Kayıtlı parola var; değiştirmek için yazın." if has_pw else "Kayıtlı parola yok."
        if not self.app.connections.credential_store_available():
            hint += (
                " ⚠ Güvenli parola kasası bulunamadı: parolalar yalnızca bu oturum boyunca "
                "bellekte tutulur."
            )
        self.password_hint.setText(hint)
        self.ssl.setCurrentIndex(self.ssl.findData(p.ssl_mode))
        self.default_db.setText(p.default_database)
        idx = self.odbc_driver.findData(p.options.get("odbc_driver", ""))
        if idx < 0:
            self.odbc_driver.addItem(p.options["odbc_driver"], p.options["odbc_driver"])
            idx = self.odbc_driver.count() - 1
        self.odbc_driver.setCurrentIndex(idx)
        self.trust_cert.setChecked(
            p.options.get("trust_server_certificate", "") in {"true", "yes", "1"}
        )
        self.server_dir.setText(p.options.get("server_backup_dir", ""))
        self.local_dir.setText(p.options.get("local_backup_dir", ""))
        self.transfer.setCurrentIndex(
            max(0, self.transfer.findData(p.options.get("transfer", "auto")))
        )
        self.databases.clear()
        self.status.clear()
        self._update_engine_fields()

    def _on_engine_changed(self) -> None:
        engine = Engine(self.engine.currentData())
        self.port.setValue(engine.default_port)
        self._update_engine_fields()

    def _update_engine_fields(self) -> None:
        engine = Engine(self.engine.currentData())
        self.mssql_box.setVisible(engine is Engine.MSSQL)
        self.auth.setEnabled(engine is Engine.MSSQL)
        if engine is not Engine.MSSQL:
            self.auth.setCurrentIndex(self.auth.findData(AuthType.PASSWORD))
        self._update_auth_fields()

    def _update_auth_fields(self) -> None:
        windows = AuthType(self.auth.currentData()) is AuthType.WINDOWS
        self.username.setEnabled(not windows)
        self.password.setEnabled(not windows)

    def _browse_local_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Yerel erişim klasörü")
        if path:
            self.local_dir.setText(path)

    def _form_profile(self) -> ConnectionProfile:
        base = self._current or ConnectionProfile(
            name="", engine=Engine.POSTGRESQL, host="", port=5432
        )
        engine = Engine(self.engine.currentData())
        options = dict(base.options)
        if engine is Engine.MSSQL:
            for key, value in {
                "odbc_driver": self._driver_text(),
                "trust_server_certificate": "true" if self.trust_cert.isChecked() else "false",
                "server_backup_dir": self.server_dir.text().strip(),
                "local_backup_dir": self.local_dir.text().strip(),
                "transfer": self.transfer.currentData(),
            }.items():
                if value:
                    options[key] = value
                else:
                    options.pop(key, None)
        return ConnectionProfile(
            id=base.id,
            created_at_utc=base.created_at_utc,
            name=self.name.text().strip(),
            engine=engine,
            host=self.host.text().strip(),
            port=self.port.value(),
            username=self.username.text().strip(),
            auth_type=AuthType(self.auth.currentData()),
            ssl_mode=SslMode(self.ssl.currentData()),
            default_database=self.default_db.text().strip(),
            options=options,
        )

    def _driver_text(self) -> str:
        text = self.odbc_driver.currentText().strip()
        return "" if text in {"", "(otomatik)"} else text

    # -- actions --------------------------------------------------------

    def new_profile(self) -> None:
        self.list.clearSelection()
        self._current = None
        blank = ConnectionProfile(
            name="", engine=Engine.POSTGRESQL, host="localhost", port=Engine.POSTGRESQL.default_port
        )
        self._load(blank)
        self._current = None
        self.password_hint.setText("")
        self.name.setFocus()

    def save_profile(self) -> None:
        profile = self._form_profile()
        password = self.password.text() or None
        try:
            self.app.connections.save(profile, password)
        except ValidationError as exc:
            show_error(self, "Kaydedilemedi", exc)
            return
        except Exception as exc:
            show_error(self, "Kaydedilemedi", exc)
            return
        self._current = profile
        self.password.clear()
        self.reload(select_id=profile.id)
        self._load(profile)
        self.status.setText("Kaydedildi.")
        self.connections_changed.emit()

    def delete_profile(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        profile = self.app.connections.get(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self,
            "Bağlantıyı sil",
            f"'{profile.name}' bağlantısı ve kayıtlı parolası silinsin mi?\n"
            "Bu bağlantıyla alınmış yedekler arşivde kalır.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.app.connections.delete(profile.id)
        self.reload()
        if self.list.count() == 0:
            self.new_profile()
        self.connections_changed.emit()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        for b in (self.btn_test, self.btn_discover, self.btn_save):
            b.setEnabled(not busy)
        if message:
            self.status.setText(message)

    def test_connection(self) -> None:
        profile = self._form_profile()
        password = self.password.text() or None
        self._set_busy(True, "Bağlanılıyor…")

        def ok(version: str) -> None:
            self._set_busy(False)
            self.status.setText(f"✔ Bağlantı başarılı. Sunucu sürümü: {version}")

        def fail(exc: BaseException) -> None:
            self._set_busy(False)
            self.status.setText(f"✖ Bağlantı başarısız: {error_text(exc)}")

        run_async(lambda: self.app.connections.test(profile, password), ok, fail)

    def discover(self) -> None:
        profile = self._form_profile()
        if not profile.host:
            return
        password = self.password.text() or None
        include_system = self.show_system.isChecked()
        self._set_busy(True, "Veritabanları listeleniyor…")

        def work() -> tuple[list[str], list[str]]:
            adapter = self.app.connections.adapter_for(profile, password)
            user_dbs = adapter.list_databases(False)
            all_dbs = adapter.list_databases(True) if include_system else user_dbs
            return user_dbs, all_dbs

        def ok(result: tuple[list[str], list[str]]) -> None:
            user_dbs, all_dbs = result
            self._set_busy(False)
            self.databases.clear()
            for name in all_dbs:
                item = QListWidgetItem(name if name in user_dbs else f"{name}  (sistem)")
                if name not in user_dbs:
                    item.setForeground(Qt.GlobalColor.gray)
                self.databases.addItem(item)
            self.status.setText(f"{len(user_dbs)} kullanıcı veritabanı bulundu.")

        def fail(exc: BaseException) -> None:
            self._set_busy(False)
            self.status.setText(f"✖ Listelenemedi: {error_text(exc)}")

        run_async(work, ok, fail)
