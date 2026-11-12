"""Application entry point."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from dbnero import APP_NAME, __version__
from dbnero.core.logging_setup import configure_logging, mask_secrets
from dbnero.core.settings import load_settings, log_dir, settings_path

log = logging.getLogger(__name__)


def install_error_dialog(message_box: Any, log_file: Path | None = None) -> None:
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
            f"Something went wrong: {text}"
            + (f"\n\nDetails were written to {log_file}." if log_file else ""),
        )

    sys.excepthook = hook


def ensure_legal_accepted(settings: Any, dialog_factory: Any = None) -> bool:
    """Ask once (and again when the terms change) to accept the Terms of Use
    and Privacy Policy. Returns False when the user declines."""
    from dbnero.core.models import utc_now
    from dbnero.core.settings import save_settings
    from dbnero.legal import LEGAL_VERSION

    if settings.legal_accepted_version == LEGAL_VERSION:
        return True
    if dialog_factory is None:
        from dbnero.ui.legal_dialog import LegalDialog

        dialog_factory = LegalDialog
    if not dialog_factory(require_acceptance=True).exec():
        return False
    settings.legal_accepted_version = LEGAL_VERSION
    settings.legal_accepted_at = utc_now().isoformat(timespec="seconds")
    save_settings(settings, settings_path())
    return True


def run_due(settings_file: Path | None = None) -> int:
    """Headless: run the scheduled backups that are due, then exit. Started by
    the operating system's scheduler (see ``core.os_scheduler``)."""
    from dbnero.core.app import AppContext
    from dbnero.core.models import JobKind
    from dbnero.core.schedule_service import scheduler_lock

    settings = load_settings(settings_file)
    # Never mark other processes' running work as interrupted from here.
    context = AppContext.create(settings, settings_path=settings_file, recover=False)
    try:
        with scheduler_lock(context.scheduler_lock_path) as acquired:
            if not acquired:
                return 0  # the app (or another runner) is handling schedules now
            due = context.schedules.due()
            if not due:
                return 0
            title = "Scheduled: " + ", ".join(s.name for s in due)
            job = context.jobs.submit(
                JobKind.BACKUP,
                title,
                lambda h: context.schedules.run_due(h.ctx, lambda lvl, msg: h.log(msg, lvl)),
            )
            context.jobs.wait(job)
            return 0 if job.error is None else 1
    finally:
        context.close()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    log_file = configure_logging(log_dir())
    from dbnero.core.os_scheduler import RUN_DUE_FLAG

    if RUN_DUE_FLAG in argv:
        try:
            return run_due(settings_path())
        except Exception:
            log.exception("Scheduled run failed")
            return 1
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

    install_error_dialog(QMessageBox, log_file)

    if not ensure_legal_accepted(settings):
        log.info("Terms not accepted; quitting")
        return 0

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
