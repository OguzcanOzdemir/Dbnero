"""Design tokens and the application stylesheet.

Layout concept: an ink-black navigation rail ("nero") beside calm, cool-grey
work surfaces; one cobalt accent for primary actions, separate semantic colors
for success / warning / danger so status never competes with the accent.

Every color comes from a token set; light and dark are designed separately
rather than inverted. ``apply_theme`` follows the OS color scheme by default.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


class ThemeMode(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True, slots=True)
class Tokens:
    bg: str  # window background
    surface: str  # cards, tables, inputs
    surface_alt: str  # hover rows, subtle fills
    border: str
    text: str
    text_muted: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str  # text on accent
    rail: str  # navigation rail
    rail_text: str
    rail_active: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    dark: bool


LIGHT = Tokens(
    bg="#F4F6F9",
    surface="#FFFFFF",
    surface_alt="#EEF1F6",
    border="#DCE1E9",
    text="#151922",
    text_muted="#5F6878",
    accent="#2F5BD3",
    accent_hover="#244BB4",
    accent_soft="#E5ECFC",
    accent_text="#FFFFFF",
    rail="#12151B",
    rail_text="#AEB5C2",
    rail_active="#252B37",
    success="#1B7A4B",
    success_soft="#E1F2E8",
    warning="#946000",
    warning_soft="#FAEFD8",
    danger="#B42318",
    danger_soft="#FCE6E4",
    dark=False,
)

DARK = Tokens(
    bg="#0F1116",
    surface="#171A21",
    surface_alt="#1F232C",
    border="#2B303B",
    text="#E6E9EF",
    text_muted="#98A0AF",
    accent="#7193F5",
    accent_hover="#89A6F8",
    accent_soft="#1D2843",
    accent_text="#0B1020",
    rail="#0A0C10",
    rail_text="#9AA1AE",
    rail_active="#1E232D",
    success="#5CC48D",
    success_soft="#15291F",
    warning="#E2B04F",
    warning_soft="#2D2412",
    danger="#F2766B",
    danger_soft="#33191A",
    dark=True,
)

_current: Tokens = LIGHT


def tokens() -> Tokens:
    return _current


def resolve(mode: ThemeMode) -> Tokens:
    if mode is ThemeMode.LIGHT:
        return LIGHT
    if mode is ThemeMode.DARK:
        return DARK
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme() if hints is not None else Qt.ColorScheme.Unknown
    return DARK if scheme == Qt.ColorScheme.Dark else LIGHT


def _check_icon(color: str) -> str:
    """Write a check-mark SVG for QSS indicators and return its path."""
    directory = Path(tempfile.gettempdir()) / "dbnero-theme"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"check-{color.lstrip('#')}.svg"
    if not path.exists():
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
            '<path d="M3.5 8.5l3 3 6-7" '
            f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" '
            'stroke-linejoin="round"/></svg>',
            "utf-8",
        )
    return path.as_posix()


def stylesheet(t: Tokens) -> str:
    check = _check_icon(t.accent_text)
    return f"""
* {{ outline: none; }}
QWidget {{ color: {t.text}; font-size: 10pt; }}
QMainWindow, QDialog, QWidget#page {{ background: {t.bg}; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border};
    padding: 4px 6px; }}

/* Navigation rail */
QWidget#rail {{ background: {t.rail}; }}
QLabel#brand {{ color: #FFFFFF; font-size: 15pt; font-weight: 700;
    padding: 18px 18px 14px 18px; }}
QLabel#brandSub {{ color: {t.rail_text}; font-size: 8.5pt; padding: 0 18px 12px 18px; }}
QListWidget#nav {{ background: transparent; border: none; color: {t.rail_text}; }}
QListWidget#nav::item {{ padding: 9px 14px; margin: 1px 10px; border-radius: 8px; }}
QListWidget#nav::item:hover {{ background: {t.rail_active}; color: #FFFFFF; }}
QListWidget#nav::item:selected {{ background: {t.rail_active}; color: #FFFFFF; }}
QLabel#railFooter {{ color: {t.rail_text}; font-size: 8.5pt; padding: 12px 18px; }}

/* Typography */
QLabel#pageTitle {{ font-size: 18pt; font-weight: 600; }}
QLabel#pageSubtitle, QLabel#muted {{ color: {t.text_muted}; }}
QLabel#sectionTitle {{ font-size: 11pt; font-weight: 600; }}
QLabel#eyebrow {{ color: {t.text_muted}; font-size: 8pt; font-weight: 600;
    }}
QLabel#statValue {{ font-size: 20pt; font-weight: 600; }}

/* Surfaces */
QFrame#card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame#banner[tone="warning"] {{ background: {t.warning_soft}; border-radius: 8px; }}
QFrame#banner[tone="danger"] {{ background: {t.danger_soft}; border-radius: 8px; }}
QFrame#banner[tone="info"] {{ background: {t.accent_soft}; border-radius: 8px; }}
QLabel[tone="success"] {{ color: {t.success}; }}
QLabel[tone="warning"] {{ color: {t.warning}; }}
QLabel[tone="danger"] {{ color: {t.danger}; }}

