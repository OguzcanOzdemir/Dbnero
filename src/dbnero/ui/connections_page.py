"""Connections: saved servers, an editor, connection test and database discovery."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.models import AuthType, ConnectionProfile, Engine, SslMode
from dbnero.ui.common import (
    Banner,
    Card,
    Collapsible,
    Page,
    button,
    combo,
    error_text,
    muted,
    run_async,
    show_error,
)

SSL_LABELS = {
    SslMode.DISABLE: "Off",
    SslMode.PREFER: "Use if available",
    SslMode.REQUIRE: "Required",
    SslMode.VERIFY_FULL: "Required, verify certificate",
}


class ConnectionsPage(Page):
    connections_changed = Signal()

    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(
            "Connections",
            "Servers dbnero can back up. Passwords are kept in your system keychain.",
            parent,
        )
        self.app = app
        self._current: ConnectionProfile | None = None
        self.btn_new = button("New connection", "primary", self.new_profile)
        self.header_actions.addWidget(self.btn_new)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.root.addLayout(body, 1)

        self.list = QListWidget()
        self.list.setFixedWidth(260)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(self._on_select)
        body.addWidget(self.list)

        right = QVBoxLayout()
        right.setSpacing(12)
        body.addLayout(right, 1)

        form_card = Card()
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. Production CRM")
        self.engine = combo()
        for e in Engine:
            self.engine.addItem(e.display_name, e.value)
        self.engine.currentIndexChanged.connect(self._on_engine_changed)
        self.host = QLineEdit()
        self.host.setPlaceholderText("hostname or IP")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setFixedWidth(110)
        host_row = QHBoxLayout()
        host_row.addWidget(self.host, 1)
        host_row.addWidget(self.port)
        self.auth = combo()
        self.auth.addItem("User name and password", AuthType.PASSWORD.value)
        self.auth.addItem("Windows authentication", AuthType.WINDOWS.value)
        self.auth.currentIndexChanged.connect(self._update_auth_fields)
        self.username = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.save_password = QCheckBox("Save password")
        self.save_password.setChecked(True)
        self.save_password.setToolTip(
            "Saved in your system keychain so you don't type it again.\n"
            "Untick to keep it only until dbnero closes."
        )
        self.password_hint = muted()
        form.addRow("Name", self.name)
        form.addRow("Engine", self.engine)
        form.addRow("Server", host_row)
        form.addRow("Sign in with", self.auth)
        form.addRow("User", self.username)
        form.addRow("Password", self.password)
        form.addRow("", self.save_password)
        form.addRow("", self.password_hint)
        form_card.body.addLayout(form)

        self.advanced = Collapsible("Advanced options")
        adv = QFormLayout()
        adv.setHorizontalSpacing(16)
        self.ssl = combo()
        for mode, label in SSL_LABELS.items():
            self.ssl.addItem(label, mode.value)
        self.default_db = QLineEdit()
        self.default_db.setPlaceholderText("Optional")
        adv.addRow("TLS / SSL", self.ssl)
        adv.addRow("Default database", self.default_db)
        self.advanced.content_layout.addLayout(adv)

        # SQL Server specifics live inside the advanced section.
        self.mssql_box = QWidget()
        mf = QFormLayout(self.mssql_box)
        mf.setContentsMargins(0, 8, 0, 0)
        mf.setHorizontalSpacing(16)
        self.odbc_driver = combo()
        self.odbc_driver.setEditable(True)
        self.trust_cert = QCheckBox("Trust the server certificate")
        self.server_dir = QLineEdit()
        self.server_dir.setPlaceholderText(r"As the server sees it, e.g. D:\Backups")
        self.local_dir = QLineEdit()
        self.local_dir.setPlaceholderText(r"Same folder from this computer, e.g. \\server\Backups")
        local_row = QHBoxLayout()
        local_row.addWidget(self.local_dir, 1)
        local_row.addWidget(button("Browse…", on_click=self._browse_local_dir))
        self.transfer = combo()
        self.transfer.addItem("Automatic", "auto")
        self.transfer.addItem("Shared folder", "share")
        self.transfer.addItem("Through SQL (OPENROWSET)", "sql")
        mf.addRow("ODBC driver", self.odbc_driver)
        mf.addRow("", self.trust_cert)
        mf.addRow("Server backup folder", self.server_dir)
        mf.addRow("Local path to it", local_row)
        mf.addRow("Transfer", self.transfer)
        mf.addRow(
            muted(
                "SQL Server writes .bak files to its own disk. Share that folder with this "
                "computer so backups can be copied into the archive and restored."
            )
        )
        self.advanced.content_layout.addWidget(self.mssql_box)
        form_card.body.addWidget(self.advanced)

        actions = QHBoxLayout()
        self.btn_test = button("Test connection", on_click=self.test_connection)
        self.btn_discover = button("List databases", on_click=self.discover)
        self.btn_delete = button("Delete", "danger", self.delete_profile)
        self.btn_save = button("Save", "primary", self.save_profile)
        actions.addWidget(self.btn_test)
        actions.addWidget(self.btn_discover)
        actions.addStretch(1)
        actions.addWidget(self.btn_delete)
        actions.addWidget(self.btn_save)
        form_card.body.addLayout(actions)
        right.addWidget(form_card)

        self.status_banner = Banner()
        self.status = self.status_banner.label
        right.addWidget(self.status_banner)

        db_card = Card("Databases")
        self.show_system = QCheckBox("Show system databases")
        self.show_system.toggled.connect(lambda _: self.discover())
        db_card.body.addWidget(self.show_system)
        self.databases = QListWidget()
        self.databases.setMinimumHeight(120)
        db_card.body.addWidget(self.databases, 1)
        right.addWidget(db_card, 1)

        self._load_drivers()
        self.reload()
        if self.list.count() == 0:
            self.new_profile()

    # -- data -----------------------------------------------------------

    def _engine(self) -> Engine:
        return Engine(self.engine.currentData())

    def _load_drivers(self) -> None:
        report = self.app.connections.check_tools(Engine.MSSQL)
        self.odbc_driver.clear()
        self.odbc_driver.addItem("(automatic)", "")
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
        self._load(self.app.connections.get(item.data(Qt.ItemDataRole.UserRole)))

    def _load(self, p: ConnectionProfile) -> None:
        self._current = p
        self.name.setText(p.name)
        self.engine.blockSignals(True)
        self.engine.setCurrentIndex(self.engine.findData(p.engine.value))
        self.engine.blockSignals(False)
        self.host.setText(p.host)
        self.port.setValue(p.port)
        self.auth.setCurrentIndex(self.auth.findData(p.auth_type.value))
        self.username.setText(p.username)
        self.password.clear()
        has_pw = self.app.connections.has_password(p.id)
        session_only = has_pw and self.app.connections.password_is_session_only(p.id)
        can_save = self.app.connections.credential_store_available()
        self.save_password.setChecked(can_save and not session_only)
        self.save_password.setVisible(can_save)
        if session_only:
            hint = "The password is kept until you close dbnero. Tick Save password to keep it."
        elif has_pw:
            hint = "A password is saved. Type a new one to replace it."
        else:
            hint = ""
        if not can_save:
            hint += (
                " No system keychain was found, so passwords are only kept until you close dbnero."
            )
        self.password_hint.setText(hint.strip())
        self.password_hint.setVisible(bool(hint.strip()))
        self.ssl.setCurrentIndex(self.ssl.findData(p.ssl_mode.value))
        self.default_db.setText(p.default_database)
        driver = p.options.get("odbc_driver", "")
        idx = self.odbc_driver.findData(driver)
        if idx < 0:
            self.odbc_driver.addItem(driver, driver)
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
        self.status_banner.show_message("")
        self.btn_delete.setEnabled(any(c.id == p.id for c in self.app.connections.list_profiles()))
        self._update_engine_fields()

    def _on_engine_changed(self) -> None:
        self.port.setValue(self._engine().default_port)
        self._update_engine_fields()

    def _update_engine_fields(self) -> None:
        engine = self._engine()
        self.mssql_box.setVisible(engine is Engine.MSSQL)
        self.auth.setEnabled(engine is Engine.MSSQL)
        if engine is not Engine.MSSQL:
            self.auth.setCurrentIndex(self.auth.findData(AuthType.PASSWORD.value))
        self._update_auth_fields()

    def _update_auth_fields(self) -> None:
        windows = AuthType(self.auth.currentData()) is AuthType.WINDOWS
        self.username.setEnabled(not windows)
        self.password.setEnabled(not windows)
        self.save_password.setEnabled(not windows)

    def _browse_local_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Local path to the server backup folder")
        if path:
            self.local_dir.setText(path)

    def _driver_text(self) -> str:
        text = self.odbc_driver.currentText().strip()
        return "" if text in {"", "(automatic)"} else text

    def _form_profile(self) -> ConnectionProfile:
        base = self._current or ConnectionProfile(
            name="", engine=Engine.POSTGRESQL, host="", port=5432
        )
        engine = self._engine()
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

    # -- actions --------------------------------------------------------

    def new_profile(self) -> None:
        self.list.clearSelection()
        blank = ConnectionProfile(
            name="", engine=Engine.POSTGRESQL, host="localhost", port=Engine.POSTGRESQL.default_port
        )
        self._load(blank)
        self._current = None
        self.password_hint.setVisible(False)
        self.btn_delete.setEnabled(False)
        self.name.setFocus()

    def save_profile(self) -> None:
        profile = self._form_profile()
        password = self.password.text() or None
        try:
            self.app.connections.save(profile, password, remember=self.save_password.isChecked())
        except Exception as exc:
            show_error(self, "Could not save the connection", exc)
            return
        self._current = profile
        self.password.clear()
        self.reload(select_id=profile.id)
        self._load(profile)
        self.status_banner.show_message(f"Saved “{profile.name}”.", "info")
        self.connections_changed.emit()

    def delete_profile(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        profile = self.app.connections.get(item.data(Qt.ItemDataRole.UserRole))
        answer = QMessageBox.question(
            self,
            "Delete connection",
            f"Delete “{profile.name}” and its saved password?\n"
            "Backups taken with this connection stay in the archive.",
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
            self.status_banner.show_message(message, "info")

    def test_connection(self) -> None:
        profile = self._form_profile()
        password = self.password.text() or None
        self._set_busy(True, "Connecting…")

        def ok(version: str) -> None:
            self._set_busy(False)
            self.status_banner.show_message(
                f"Connection successful. Server version {version}", "info"
            )

        def fail(exc: BaseException) -> None:
            self._set_busy(False)
            self.status_banner.show_message(f"Connection failed: {error_text(exc)}", "danger")

        run_async(lambda: self.app.connections.test(profile, password), ok, fail)

    def discover(self) -> None:
        profile = self._form_profile()
        if not profile.host:
            return
        password = self.password.text() or None
        include_system = self.show_system.isChecked()
        self._set_busy(True, "Listing databases…")

        def work() -> tuple[list[str], list[str]]:
            return self.app.connections.discover(profile, password, include_system)

        def ok(result: tuple[list[str], list[str]]) -> None:
            user_dbs, all_dbs = result
            self._set_busy(False)
            self.databases.clear()
            for name in all_dbs:
                item = QListWidgetItem(name if name in user_dbs else f"{name}  (system)")
                if name not in user_dbs:
                    item.setForeground(Qt.GlobalColor.gray)
                self.databases.addItem(item)
            self.status_banner.show_message(f"Found {len(user_dbs)} user databases.", "info")

        def fail(exc: BaseException) -> None:
            self._set_busy(False)
            self.status_banner.show_message(
                f"Could not list databases: {error_text(exc)}", "danger"
            )

        run_async(work, ok, fail)
