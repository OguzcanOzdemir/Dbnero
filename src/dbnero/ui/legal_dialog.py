"""Terms of Use and Privacy Policy: accepted once on first start, readable
any time from Settings."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dbnero import APP_NAME
from dbnero.legal import privacy_text, terms_text
from dbnero.ui.common import button, muted


def _text_view(text: str) -> QPlainTextEdit:
    view = QPlainTextEdit(text)
    view.setReadOnly(True)
    view.setMinimumHeight(320)
    return view


class LegalDialog(QDialog):
    """``require_acceptance=True``: Accept (after ticking the box) or Quit."""

    def __init__(self, require_acceptance: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(
            f"Welcome to {APP_NAME}" if require_acceptance else "Terms of Use and Privacy"
        )
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        if require_acceptance:
            heading = QLabel("Before you start")
            heading.setObjectName("pageTitle")
            layout.addWidget(heading)
            layout.addWidget(
                muted(
                    f"{APP_NAME} is free software provided without warranty. You are responsible "
                    "for checking that your backups work and for keeping extra copies: the "
                    "authors are not liable for lost data. Please read and accept the terms."
                )
            )
        self.tabs = QTabWidget()
        self.tabs.addTab(_text_view(terms_text()), "Terms of Use")
        self.tabs.addTab(_text_view(privacy_text()), "Privacy Policy")
        layout.addWidget(self.tabs, 1)

        actions = QHBoxLayout()
        if require_acceptance:
            self.accept_box = QCheckBox(
                "I have read and accept the Terms of Use and the Privacy Policy"
            )
            self.btn_accept = button("Accept and continue", "primary", self.accept)
            self.btn_accept.setEnabled(False)
            self.accept_box.toggled.connect(self.btn_accept.setEnabled)
            layout.addWidget(self.accept_box)
            actions.addStretch(1)
            actions.addWidget(button("Quit", on_click=self.reject))
            actions.addWidget(self.btn_accept)
        else:
            actions.addStretch(1)
            actions.addWidget(button("Close", "primary", self.accept))
        layout.addLayout(actions)
