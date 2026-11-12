"""Terms of Use and Privacy Policy shipped with the app.

The same text files are shown on first start (and accepted once per
``LEGAL_VERSION``), on the Settings page, in the Windows installer and on the
website (``site/terms.html``, ``site/privacy.html``).
"""

from __future__ import annotations

from pathlib import Path

# Bump when TERMS.txt or PRIVACY.txt change in substance: users accept again.
LEGAL_VERSION = "1"

_DIR = Path(__file__).parent


def terms_text() -> str:
    return (_DIR / "TERMS.txt").read_text("utf-8")


def privacy_text() -> str:
    return (_DIR / "PRIVACY.txt").read_text("utf-8")
