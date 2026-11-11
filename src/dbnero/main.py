"""Application entry point."""

from __future__ import annotations

import logging
import sys

from dbnero import APP_NAME, __version__
from dbnero.core.logging_setup import configure_logging
from dbnero.core.settings import load_settings, log_dir

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    configure_logging(log_dir())
    settings = load_settings()
    log.info("Starting %s %s", APP_NAME, __version__)

    # Imported lazily so the non-GUI modules stay importable without Qt.
    from PySide6.QtWidgets import QApplication

    from dbnero.storage.catalog import Catalog
    from dbnero.ui.main_window import MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    catalog = Catalog(settings.resolved_catalog_path())
    window = MainWindow(settings=settings, catalog=catalog)
    window.show()
    try:
        return app.exec()
    finally:
        catalog.close()


if __name__ == "__main__":
    raise SystemExit(main())
