"""Settings page and the first-run environment check."""

from __future__ import annotations

import html
import sys
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from dbnero import APP_NAME, __version__
from dbnero.core.app import AppContext
from dbnero.core.connection_service import EngineToolReport, install_hint
from dbnero.core.models import Engine
from dbnero.ui.common import Card, Page, button, muted, run_async
from dbnero.ui.legal_dialog import LegalDialog

TOOL_NAMES = ("pg_dump", "pg_restore", "mysqldump", "mysql")


def tool_report_html(reports: list[EngineToolReport], keyring_ok: bool) -> str:
    e = html.escape
    rows = []
    for rep in reports:
        mark = "✓" if rep.ok else "✗"
        rows.append(f"<h3>{mark} {e(rep.engine.display_name)}</h3><ul>")
        for t in rep.tools:
            if t.available:
                rows.append(
                    f"<li>✓ <b>{e(t.name)}</b> {e(t.version or '')} — "
                    f"<code>{e(str(t.path))}</code></li>"
                )
            else:
                rows.append(f"<li>✗ <b>{e(t.name)}</b> not found</li>")
        if rep.engine is Engine.MSSQL:
            if rep.odbc_drivers:
                rows.append("<li>✓ ODBC drivers: " + e(", ".join(rep.odbc_drivers)) + "</li>")
            else:
                rows.append("<li>✗ No ODBC driver for SQL Server found</li>")
        rows.append("</ul>")
        if not rep.ok:
            rows.append(
                f"<p><i>How to install:</i> {e(install_hint(rep.engine, sys.platform))}</p>"
            )
    rows.append(
        "<h3>"
        + ("✓" if keyring_ok else "✗")
        + " Password storage</h3>"
        + (
            "<p>Your system keychain is available.</p>"
            if keyring_ok
            else "<p>No system keychain was found. On Linux, install and unlock GNOME Keyring "
            "or KWallet. Without it, passwords are only kept until dbnero closes.</p>"
        )
    )
    rows.append(
        "<p>dbnero ships with the PostgreSQL and MySQL client tools, so nothing needs to "
        "be installed for them. SQL Server uses the ODBC driver of your system; Windows "
        "includes one.</p>"
    )
    return "\n".join(rows)


