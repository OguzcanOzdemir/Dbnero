"""Settings page and first-run tool check dialog."""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from dbnero.core.app import AppContext
from dbnero.core.connection_service import EngineToolReport, install_hint
from dbnero.core.models import Engine
from dbnero.ui.common import page_header, run_async

TOOL_NAMES = ("pg_dump", "pg_restore", "mysqldump", "mysql")


def tool_report_html(reports: list[EngineToolReport], keyring_ok: bool) -> str:
    rows = []
    for rep in reports:
        icon = "✔" if rep.ok else "✖"
        rows.append(f"<h3>{icon} {rep.engine.display_name}</h3><ul>")
        for t in rep.tools:
            if t.available:
                rows.append(f"<li>✔ <b>{t.name}</b> {t.version or ''} — <code>{t.path}</code></li>")
            else:
                rows.append(f"<li>✖ <b>{t.name}</b> bulunamadı</li>")
        if rep.engine is Engine.MSSQL:
            if rep.odbc_drivers:
                rows.append("<li>✔ ODBC sürücüleri: " + ", ".join(rep.odbc_drivers) + "</li>")
            else:
                rows.append("<li>✖ SQL Server ODBC sürücüsü bulunamadı</li>")
        rows.append("</ul>")
        if not rep.ok:
            rows.append(f"<p><i>Kurulum:</i> {install_hint(rep.engine, sys.platform)}</p>")
    rows.append(
        "<h3>"
        + ("✔" if keyring_ok else "✖")
        + " Parola kasası</h3>"
        + (
            "<p>İşletim sistemi anahtar kasası kullanılabilir.</p>"
            if keyring_ok
            else "<p>Güvenli parola kasası bulunamadı. Linux'ta GNOME Keyring veya KWallet "
            "kurup oturumda kilidini açın. Kasa olmadan parolalar kaydedilemez.</p>"
        )
    )
    rows.append(
        "<p>Not: Yalnızca kullanacağınız motorların araçları gereklidir. PostgreSQL için "
        "<code>pg_dump</code> sürümü sunucu sürümüyle aynı veya daha yeni olmalıdır.</p>"
    )
    return "\n".join(rows)


