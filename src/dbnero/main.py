"""Application entry point."""

from __future__ import annotations

import logging
import sys

from dbnero import APP_NAME, __version__
from dbnero.core.logging_setup import configure_logging
from dbnero.core.settings import load_settings, log_dir, settings_path

log = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    configure_logging(log_dir())
    settings = load_settings()
    log.info("Starting %s %s", APP_NAME, __version__)

    # Imported lazily so the non-GUI modules stay importable without Qt.
    from PySide6.QtWidgets import QApplication, QMessageBox

    from dbnero.core.app import AppContext
    from dbnero.storage.migrations import CatalogTooNew
    from dbnero.ui.main_window import MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)

    try:
        context = AppContext.create(settings, settings_path=settings_path())
    except CatalogTooNew as exc:
        QMessageBox.critical(None, APP_NAME, str(exc))
        return 1

    window = MainWindow(context)
    window.show()
    if "--no-first-run" not in argv:
        window.maybe_show_first_run()
    try:
        return app.exec()
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
