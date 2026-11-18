"""One look system for the cards plugins draw (Now playing, Chat, Local time, Countdown, Stream timer...).

Every styled plugin gets the same Style tab in its settings, filled from the keys it lists in its defaults:

    text_color, card_color, accent_color   colours ("#rrggbb" or "#rrggbbaa"; ending in 00 is see-through)
    font                                   a font family ("" = SprigCast's own, Nunito)
    text_scale                             text size in % of the plugin's own (50 to 200)
    corners                                "round", "leaf" (two big corners, two small) or "square"
    show_*                                 parts to show or hide (show_date, show_dot...)
    custom_look                            the power-user box, below

Custom look is a short list of `name: value` lines, like CSS. It only changes how a plugin looks: it is read,
never run, and a line it doesn't understand is skipped (and named under the box) so the plugin falls back to
its own look for that part. It can never crash or freeze a plugin.

    text: #fff8e7          card: #10201880         accent: pink
    font: Comic Sans MS    size: 120%              corners: leaf      (or a number: 0 to 60)
    border: 3 #ffffff      shadow: on              hide: date, dot    show: dot

A plugin reads its look once per settings change (resolve(settings, base) is cached), so styling costs
nothing per frame: the look is part of the key that decides when the card is drawn again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

CORNERS = (("round", "Round"), ("leaf", "Leaf"), ("square", "Square"))
CORNER_NAMES = {key for key, _ in CORNERS}
FONTS = ("Nunito", "Arial", "Bahnschrift", "Calibri", "Cascadia Code", "Comic Sans MS", "Consolas", "Georgia",
         "Impact", "Segoe Print", "Segoe UI", "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana")
# Font names to Windows font files, for plugins that draw with Pillow (the chat box) rather than Qt.
FONT_FILES = {"arial": "arial.ttf", "bahnschrift": "bahnschrift.ttf", "calibri": "calibri.ttf",
              "cascadia code": "CascadiaCode.ttf", "comic sans ms": "comic.ttf", "consolas": "consola.ttf",
              "georgia": "georgia.ttf", "impact": "impact.ttf", "nunito": "", "segoe print": "segoepr.ttf",
              "segoe ui": "segoeui.ttf", "tahoma": "tahoma.ttf", "times new roman": "times.ttf",
              "trebuchet ms": "trebuc.ttf", "verdana": "verdana.ttf"}
OWN_FONT = ["Nunito", "Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"]  # SprigCast's, in order

# The keys that go on the Style tab of a plugin's settings (show_* ones too).
STYLE_KEYS = ("text_color", "card_color", "accent_color", "bubble_color", "background_color", "outline_color",
              "font", "text_scale", "corners", "custom_look")

MOST_LOOK = 4000  # characters read from a custom look; the rest is ignored
MOST_RULES = 64
MOST_SKIPPED = 5  # problems named under the box

NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#e5484d", "pink": "#ff7eb6", "orange": "#ff9a3c",
    "yellow": "#ffd23f", "gold": "#f5c542", "green": "#4fd88f", "mint": "#9ff0c4", "teal": "#2ec4b6",
    "blue": "#4a9eff", "navy": "#1b2a4a", "purple": "#9b6bff", "violet": "#b48cff", "brown": "#8a5a3b",
    "grey": "#8b9a92", "gray": "#8b9a92", "cream": "#fff8e7", "transparent": "#00000000", "none": "#00000000",
}

# What a custom look's names mean (CSS's names work too).
ALIASES = {
    "text": "text", "color": "text", "colour": "text", "text-color": "text", "text-colour": "text",
    "card": "card", "background": "card", "background-color": "card", "bg": "card",
    "accent": "accent", "accent-color": "accent",
    "font": "font", "font-family": "font", "family": "font",
    "size": "size", "font-size": "size", "text-size": "size",
    "corners": "corners", "corner": "corners", "border-radius": "corners", "radius": "corners",
    "border": "border", "outline": "border",
    "shadow": "shadow", "box-shadow": "shadow",
    "hide": "hide", "show": "show",
}


@dataclass(frozen=True)
class Style:
    text: str = "#f2f7f3ff"
    card: str = "#151c19e0"
    accent: str = "#4fd88fff"
    font: str = ""  # "" = SprigCast's own
    scale: float = 1.0
    corners: str = "round"
    radius: float | None = None  # a custom look's exact corner size (box units); None = the plugin's own
    border_width: float = 0.0
    border_color: str = "#ffffff16"
    shadow: bool = False
    hidden: frozenset = field(default_factory=frozenset)  # parts a custom look hid
    shown: frozenset = field(default_factory=frozenset)  # parts a custom look showed
    problems: tuple = ()  # custom look lines that were skipped

    @property
    def key(self) -> tuple:
        """Everything that changes the picture, for a card's "draw again?" key."""
        return (self.text, self.card, self.accent, self.font, self.scale, self.corners, self.radius,
                self.border_width, self.border_color, self.shadow, self.hidden, self.shown)

    def shows(self, part: str, settings: dict[str, Any] | None = None, default: bool = True) -> bool:
        """Whether a part (date, dot, label...) is drawn: the custom look wins, then show_<part>, then default."""
        if part in self.hidden:
            return False
        if part in self.shown:
            return True
        if settings is not None and f"show_{part}" in settings:
            return bool(settings[f"show_{part}"])
        return default

    def families(self) -> list[str]:
        return [self.font, *OWN_FONT] if self.font and self.font != "Nunito" else list(OWN_FONT)

    def px(self, size: float) -> int:
        """A font size in pixels, at the chosen text size."""
        return max(4, round(size * self.scale))

    def corner(self, radius: float, height: float | None = None) -> float:
        """The plugin's corner size, or the custom look's (never more than half the card's height)."""
        r = radius if self.radius is None else self.radius
        if self.corners == "square" and self.radius is None:
            r = min(r, 4.0)
        return min(r, height / 2) if height else r


