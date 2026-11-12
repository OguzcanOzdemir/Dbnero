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
from typing import Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QLineEdit,
    QProxyStyle,
    QStyle,
    QStyleFactory,
    QStyleHintReturn,
    QStyleOption,
    QWidget,
)


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


_image_dir: Path | None = None


def _chevron_image(color: str, path: Path, scale: int) -> None:
    size = 12 * scale
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.6 * scale)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    path_ = QPainterPath(QPointF(size * 0.2, size * 0.38))
    path_.lineTo(QPointF(size * 0.5, size * 0.66))
    path_.lineTo(QPointF(size * 0.8, size * 0.38))
    p.drawPath(path_)
    p.end()
    img.save(str(path))


def theme_images(t: Tokens) -> dict[str, str]:
    """PNG arrows for the stylesheet (PNG needs no image plugin, unlike SVG).
    Qt picks the ``@2x`` file on high-DPI screens."""
    global _image_dir
    if _image_dir is None:
        _image_dir = Path(tempfile.mkdtemp(prefix="dbnero-theme-"))
    name = "dark" if t.dark else "light"
    out = {}
    for key, color in (("arrow", t.text_muted), ("arrow_disabled", t.border)):
        base = _image_dir / f"{key}-{name}.png"
        if not base.exists():
            _chevron_image(color, base, 1)
            _chevron_image(color, base.with_name(f"{key}-{name}@2x.png"), 2)
        out[key] = base.as_posix()
    return out


def stylesheet(t: Tokens, images: dict[str, str] | None = None) -> str:
    img = images or {"arrow": "", "arrow_disabled": ""}
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
QLineEdit, QComboBox, QSpinBox, QDateEdit, QDateTimeEdit, QTimeEdit, QPlainTextEdit,
QTextBrowser {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 7px;
    padding: 6px 8px; selection-background-color: {t.accent}; selection-color: {t.accent_text};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus, QDateTimeEdit:focus,
QTimeEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {t.accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDateEdit:disabled,
QTimeEdit:disabled {{ color: {t.text_muted}; background: {t.surface_alt}; }}
QComboBox:hover, QDateEdit:hover, QDateTimeEdit:hover {{ border: 1px solid {t.text_muted}; }}
QComboBox:focus, QDateEdit:focus {{ border: 1px solid {t.accent}; }}
/* Drop-down: a borderless area with a themed chevron (no Fusion bevel). */
QComboBox, QDateEdit {{ padding-right: 30px; }}
QComboBox::drop-down, QDateEdit::drop-down, QDateTimeEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right; width: 28px;
    border: none; background: transparent; }}
QComboBox::down-arrow, QDateEdit::down-arrow, QDateTimeEdit::down-arrow {{
    image: url("{img["arrow"]}"); width: 12px; height: 12px; }}
QComboBox::down-arrow:disabled, QDateEdit::down-arrow:disabled {{
    image: url("{img["arrow_disabled"]}"); }}
/* An editable combo's text field must not draw a second box inside it. */
QComboBox QLineEdit {{ border: none; background: transparent; padding: 0; margin: 0;
    border-radius: 0; }}
/* Spin boxes and time fields are typed into; no tiny step buttons. */
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 0; height: 0;
    border: none; image: none; }}
