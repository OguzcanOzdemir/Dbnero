"""Main window: navigation sidebar plus a stacked page area."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dbnero import APP_NAME, __version__
from dbnero.core.settings import Settings
from dbnero.storage.catalog import Catalog


@dataclass(frozen=True, slots=True)
class PageSpec:
    key: str
    title: str
    description: str


PAGES: tuple[PageSpec, ...] = (
    PageSpec("overview", "Genel Bakış", "Son yedekler, başarısız işler ve kullanılan alan."),
    PageSpec(
        "connections", "Bağlantılar", "Bağlantı profilleri, bağlantı testi ve veritabanı keşfi."
    ),
    PageSpec("new_backup", "Yeni Yedek", "Tekil veya toplu yedekleme."),
    PageSpec("archive", "Yedek Arşivi", "Arama, filtreleme, doğrulama ve geri yükleme."),
    PageSpec("restore", "Geri Yükle", "Uyumluluk kontrolleri, hedef seçimi ve risk onayı."),
    PageSpec("compare", "Karşılaştır", "Yedek–canlı veya yedek–yedek şema farkları."),
    PageSpec("jobs", "İş Geçmişi", "Devam eden ve biten işler, günlükler."),
    PageSpec("settings", "Ayarlar", "Araç konumları, arşiv klasörü ve performans."),
)


class PlaceholderPage(QWidget):
    """Temporary page until the real screen is implemented."""

    def __init__(self, spec: PageSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel(spec.title)
        title.setObjectName("pageTitle")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")
        body = QLabel(
            spec.description + "\n\n" + self.tr("Bu ekran henüz geliştirilme aşamasında.")
        )
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(title)
        layout.addWidget(body, 1)


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, catalog: Catalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.catalog = catalog
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1100, 720)

        self.nav = QListWidget()
        self.nav.setObjectName("navigation")
        self.nav.setFixedWidth(200)
        self.stack = QStackedWidget()
        self._page_index: dict[str, int] = {}

        for spec in PAGES:
            item = QListWidgetItem(spec.title)
            item.setData(Qt.ItemDataRole.UserRole, spec.key)
            self.nav.addItem(item)
            self._page_index[spec.key] = self.stack.addWidget(PlaceholderPage(spec))

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.nav)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(self.tr("Hazır"))

    def show_page(self, key: str) -> None:
        self.nav.setCurrentRow(self._page_index[key])

    def current_page_key(self) -> str:
        return PAGES[self.stack.currentIndex()].key