class ToolCheckDialog(QDialog):
    """First-run check of client tools, ODBC drivers and the keychain."""

    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Welcome to dbnero")
        self.resize(620, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        title = QLabel("Check your setup")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        layout.addWidget(
            muted(
                "dbnero uses each database's own backup tools. They are not bundled, so here "
                "is what this computer has. You can change tool locations later in Settings."
            )
        )
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        recheck = button("Check again", on_click=self.refresh)
        buttons.addButton(recheck, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.browser.setHtml("Checking…")
        run_async(
            self.app.connections.check_all_tools,
            lambda reports: self.browser.setHtml(
                tool_report_html(reports, self.app.connections.credential_store_available())
            ),
            lambda exc: self.browser.setPlainText(str(exc)),
        )


class SettingsPage(Page):
    theme_changed = Signal(str)

    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__("Settings", "", parent)
        self.app = app
        s = app.settings
        self.btn_save = button("Save", "primary", self.save)
        self.header_actions.addWidget(self.btn_save)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner.setObjectName("page")
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(12)
        scroll.setWidget(inner)
        self.root.addWidget(scroll, 1)

        def form_card(title: str) -> tuple[Card, QFormLayout]:
            card = Card(title)
            form = QFormLayout()
            form.setHorizontalSpacing(18)
            form.setVerticalSpacing(10)
            card.body.addLayout(form)
            col.addWidget(card)
            return card, form

        _, appearance = form_card("Appearance")
        self.theme = QComboBox()
        for value, label in (("system", "Match system"), ("light", "Light"), ("dark", "Dark")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(self.theme.findData(s.theme), 0))
        appearance.addRow("Theme", self.theme)

        _, storage = form_card("Archive")
        self.archive_root = QLineEdit(str(s.resolved_archive_root()))
        arow = QHBoxLayout()
        arow.addWidget(self.archive_root, 1)
        arow.addWidget(button("Browse…", on_click=self._browse_archive))
        storage.addRow("Archive folder", arow)
        self.min_free = QSpinBox()
        self.min_free.setRange(0, 1_000_000)
        self.min_free.setSuffix(" MB")
        self.min_free.setValue(s.min_free_space_mb)
        storage.addRow("Keep free after a backup", self.min_free)
        self.disk_warn = QSpinBox()
        self.disk_warn.setRange(50, 100)
        self.disk_warn.setSuffix(" %")
        self.disk_warn.setValue(s.disk_warning_percent)
        storage.addRow("Warn when the disk is fuller than", self.disk_warn)
        catalog = QLabel(str(s.resolved_catalog_path()))
        catalog.setObjectName("muted")
        catalog.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        storage.addRow("Catalog file", catalog)

        _, retention = form_card("Clean-up rules")
        self.keep_last = QSpinBox()
        self.keep_last.setRange(0, 10_000)
        self.keep_last.setSpecialValueText("Keep all")
        self.keep_last.setValue(s.retention_keep_last)
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 36_500)
        self.max_age.setSpecialValueText("No limit")
        self.max_age.setSuffix(" days")
        self.max_age.setValue(s.retention_max_age_days)
        retention.addRow("Backups to keep per database", self.keep_last)
        retention.addRow("Delete backups older than", self.max_age)
        retention.addRow(
            muted("Rules are applied only when you choose Clean up on the Backups page.")
        )

        _, perf = form_card("Performance")
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(s.max_parallel_jobs)
        perf.addRow("Databases backed up at once", self.parallel)

        tools_card, tools = form_card("Database tools")
        self.tool_edits: dict[str, QLineEdit] = {}
        for name in TOOL_NAMES:
            edit = QLineEdit(s.tool_paths.get(name, ""))
            edit.setPlaceholderText("Automatic (bundled with dbnero)")
            r = QHBoxLayout()
            r.addWidget(edit, 1)
            r.addWidget(button("…", on_click=self._tool_browser(edit, name)))
            tools.addRow(name, r)
            self.tool_edits[name] = edit
        tools_card.body.addWidget(
            button("Check tools…", on_click=lambda: ToolCheckDialog(self.app, self).exec()),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        about_card, about = form_card("About")
        about.addRow("Version", QLabel(f"{APP_NAME} {__version__}"))
        accepted = s.legal_accepted_at.replace("T", " ")[:16]
        about.addRow(
            "Terms accepted",
            QLabel(f"{accepted} UTC" if accepted else "Not yet"),
        )
        about_card.body.addWidget(
            button(
                "Terms of Use and Privacy Policy…",
                on_click=lambda: LegalDialog(require_acceptance=False, parent=self).exec(),
            ),
            0,
            Qt.AlignmentFlag.AlignLeft,
        )
        col.addStretch(1)

    def _browse_archive(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Archive folder", self.archive_root.text())
        if path:
            self.archive_root.setText(path)

    def _tool_browser(self, edit: QLineEdit, name: str) -> Callable[[], None]:
        return lambda: self._browse_tool(edit, name)

    def _browse_tool(self, edit: QLineEdit, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Location of {name}")
        if path:
            edit.setText(path)

    def save(self) -> None:
        s = self.app.settings
        new_root = self.archive_root.text().strip()
        if new_root and new_root != str(s.resolved_archive_root()):
            if self.app.jobs.active_jobs():
                QMessageBox.warning(
                    self, "Settings", "Wait until running jobs finish before moving the archive."
                )
                return
            QMessageBox.information(
                self,
                "Archive folder",
                "New backups will be written to this folder. Existing backups stay where they "
                "are; if you move them, use Import from folder on the Backups page.",
            )
        s.archive_root = new_root
        s.min_free_space_mb = self.min_free.value()
        s.disk_warning_percent = self.disk_warn.value()
        s.retention_keep_last = self.keep_last.value()
        s.retention_max_age_days = self.max_age.value()
        s.max_parallel_jobs = self.parallel.value()
        s.tool_paths = {n: e.text().strip() for n, e in self.tool_edits.items() if e.text().strip()}
        theme = str(self.theme.currentData())
        changed_theme = theme != s.theme
        s.theme = theme
        self.app.save_settings()
        if changed_theme:
            self.theme_changed.emit(theme)
        QMessageBox.information(self, "Settings", "Settings saved.")