# Colours -------------------------------------------------------------------------------------------------

_HEX = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_RGB = re.compile(r"rgba?\(\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*(?:,\s*([\d.]+%?)\s*)?\)", re.I)


def colour(text: Any) -> str | None:
    """A colour as "#rrggbbaa" (lower case), or None if it isn't one. Never raises."""
    if not isinstance(text, str):
        return None
    text = text.strip().lower()
    if text in NAMED:
        text = NAMED[text]
    m = _HEX.fullmatch(text)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h)
        return "#" + (h + "ff" if len(h) == 6 else h).lower()
    m = _RGB.fullmatch(text)
    if m:
        def part(v: str, top: float) -> int | None:
            n = float(v[:-1]) * top / 100 if v.endswith("%") else float(v)
            return round(n) if 0 <= n <= top else None

        try:
            rgb = [part(v, 255) for v in m.groups()[:3]]
            alpha = m.group(4)
            if alpha is None:
                a = 255
            elif alpha.endswith("%"):
                a = part(alpha, 255)
            else:
                a = round(float(alpha) * 255) if float(alpha) <= 1 else None
        except ValueError:
            return None
        if None in rgb or a is None:
            return None
        return "#{:02x}{:02x}{:02x}{:02x}".format(*rgb, a)
    return None


def _setting_colour(settings: dict[str, Any], key: str, fallback: str) -> str:
    return colour(settings.get(key)) or colour(fallback) or "#ffffffff"


# Custom look ---------------------------------------------------------------------------------------------

_FONT_NAME = re.compile(r"[A-Za-z0-9 ._'-]{1,64}")
_PART = re.compile(r"[a-z][a-z0-9_]{0,23}")


@lru_cache(maxsize=64)
def parse_look(text: str) -> tuple[dict[str, Any], tuple[str, ...]]:
    """A custom look's rules as {name: value}, and the lines it skipped (with why). Never raises."""
    rules: dict[str, Any] = {}
    skipped: list[str] = []
    try:
        text = str(text or "")[:MOST_LOOK]
        text = re.sub(r"/\*.*?(\*/|$)", " ", text, flags=re.S)  # /* comments */
        text = re.sub(r"[^{};:\n]*\{", "\n", text).replace("}", "\n")  # ".clock { ... }": just the lines inside
        lines = [part for line in text.splitlines() for part in line.split(";")]
        for raw in lines[: MOST_RULES * 4]:
            line = raw.split("//", 1)[0].strip()
            if not line:
                continue
            if len(rules) >= MOST_RULES:
                break
            if ":" not in line:
                skipped.append(f"{line[:30]}: needs a colon")
                continue
            name, value = (s.strip() for s in line.split(":", 1))
            what = ALIASES.get(name.lower())
            if what is None:
                skipped.append(f"{name[:30]}: not a look setting")
                continue
            value = value.removesuffix("!important").strip().strip("'\"")
            got = _rule(what, value)
            if got is None:
                skipped.append(f"{name[:30]}: {value[:30] or 'empty'} isn't understood")
                continue
            if what in ("hide", "show"):
                rules[what] = rules.get(what, frozenset()) | got
            else:
                rules[what] = got
    except Exception as exc:  # noqa: BLE001 (a look can't break a plugin: use what was read)
        skipped.append(f"stopped reading: {exc.__class__.__name__}")
    return rules, tuple(skipped)


