"""Application entry point."""

from __future__ import annotations

import logging
import sys
from typing import Any

from dbnero import APP_NAME, __version__
from dbnero.core.logging_setup import configure_logging, mask_secrets
from dbnero.core.settings import load_settings, log_dir, settings_path

log = logging.getLogger(__name__)


def install_error_dialog(message_box: Any) -> None:
    """Show unexpected errors to the user and log them instead of crashing."""

    def hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.error("Unexpected error", exc_info=(exc_type, exc, tb))
        text = mask_secrets(str(exc)) or exc_type.__name__
        message_box.critical(
            None,
            APP_NAME,
            f"Something went wrong: {text}\n\nDetails were written to the log in {log_dir()}.",
        )

    sys.excepthook = hook


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
    app.setApplicationDisplayName(APP_NAME)
    from PySide6.QtGui import QIcon

    from dbnero.ui.icons import app_icon_path

    app.setWindowIcon(QIcon(str(app_icon_path())))
    from dbnero.ui.theme import ThemeMode, apply_theme

    try:
        mode = ThemeMode(settings.theme)
    except ValueError:
        mode = ThemeMode.SYSTEM
    apply_theme(app, mode)

    install_error_dialog(QMessageBox)

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
