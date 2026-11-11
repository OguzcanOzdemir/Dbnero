"""Small stroke icons drawn for dbnero (24x24 viewBox, 1.75px strokes).

Icons are SVG templates rendered at runtime in the requested color, so they
follow the active theme without image assets.
"""

from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS: dict[str, str] = {
    # house
    "home": '<path d="M4 11 12 4l8 7"/><path d="M6 10v9h12v-9"/><path d="M10 19v-5h4v5"/>',
    # cylinder with down arrow
    "backup": (
        '<ellipse cx="12" cy="6" rx="7" ry="2.5"/><path d="M5 6v6c0 1.4 3.1 2.5 7 2.5"/>'
        '<path d="M19 6v4"/><path d="M17 14v6"/><path d="m14.5 17.5 2.5 2.5 2.5-2.5"/>'
    ),
    # stacked boxes
    "archive": (
        '<rect x="3.5" y="4" width="17" height="5" rx="1.2"/><path d="M5 9v10h14V9"/>'
        '<path d="M10 13h4"/>'
    ),
    # counter-clockwise arrow
    "restore": '<path d="M4 5v5h5"/><path d="M5.5 14a7 7 0 1 0 1.6-7.3L4 10"/>',
    # two columns with arrows
    "compare": (
        '<rect x="3.5" y="4" width="7" height="16" rx="1.2"/>'
        '<rect x="13.5" y="4" width="7" height="16" rx="1.2"/><path d="M6 9h2M6 12h2M16 12h2"/>'
    ),
    # plug
    "connections": (
        '<path d="M9 3v4M15 3v4"/><path d="M6.5 7h11v3a5.5 5.5 0 0 1-11 0z"/>'
        '<path d="M12 15.5V21"/>'
    ),
    # pulse line
    "activity": '<path d="M3 12h4l2.5-6 5 12 2.5-6H21"/>',
    # sliders
    "settings": (
        '<path d="M4 6h10M18 6h2M4 12h3M11 12h9M4 18h12M20 18h0"/>'
        '<circle cx="16" cy="6" r="2"/><circle cx="9" cy="12" r="2"/>'
        '<circle cx="18" cy="18" r="2"/>'
    ),
}


def _svg(name: str, color: str) -> bytes:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{color}" stroke-width="1.75" stroke-linecap="round" '
        f'stroke-linejoin="round">{_PATHS[name]}</svg>'
    ).encode()


def _pixmaps(name: str, color: str, size: int) -> list[QPixmap]:
    renderer = QSvgRenderer(QByteArray(_svg(name, color)))
    out = []
    for scale in (1, 2):
        pix = QPixmap(QSize(size * scale, size * scale))
        pix.setDevicePixelRatio(scale)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        out.append(pix)
    return out


@lru_cache(maxsize=64)
def icon(name: str, color: str, active_color: str | None = None, size: int = 20) -> QIcon:
    """Icon in ``color``; ``active_color`` is used for selected/hovered items."""
    result = QIcon()
    for pix in _pixmaps(name, color, size):
        result.addPixmap(pix, QIcon.Mode.Normal)
    if active_color:
        for pix in _pixmaps(name, active_color, size):
            result.addPixmap(pix, QIcon.Mode.Selected)
            result.addPixmap(pix, QIcon.Mode.Active)
    return result


def names() -> list[str]:
    return list(_PATHS)