QAbstractSpinBox::up-arrow, QAbstractSpinBox::down-arrow {{ width: 0; height: 0;
    image: none; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border};
    border-radius: 6px; padding: 4px; outline: none;
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QComboBox QAbstractItemView::item {{ min-height: 28px; padding: 2px 8px; border-radius: 5px; }}
QCalendarWidget QWidget {{ alternate-background-color: {t.surface_alt}; }}
QCalendarWidget QToolButton {{ color: {t.text}; background: transparent; border: none;
    padding: 4px 8px; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}

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

/* Tables and lists (DataTable cells are painted by CellDelegate) */
QTableWidget, QTreeWidget, QListWidget {{ background: {t.surface}; border: 1px solid {t.border};
    border-radius: 10px; gridline-color: transparent; alternate-background-color: {t.surface};
    selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QTableWidget::item {{ border: none; padding: 0; }}
QTreeWidget::item {{ padding: 6px 8px; border-bottom: 1px solid {t.surface_alt}; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {t.surface_alt}; }}
QTreeWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {t.accent_soft}; color: {t.text}; }}
QHeaderView {{ background: {t.surface}; border: none; border-top-left-radius: 10px;
    border-top-right-radius: 10px; }}
QTableWidget#flat, QTreeWidget#flat {{ border: none; border-radius: 0; }}
QHeaderView::section {{ background: {t.surface}; color: {t.text_muted}; border: none;
    border-bottom: 1px solid {t.border}; padding: 10px 12px; font-weight: 600;
    font-size: 9pt; }}
QHeaderView::section:hover {{ color: {t.text}; }}
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


class DbneroStyle(QProxyStyle):
    """Fusion with check boxes, radio buttons and arrows drawn from the theme
    tokens, so they stay visible in both themes without image files."""

    def __init__(self, t: Tokens) -> None:
        super().__init__(QStyleFactory.create("Fusion"))
        self.t = t

    def polish(self, arg: Any) -> Any:  # QWidget | QApplication | QPalette overloads
        if isinstance(arg, QAbstractSpinBox):
            # Stylesheet padding does not reach a spin box's inner text field.
            edit = arg.findChild(QLineEdit)
            if edit is not None:
                edit.setTextMargins(6, 0, 0, 0)
        return super().polish(arg)

    def styleHint(
        self,
        hint: QStyle.StyleHint,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
        returnData: QStyleHintReturn | None = None,
    ) -> int:
        if hint == QStyle.StyleHint.SH_FormLayoutLabelAlignment:
            # Form labels line up with the middle of their input fields.
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        return int(super().styleHint(hint, option, widget, returnData))

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        pe = QStyle.PrimitiveElement
        if element in (pe.PE_IndicatorCheckBox, pe.PE_IndicatorItemViewItemCheck):
            self._check_box(option, painter)
        elif element == pe.PE_IndicatorRadioButton:
            self._radio(option, painter)
        elif element == pe.PE_IndicatorArrowDown:
            self._chevron(option, painter)
        else:
            super().drawPrimitive(element, option, painter, widget)

    def _square(self, option: QStyleOption, size: int = 16) -> QRectF:
        r = option.rect
        side = min(size, r.width(), r.height())
        return QRectF(
            r.x() + (r.width() - side) / 2 + 0.5,
            r.y() + (r.height() - side) / 2 + 0.5,
            side - 1,
            side - 1,
        )

    def _states(self, option: QStyleOption) -> tuple[bool, bool, bool]:
        st = option.state
        flag = QStyle.StateFlag
        return (
            bool(st & flag.State_On),
            bool(st & flag.State_NoChange),
            bool(st & flag.State_Enabled),
        )

    def _check_box(self, option: QStyleOption, p: QPainter) -> None:
        t = self.t
        on, partial, enabled = self._states(option)
        box = self._square(option)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        filled = (on or partial) and enabled
        p.setPen(QPen(QColor(t.accent if filled else (t.text_muted if t.dark else "#8A93A3")), 1.4))
        p.setBrush(QColor(t.accent if filled else t.surface_alt if not enabled else t.surface))
        p.drawRoundedRect(box, 4, 4)
        if on or partial:
            pen = QPen(QColor(t.accent_text if enabled else t.text_muted), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            x, y, w, h = box.x(), box.y(), box.width(), box.height()
            if on:
                path = QPainterPath(QPointF(x + w * 0.24, y + h * 0.52))
                path.lineTo(QPointF(x + w * 0.43, y + h * 0.71))
                path.lineTo(QPointF(x + w * 0.77, y + h * 0.31))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            else:
                p.drawLine(QPointF(x + w * 0.27, y + h / 2), QPointF(x + w * 0.73, y + h / 2))
        p.restore()

    def _radio(self, option: QStyleOption, p: QPainter) -> None:
        t = self.t
        on, _partial, enabled = self._states(option)
        box = self._square(option)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = t.accent if on and enabled else (t.text_muted if t.dark else "#8A93A3")
        p.setPen(QPen(QColor(ring), 1.4))
        p.setBrush(QColor(t.surface if enabled else t.surface_alt))
        p.drawEllipse(box)
        if on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent if enabled else t.text_muted))
            inner = box.adjusted(
                box.width() * 0.28, box.height() * 0.28, -box.width() * 0.28, -box.height() * 0.28
            )
            p.drawEllipse(inner)
        p.restore()

    def _chevron(self, option: QStyleOption, p: QPainter) -> None:
        box = self._square(option, 12)
        st = option.state
        enabled = bool(st & QStyle.StateFlag.State_Enabled)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self.t.text_muted if enabled else self.t.border), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        x, y, w, h = box.x(), box.y(), box.width(), box.height()
        path = QPainterPath(QPointF(x + w * 0.2, y + h * 0.38))
        path.lineTo(QPointF(x + w * 0.5, y + h * 0.66))
        path.lineTo(QPointF(x + w * 0.8, y + h * 0.38))
        p.drawPath(path)
        p.restore()


_style: DbneroStyle | None = None


def apply_theme(app: QApplication, mode: ThemeMode = ThemeMode.SYSTEM) -> Tokens:
    global _current
    t = resolve(mode)
    global _style
    _current = t
    _style = DbneroStyle(t)
    app.setStyle(_style)
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
    app.setStyleSheet(stylesheet(t, theme_images(t)))
    return t