def _rule(what: str, value: str) -> Any:
    v = value.strip()
    if what in ("text", "card", "accent"):
        return colour(v)
    if what == "font":
        name = v.split(",")[0].strip().strip("'\"")
        return name if _FONT_NAME.fullmatch(name) else None
    if what == "size":
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(%|x|em)?", v.lower())
        if not m:
            return None
        n = float(m.group(1))
        n = n / 100 if m.group(2) == "%" or (m.group(2) is None and n > 4) else n
        return n if 0.5 <= n <= 2.0 else None
    if what == "corners":
        low = v.lower()
        if low in CORNER_NAMES:
            return low
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(px)?", low)
        return min(60.0, float(m.group(1))) if m else None
    if what == "border":
        low = v.lower()
        if low in ("none", "off", "0", "no"):
            return (0.0, None)
        width, col = None, None
        for word in low.replace("px", " ").split():
            if word == "solid":
                continue
            if re.fullmatch(r"\d+(?:\.\d+)?", word) and width is None:
                width = min(12.0, float(word))
            elif colour(word):
                col = colour(word)
            else:
                return None
        return (2.0 if width is None else width, col)
    if what == "shadow":
        low = v.lower()
        if low in ("on", "yes", "true", "1"):
            return True
        if low in ("off", "no", "false", "0", "none"):
            return False
        return True if re.fullmatch(r"[\w\s#.,()%-]+", low) else None  # a CSS box-shadow: just "on"
    if what in ("hide", "show"):
        parts = {p.strip().lower().replace(" ", "_").removeprefix("show_") for p in v.split(",")}
        parts.discard("")
        return frozenset(parts) if parts and all(_PART.fullmatch(p) for p in parts) else None
    return None


# Resolving -----------------------------------------------------------------------------------------------

def resolve(settings: dict[str, Any], base: dict[str, Any] | None = None) -> Style:
    """The look these settings ask for. base: the plugin's own defaults (its colours, corners). Never raises:
    anything that isn't valid falls back to the plugin's own look."""
    base = base or {}
    try:
        return _resolve(_items(settings, base), _items(base, {}))
    except Exception:  # noqa: BLE001
        return Style()


def _items(settings: dict[str, Any], base: dict[str, Any]) -> tuple:
    keys = ("text_color", "card_color", "accent_color", "font", "text_scale", "corners", "custom_look")
    return tuple((k, _hashable(settings.get(k, base.get(k)))) for k in keys)


def _hashable(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool, type(None))) else str(value)


@lru_cache(maxsize=128)
def _resolve(items: tuple, base_items: tuple) -> Style:
    s, b = dict(items), dict(base_items)
    own = Style()
    text = _setting_colour(s, "text_color", b.get("text_color") or own.text)
    card = _setting_colour(s, "card_color", b.get("card_color") or own.card)
    accent = _setting_colour(s, "accent_color", b.get("accent_color") or own.accent)
    font = str(s.get("font") or "").strip()
    font = font if _FONT_NAME.fullmatch(font or "-") and not font.lower().endswith((".ttf", ".otf")) else ""
    try:
        scale = min(2.0, max(0.5, float(s.get("text_scale") or 100) / 100))
    except (TypeError, ValueError):
        scale = 1.0
    corners = str(s.get("corners") or b.get("corners") or "round").lower()
    corners = corners if corners in CORNER_NAMES else "round"
    radius, border_w, border_c, shadow = None, 0.0, own.border_color, False
    hidden: frozenset = frozenset()
    shown: frozenset = frozenset()
    rules, problems = parse_look(str(s.get("custom_look") or ""))
    text = rules.get("text", text)
    card = rules.get("card", card)
    accent = rules.get("accent", accent)
    font = rules.get("font", font)
    scale = rules.get("size", scale)
    if "corners" in rules:
        if isinstance(rules["corners"], str):
            corners = rules["corners"]
        else:
            radius = rules["corners"]
            corners = "square" if radius == 0 else "round"
    if "border" in rules:
        border_w, col = rules["border"]
        border_c = col or text
    shadow = rules.get("shadow", shadow)
    hidden = rules.get("hide", hidden)
    shown = rules.get("show", shown) - hidden
    return Style(text=text, card=card, accent=accent, font=font, scale=scale, corners=corners, radius=radius,
                 border_width=border_w, border_color=border_c, shadow=shadow, hidden=hidden, shown=shown,
                 problems=problems)


