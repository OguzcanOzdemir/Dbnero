from dbnero.core.settings import Settings
from dbnero.ui.main_window import PAGES, MainWindow


def test_main_window_navigation(qtbot, catalog):
    w = MainWindow(Settings(), catalog)
    qtbot.addWidget(w)
    assert w.nav.count() == len(PAGES)
    assert w.current_page_key() == "overview"
    w.show_page("connections")
    assert w.current_page_key() == "connections"