/* Inputs */
QLineEdit, QComboBox, QSpinBox, QDateEdit, QDateTimeEdit, QPlainTextEdit, QTextBrowser {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 7px;
    padding: 6px 8px; selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus, QDateTimeEdit:focus,
QPlainTextEdit:focus {{ border: 1px solid {t.accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {t.text_muted};
    background: {t.surface_alt}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; border: none; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border};
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QListWidget::indicator {{ width: 16px; height: 16px;
    border: 1px solid {t.text_muted if t.dark else t.border}; border-radius: 4px;
    background: {t.surface}; }}
QCheckBox::indicator:checked, QListWidget::indicator:checked {{ background: {t.accent};
    border: 1px solid {t.accent}; image: url("{check}"); }}
QCheckBox::indicator:disabled {{ background: {t.surface_alt}; }}

/* Buttons */
QPushButton {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 7px;
    padding: 7px 14px; font-weight: 500; }}
QPushButton:hover {{ background: {t.surface_alt}; }}
QPushButton:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; }}
QPushButton[kind="primary"] {{ background: {t.accent}; color: {t.accent_text};
    border: 1px solid {t.accent}; }}
QPushButton[kind="primary"]:hover {{ background: {t.accent_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {t.accent_soft}; color: {t.text_muted};
    border: 1px solid {t.accent_soft}; }}
QPushButton[kind="danger"] {{ color: {t.danger}; }}
QPushButton[kind="danger"]:hover {{ background: {t.danger_soft}; }}
QPushButton[kind="ghost"] {{ background: transparent; border: none; color: {t.accent};
    padding: 4px 6px; }}
QPushButton[kind="ghost"]:hover {{ text-decoration: underline; }}

/* Tables and lists */
QTableWidget, QTreeWidget, QListWidget {{ background: {t.surface}; border: 1px solid {t.border};
    border-radius: 10px; gridline-color: transparent; alternate-background-color: {t.surface};
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QTableWidget::item, QTreeWidget::item {{ padding: 6px 8px;
    border-bottom: 1px solid {t.surface_alt}; }}
QListWidget::item {{ padding: 6px 8px; }}
QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QHeaderView {{ background: {t.surface}; border: none; }}
QTableWidget#flat, QTreeWidget#flat {{ border: none; border-radius: 0; }}
QHeaderView::section {{ background: {t.surface}; color: {t.text_muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 8px; font-weight: 600; font-size: 9pt; }}
QTableCornerButton::section {{ background: {t.surface}; border: none; }}

QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; padding: 8px 14px; color: {t.text_muted};
    border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; }}

QProgressBar {{ background: {t.surface_alt}; border: none; border-radius: 4px; height: 8px;
    text-align: center; color: {t.text_muted}; font-size: 8.5pt; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 4px; }}
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t.border}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QStatusBar {{ background: {t.bg}; color: {t.text_muted}; }}
QMessageBox {{ background: {t.bg}; }}
"""


def apply_theme(app: QApplication, mode: ThemeMode = ThemeMode.SYSTEM) -> Tokens:
    global _current
    t = resolve(mode)
    _current = t
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in (
        (QPalette.ColorRole.Window, t.bg),
        (QPalette.ColorRole.Base, t.surface),
        (QPalette.ColorRole.AlternateBase, t.surface_alt),
        (QPalette.ColorRole.Text, t.text),
        (QPalette.ColorRole.WindowText, t.text),
        (QPalette.ColorRole.Button, t.surface),
        (QPalette.ColorRole.ButtonText, t.text),
        (QPalette.ColorRole.Highlight, t.accent),
        (QPalette.ColorRole.HighlightedText, t.accent_text),
        (QPalette.ColorRole.PlaceholderText, t.text_muted),
        (QPalette.ColorRole.ToolTipBase, t.surface),
        (QPalette.ColorRole.ToolTipText, t.text),
        (QPalette.ColorRole.Link, t.accent),
    ):
        pal.setColor(role, QColor(color))
    # Fusion draws check boxes, frames and separators from these roles.
    for role in (QPalette.ColorRole.Light, QPalette.ColorRole.Midlight):
        pal.setColor(role, QColor(t.surface_alt))
    for role in (QPalette.ColorRole.Mid, QPalette.ColorRole.Dark, QPalette.ColorRole.Shadow):
        pal.setColor(role, QColor(t.text_muted if t.dark else t.border))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t.text_muted))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t.text_muted))
    app.setPalette(pal)
    font = QFont(app.font())
    if font.pointSizeF() < 10:
        font.setPointSizeF(10)
    app.setFont(font)
    app.setStyleSheet(stylesheet(t))
    return t
