"""Each platform's official logo on a tile of its own colour, wherever a place shows (Destinations, Get started,
+ Add, Show all, Settings → Stream, the Chat panel).

The logo shapes are from Simple Icons (CC0 1.0); each logo is its owner's trademark, shown here only to say where
a place streams to. Platforms without a logo here keep two letters on their colour. Tiles are drawn once per
platform, size and screen scale.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

# Simple Icons paths, on a 24 x 24 box.
LOGOS = {
    "twitch": (
        "M11.571 4.714h1.715v5.143H11.57zm4.715 0H18v5.143h-1.714zM6 0L1.714 4.286v15.428h5.143V24l4.286-4.286h3.428L22.286 12V0zm14.571 11.143l-3.428 3.428h-3.429l-3 3v-3H6.857V1.714h13.714Z"),
    "youtube": (
        "M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z"),
    "kick": (
        "M1.333 0h8v5.333H12V2.667h2.667V0h8v8H20v2.667h-2.667v2.666H20V16h2.667v8h-8v-2.667H12v-2.666H9.333V24h-8Z"),
    "tiktok": (
        "M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z"),
    "x": (
        "M18.901 1.153h3.68l-8.04 9.19L24 22.846h-7.406l-5.8-7.584-6.638 7.584H.474l8.6-9.83L0 1.154h7.594l5.243 6.932ZM17.61 20.644h2.039L6.486 3.24H4.298Z"),
    "facebook": (
        "M9.101 23.691v-7.98H6.627v-3.667h2.474v-1.58c0-4.085 1.848-5.978 5.858-5.978.401 0 .955.042 1.468.103a8.68 8.68 0 0 1 1.141.195v3.325a8.623 8.623 0 0 0-.653-.036 26.805 26.805 0 0 0-.733-.009c-.707 0-1.259.096-1.675.309a1.686 1.686 0 0 0-.679.622c-.258.42-.374.995-.374 1.752v1.297h3.919l-.386 2.103-.287 1.564h-3.246v8.245C19.396 23.238 24 18.179 24 12.044c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.628 3.874 10.35 9.101 11.647Z"),
}

# platform -> (letters for when there's no logo, tile colour, logo colour)
TILES = {
    "twitch": ("Tw", "#9146ff", "#ffffff"), "youtube": ("Yt", "#ff2d48", "#ffffff"),
    "kick": ("K", "#53fc18", "#0b0f0d"), "tiktok": ("Tt", "#0b0f0d", "#ffffff"),
    "x": ("X", "#0b0f0d", "#ffffff"), "facebook": ("Fb", "#1877f2", "#ffffff"),
    "chzzk": ("Cz", "#00ffa3", "#0b0f0d"), "mixcloud": ("Mx", "#5000ff", "#ffffff"),
    "niconico": ("Nc", "#252525", "#ffffff"), "restream": ("Rs", "#ff5a1f", "#0b0f0d"),
    "showroom": ("Sr", "#f5c518", "#0b0f0d"), "soop": ("So", "#0545b1", "#ffffff"),
    "beam": ("Be", "#7c5cff", "#ffffff"), "shareplay": ("Sp", "#2ec4b6", "#0b0f0d"),
    "vimeo": ("Vm", "#1ab7ea", "#0b0f0d"), "whowatch": ("Ww", "#ffd43b", "#0b0f0d"),
}
LOGO_SHARE = 0.6  # of the tile's side
DARK_EDGE = "#3a4440"  # a black tile gets a soft edge so it doesn't vanish on a dark panel

_made: dict[tuple, QPixmap] = {}


def has_logo(service: str | None) -> bool:
    return (service or "") in LOGOS


def tile(service: str | None, size: int = 32, letters: str = "", fill: str = "", ink: str = "",
         ratio: float = 2.0) -> QPixmap:
    """The platform's tile: its logo (or `letters`) on its colour, rounded. Unknown places use the given
    letters and colours."""
    key = (service or "", size, letters, fill, ink, ratio)
    if key in _made:
        return _made[key]
    known = TILES.get(service or "")
    if known is not None:
        letters, fill, ink = known
    pix = QPixmap(round(size * ratio), round(size * ratio))
    pix.setDevicePixelRatio(ratio)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    box = QRectF(0.5, 0.5, size - 1, size - 1)
    radius = size * 0.31
    p.setPen(QColor(DARK_EDGE) if QColor(fill).lightness() < 40 else Qt.PenStyle.NoPen)
    p.setBrush(QColor(fill or "#2a3330"))
    p.drawRoundedRect(box, radius, radius)
    if has_logo(service):
        from PySide6.QtSvg import QSvgRenderer

        side = size * LOGO_SHARE
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
               f'<path fill="{ink}" d="{LOGOS[service]}"/></svg>')
        QSvgRenderer(QByteArray(svg.encode())).render(p, QRectF((size - side) / 2, (size - side) / 2, side, side))
    else:
        font = QFont(p.font())
        font.setPixelSize(max(8, round(size * 0.38)))
        font.setWeight(QFont.Weight.ExtraBold)
        p.setFont(font)
        p.setPen(QColor(ink or "#f2f7f3"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, letters)
    p.end()
    _made[key] = pix
    return pix


def icon(service: str | None, size: int = 20, letters: str = "") -> QIcon:
    """The tile as an icon for menus, lists and drop-downs, sharp at 1x to 3x. A place without its own tile
    shows `letters` on the same neutral tile Destinations uses."""
    from sprigcast.app import theme

    out = QIcon()
    for ratio in (1.0, 2.0, 3.0):
        out.addPixmap(tile(service, size, letters, theme.RAISED_HOVER, theme.TEXT, ratio))
    return out