def look_problems(text: str) -> list[str]:
    """What's wrong in a custom look, for the line under its box (empty: all of it is used)."""
    _, problems = parse_look(str(text or ""))
    out = list(problems[:MOST_SKIPPED])
    if len(problems) > MOST_SKIPPED:
        out.append(f"and {len(problems) - MOST_SKIPPED} more")
    return out


def is_style_key(key: str) -> bool:
    return key in STYLE_KEYS or key.startswith("show_")


def style_keys(defaults: Iterable[str]) -> list[str]:
    return [k for k in defaults if is_style_key(k)]


# Drawing (Qt, imported only when a card is drawn) -----------------------------------------------------------

def qcolor(hex_rgba: str):
    from PySide6.QtGui import QColor

    c = QColor(hex_rgba[:7])
    if len(hex_rgba) == 9:
        c.setAlpha(int(hex_rgba[7:], 16))
    return c


def qfont(style: Style, size: float, weight=None):
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(style.families())
    font.setPixelSize(style.px(size))
    font.setWeight(weight if weight is not None else QFont.Weight.Bold)
    return font


def card_path(rect, radius: float, style: Style):
    """The card's outline: round corners, leaf (big top-left and bottom-right, small others) or square."""
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QPainterPath

    r = style.corner(radius, rect.height())
    path = QPainterPath()
    if style.corners != "leaf" or r <= 0:
        path.addRoundedRect(QRectF(rect), r, r)
        return path
    big, small = r * 1.5, max(2.0, r * 0.25)
    big = min(big, rect.height() / 2, rect.width() / 2)
    x0, y0, x1, y1 = rect.left(), rect.top(), rect.right(), rect.bottom()
    path.moveTo(QPointF(x0 + big, y0))
    path.lineTo(QPointF(x1 - small, y0))
    path.quadTo(QPointF(x1, y0), QPointF(x1, y0 + small))
    path.lineTo(QPointF(x1, y1 - big))
    path.quadTo(QPointF(x1, y1), QPointF(x1 - big, y1))
    path.lineTo(QPointF(x0 + small, y1))
    path.quadTo(QPointF(x0, y1), QPointF(x0, y1 - small))
    path.lineTo(QPointF(x0, y0 + big))
    path.quadTo(QPointF(x0, y0), QPointF(x0 + big, y0))
    path.closeSubpath()
    return path


def draw_card(p, rect, radius: float, style: Style, card=None):
    """Fill the card (with its shadow and border, if the look has them). Returns its outline, for clipping."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPen

    path = card_path(rect, radius, style)
    fill = card if card is not None else qcolor(style.card)
    p.save()
    p.setPen(Qt.PenStyle.NoPen)
    if style.shadow and fill.alpha():
        for i, a in ((10, 18), (6, 26), (3, 34)):  # a soft drop shadow, a few layers (no blur pass)
            p.setBrush(QColor(0, 0, 0, int(a * fill.alphaF())))
            p.drawPath(path.translated(0, i))
    if fill.alpha():
        p.setBrush(fill)
        p.drawPath(path)
    if style.border_width > 0:
        pen = QPen(qcolor(style.border_color), style.border_width)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        w = style.border_width / 2
        p.drawPath(card_path(rect.adjusted(w, w, -w, -w), max(0.0, radius - w), style))
    p.restore()
    return path
