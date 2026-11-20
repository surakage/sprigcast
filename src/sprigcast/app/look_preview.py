"""Settings → Appearance: what a theme looks like before you pick it.

LookPreview paints a small piece of SprigCast (the side menu, a titled section, a button, the action button, a
Live pill, a text box and a level bar) in any theme and color preset, at any text size. ThemeTile is a small
one you click to pick that theme. Nothing here restyles the app: the colors come from theme.palette().
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

from sprigcast.app import theme

# The System theme is Windows' own look, which SprigCast doesn't draw: a plain light window stands for it.
SYSTEM_COLORS = {
    "BG": "#f3f3f3", "CHROME": "#e6e6e6", "PANEL": "#ffffff", "SECTION": "#ffffff", "SECTION_EDGE": "#c9c9c9",
    "FIELD": "#ffffff", "FIELD_EDGE": "#8a8a8a", "BUTTON": "#e1e1e1", "BUTTON_EDGE": "#adadad",
    "TEXT": "#1b1b1b", "TEXT_BRIGHT": "#000000", "MUTED": "#6b6b6b", "LABEL": "#3a3a3a", "ACCENT": "#0067c0",
    "ACCENT_TEXT": "#ffffff", "SELECT_BG": "#cce4f7", "SELECT_TEXT": "#003e73", "TRACK": "#d6d6d6",
    "WARN": "#c9a400", "BAD": "#c42b1c", "LIVE": theme.LIVE, "LIVE_EDGE": theme.LIVE_EDGE, "EDGE": "#d0d0d0",
    "EDGE_STRONG": "#b0b0b0",
}


_palettes: dict[tuple, dict[str, str]] = {}  # worked out once each: five themes a visit were most of the page's 300 ms


def colors_for(theme_name: str, preset: str = "default") -> dict[str, str]:
    if theme_name == "system":
        return dict(SYSTEM_COLORS)
    key = (theme_name, preset, len(theme._NIGHT_CANOPY))  # colors named later in theme.py make it anew
    if key not in _palettes:
        _palettes[key] = theme.palette(theme_name, preset)
    return dict(_palettes[key])


def paint_look(p: QPainter, box: QRectF, colors: dict[str, str], base: QFont, scale: float = 1.0,
               words: bool = True) -> None:
    """A small piece of the app in `colors`, filling `box`. `words` off draws bars for text (small tiles)."""
    c = lambda name: QColor(colors[name])  # noqa: E731
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    radius = min(16.0, box.height() * 0.12)
    frame = QPainterPath()
    frame.addRoundedRect(box, radius, radius)
    p.setClipPath(frame)
    p.fillRect(box, c("BG"))
    u = box.height() / 12  # one row of the picture
    # The side menu: three choices, the middle one picked.
    side = QRectF(box.left(), box.top(), box.width() * 0.2, box.height())
    p.fillRect(side, c("CHROME"))
    p.setPen(Qt.PenStyle.NoPen)
    for i, name in enumerate(("Live", "Build", "Sound")):
        row = QRectF(side.left() + u * 0.7, side.top() + u * (1.1 + i * 1.9), side.width() - u * 1.4, u * 1.5)
        picked = i == 1
        if picked:
            p.setBrush(c("SELECT_BG"))
            p.drawRoundedRect(row, row.height() / 2, row.height() / 2)
        ink = c("SELECT_TEXT" if picked else "MUTED")
        if words:
            font = QFont(base)
            font.setPixelSize(max(8, round(u * 0.78 * scale)))
            font.setWeight(QFont.Weight.DemiBold if picked else QFont.Weight.Normal)
            p.setFont(font)
            p.setPen(ink)
            p.drawText(row.adjusted(u * 0.7, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, name)
            p.setPen(Qt.PenStyle.NoPen)
        else:
            p.setBrush(ink)
            bar = QRectF(row.left() + u * 0.6, row.center().y() - u * 0.2, row.width() * 0.55, u * 0.4)
            p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
    # A titled section card.
    card = QRectF(side.right() + u, box.top() + u, box.right() - side.right() - 2 * u, box.height() - 2 * u)
    p.setPen(QPen(c("SECTION_EDGE"), 1))
    p.setBrush(c("SECTION"))
    p.drawRoundedRect(card, u * 1.1, u * 1.1)
    inner = card.adjusted(u, u * 0.8, -u, -u * 0.8)
    leaf = QRectF(inner.left(), inner.top(), u * 1.2, u * 1.2)
    theme.paint_leaf(p, leaf, True, colors["ACCENT"], vein=colors["SECTION"], width=max(1.0, u * 0.12))
    title = QRectF(leaf.right() + u * 0.5, inner.top(), inner.width() - leaf.width(), u * 1.2)
    p.setPen(Qt.PenStyle.NoPen)
    if words:
        font = QFont(base)
        font.setPixelSize(max(9, round(u * 0.95 * scale)))
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(c("TEXT_BRIGHT"))
        p.drawText(title, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Going live")
        font.setWeight(QFont.Weight.Normal)
        font.setPixelSize(max(8, round(u * 0.78 * scale)))
        p.setFont(font)
        p.setPen(c("MUTED"))
        p.drawText(QRectF(inner.left(), inner.top() + u * 1.6, inner.width(), u * 1.2),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Sloth cam, game and chat are ready.")
        p.setPen(Qt.PenStyle.NoPen)
    else:
        p.setBrush(c("TEXT"))
        p.drawRoundedRect(QRectF(title.left(), title.center().y() - u * 0.25, inner.width() * 0.4, u * 0.5), u * 0.25, u * 0.25)
        p.setBrush(c("MUTED"))
        p.drawRoundedRect(QRectF(inner.left(), inner.top() + u * 2.0, inner.width() * 0.75, u * 0.4), u * 0.2, u * 0.2)
    # A row of buttons: a plain one, the action one, and the Live pill (pink in every theme).
    top = inner.top() + u * 3.4
    height = u * 1.9
    gap = u * 0.6
    wide = (inner.width() - 2 * gap) / 3
    for i, (name, fill, edge, ink) in enumerate((("Button", "BUTTON", "BUTTON_EDGE", "TEXT"),
                                                 ("Go live", "ACCENT", "ACCENT", "ACCENT_TEXT"),
                                                 ("LIVE", "LIVE", "LIVE_EDGE", None))):
        rect = QRectF(inner.left() + i * (wide + gap), top, wide, height)
        p.setPen(QPen(c(edge), 1))
        p.setBrush(c(fill))
        p.drawRoundedRect(rect, height / 2, height / 2)
        text = QColor("#ffffff") if ink is None else c(ink)
        if words:
            font = QFont(base)
            font.setPixelSize(max(8, round(u * 0.8 * scale)))
            font.setWeight(QFont.Weight.DemiBold)
            p.setFont(font)
            p.setPen(text)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(text)
            bar = QRectF(0, 0, rect.width() * 0.45, u * 0.4)
            bar.moveCenter(rect.center())
            p.drawRoundedRect(bar, bar.height() / 2, bar.height() / 2)
    # A text box and a level bar in the good, warning and bad colors.
    field = QRectF(inner.left(), top + height + u * 0.7, inner.width() * 0.42, u * 1.7)
    p.setPen(QPen(c("FIELD_EDGE"), 1))
    p.setBrush(c("FIELD"))
    p.drawRoundedRect(field, u * 0.6, u * 0.6)
    if words:
        font = QFont(base)
        font.setPixelSize(max(8, round(u * 0.78 * scale)))
        p.setFont(font)
        p.setPen(c("TEXT"))
        p.drawText(field.adjusted(u * 0.7, 0, 0, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Stream title")
    meter = QRectF(field.right() + u, field.center().y() - u * 0.3, inner.right() - field.right() - u, u * 0.6)
    p.setPen(Qt.PenStyle.NoPen)
    clip = QPainterPath()
    clip.addRoundedRect(meter, meter.height() / 2, meter.height() / 2)
    p.setClipPath(frame.intersected(clip))
    p.fillRect(meter, c("TRACK"))
    x = meter.left()
    for name, share in (("ACCENT", 0.5), ("WARN", 0.2), ("BAD", 0.1)):
        p.fillRect(QRectF(x, meter.top(), meter.width() * share + 1, meter.height()), c(name))
        x += meter.width() * share
    p.restore()


class LookPreview(QWidget):
    """The big preview: follows the theme, color preset and text size picked on the page."""

    def __init__(self, theme_name: str, preset: str = "default", text_size: str = "normal",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(320, 200)
        self.setMaximumHeight(200)
        self.setMaximumWidth(940)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.theme_name, self.preset, self.text_size = theme_name, preset, text_size
        self.colors = colors_for(theme_name, preset)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(520, 200)

    def show_theme(self, theme_name: str) -> None:
        self.theme_name = theme_name
        self._again()

    def show_preset(self, preset: str) -> None:
        self.preset = preset
        self._again()

    def show_text_size(self, text_size: str) -> None:
        self.text_size = text_size
        self._again()

    def _again(self) -> None:
        self.colors = colors_for(self.theme_name, self.preset)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        scale = theme.TEXT_SIZES.get(self.text_size, ("", 1.0))[1]
        paint_look(p, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), self.colors, self.font(), scale)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(theme.SECTION_EDGE), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        radius = min(16.0, self.height() * 0.12)
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.end()


class ThemeTile(QAbstractButton):
    """One theme to pick: a small picture of it over its name."""

    def __init__(self, theme_name: str, label: str, preset: str = "default", parent: QWidget | None = None):
        super().__init__(parent)
        self.theme_name, self.label = theme_name, label
        self.colors = colors_for(theme_name, preset)
        self.setCheckable(True)
        self.setAutoExclusive(True)
        self.setAccessibleName(f"{label} theme")  # each its own: all six were announced as "Theme" (2026-11-20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(92, 108)
        self.setMaximumSize(150, 108)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(theme.THEME_ABOUT.get(theme_name, "Windows' own look"))

    def show_preset(self, preset: str) -> None:
        self.colors = colors_for(self.theme_name, preset)
        self.update()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on, hover = self.isChecked(), self.underMouse()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(QColor(theme.ACCENT if on else (theme.BUTTON_EDGE_HOVER if hover else theme.BUTTON_EDGE)),
                      2 if on else 1))
        p.setBrush(QColor(theme.SELECT_BG if on else (theme.BUTTON_HOVER if hover else theme.BUTTON)))
        p.drawRoundedRect(r, 14, 14)
        picture = QRectF(r.left() + 8, r.top() + 8, r.width() - 16, 66)
        paint_look(p, picture, self.colors, self.font(), words=False)
        font = QFont(self.font())
        font.setPixelSize(12)
        font.setWeight(QFont.Weight.DemiBold if on else QFont.Weight.Medium)
        p.setFont(font)
        p.setPen(QColor(theme.SELECT_TEXT if on else theme.TEXT))
        name = self.label.split(" (")[0]
        p.drawText(QRectF(r.left() + 4, picture.bottom() + 4, r.width() - 8, r.bottom() - picture.bottom() - 8),
                   Qt.AlignmentFlag.AlignCenter, name)
        p.end()