class ToolCheckDialog(QDialog):
    """First-run wizard: checks client tools, ODBC drivers and the keyring."""

    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("dbnero — Ortam kontrolü")
        self.resize(640, 520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "dbnero, veritabanı motorlarının kendi yedekleme araçlarını kullanır. Bu araçlar "
            "uygulamayla birlikte gelmez; aşağıda bu bilgisayarda bulunanlar listelenmiştir. "
            "Araç yollarını daha sonra Ayarlar ekranından değiştirebilirsiniz."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        layout.addWidget(self.browser, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        recheck = QPushButton("Yeniden kontrol et")
        buttons.addButton(recheck, QDialogButtonBox.ButtonRole.ActionRole)
        recheck.clicked.connect(self.refresh)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.refresh()

    def refresh(self) -> None:
        self.browser.setHtml("Kontrol ediliyor…")
        run_async(
            self.app.connections.check_all_tools,
            lambda reports: self.browser.setHtml(
                tool_report_html(reports, self.app.connections.credential_store_available())
            ),
            lambda exc: self.browser.setPlainText(str(exc)),
        )


class SettingsPage(QWidget):
    def __init__(self, app: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app = app
        s = app.settings
        layout = QVBoxLayout(self)
        layout.addWidget(page_header("Ayarlar"))

        storage = QGroupBox("Arşiv")
        sf = QFormLayout(storage)
        self.archive_root = QLineEdit(str(s.resolved_archive_root()))
        browse = QPushButton("Gözat…")
        browse.clicked.connect(self._browse_archive)
        arow = QHBoxLayout()
        arow.addWidget(self.archive_root, 1)
        arow.addWidget(browse)
        sf.addRow("Arşiv klasörü", arow)
        self.min_free = QSpinBox()
        self.min_free.setRange(0, 1_000_000)
        self.min_free.setSuffix(" MB")
        self.min_free.setValue(s.min_free_space_mb)
        sf.addRow("Yedek sonrası en az boş alan", self.min_free)
        self.disk_warn = QSpinBox()
        self.disk_warn.setRange(50, 100)
        self.disk_warn.setSuffix(" %")
        self.disk_warn.setValue(s.disk_warning_percent)
        sf.addRow("Disk doluluk uyarısı", self.disk_warn)
        catalog = QLabel(str(s.resolved_catalog_path()))
        catalog.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        sf.addRow("Katalog dosyası", catalog)
        layout.addWidget(storage)

        retention = QGroupBox("Saklama politikası (Yedek Arşivi ekranından onayla uygulanır)")
        rf = QFormLayout(retention)
        self.keep_last = QSpinBox()
        self.keep_last.setRange(0, 10_000)
        self.keep_last.setSpecialValueText("Kapalı")
        self.keep_last.setValue(s.retention_keep_last)
        self.max_age = QSpinBox()
        self.max_age.setRange(0, 36_500)
        self.max_age.setSpecialValueText("Kapalı")
        self.max_age.setSuffix(" gün")
        self.max_age.setValue(s.retention_max_age_days)
        rf.addRow("Veritabanı başına son N yedeği sakla", self.keep_last)
        rf.addRow("En fazla yaş", self.max_age)
        layout.addWidget(retention)

        perf = QGroupBox("Performans")
        pf = QFormLayout(perf)
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(s.max_parallel_jobs)
        pf.addRow("Toplu yedekte eşzamanlı veritabanı", self.parallel)
        layout.addWidget(perf)

        tools = QGroupBox("Araç konumları (boş = PATH'te ara)")
        tf = QFormLayout(tools)
        self.tool_edits: dict[str, QLineEdit] = {}
        for name in TOOL_NAMES:
            edit = QLineEdit(s.tool_paths.get(name, ""))
            btn = QPushButton("…")
            btn.setMaximumWidth(32)
            btn.clicked.connect(lambda _=False, e=edit, n=name: self._browse_tool(e, n))
            r = QHBoxLayout()
            r.addWidget(edit, 1)
            r.addWidget(btn)
            tf.addRow(name, r)
            self.tool_edits[name] = edit
        check = QPushButton("Araçları kontrol et…")
        check.clicked.connect(lambda: ToolCheckDialog(self.app, self).exec())
        tf.addRow(check)
        layout.addWidget(tools)

        buttons = QHBoxLayout()
        save = QPushButton("Kaydet")
        save.clicked.connect(self.save)
        buttons.addStretch(1)
        buttons.addWidget(save)
        layout.addLayout(buttons)
        layout.addStretch(1)

    def _browse_archive(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Arşiv klasörü", self.archive_root.text())
        if path:
            self.archive_root.setText(path)

    def _browse_tool(self, edit: QLineEdit, name: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"{name} konumu")
        if path:
            edit.setText(path)

    def save(self) -> None:
        s = self.app.settings
        new_root = self.archive_root.text().strip()
        if new_root and new_root != str(s.resolved_archive_root()):
            if self.app.jobs.active_jobs():
                QMessageBox.warning(
                    self, "Ayarlar", "Çalışan işler varken arşiv klasörü değiştirilemez."
                )
                return
            QMessageBox.information(
                self,
                "Arşiv klasörü",
                "Yeni yedekler bu klasöre yazılacak. Mevcut yedekler eski konumlarında kalır; "
                "taşırsanız Yedek Arşivi ekranından 'Arşivden içe aktar' ile yeniden tanıtın.",
            )
        s.archive_root = new_root
        s.min_free_space_mb = self.min_free.value()
        s.disk_warning_percent = self.disk_warn.value()
        s.retention_keep_last = self.keep_last.value()
        s.retention_max_age_days = self.max_age.value()
        s.max_parallel_jobs = self.parallel.value()
        s.tool_paths = {n: e.text().strip() for n, e in self.tool_edits.items() if e.text().strip()}
        self.app.save_settings()
        QMessageBox.information(self, "Ayarlar", "Ayarlar kaydedildi.")
