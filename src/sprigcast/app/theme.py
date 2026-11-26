"""The Slothbloom look, "Night Canopy": calm near-neutral greens, one green accent, pink only for Live.

Uses Qt's Fusion style so it looks the same on every Windows version. Settings → General picks
the theme (saved in QSettings "general/theme"); the environment variable SPRIGCAST_THEME=system
overrides it.
"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, QStandardPaths, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen, QTransform
from PySide6.QtWidgets import QApplication

# Night Canopy: near-neutral deep greens, hairline edges, one green accent for actions and
# selection, and pink kept for the things that matter on stream: Live and Recording.
BG = "#0e1311"  # window background
CHROME = "#0b0f0d"  # menu bar, toolbars, status bar and wells (meters, text boxes)
PANEL = "#151c19"  # docks, lists, cards and pop-ups
RAISED = "#1c2420"  # buttons
RAISED_HOVER = "#232d28"
PRESSED = "#18201c"
CARD = "#19221e"  # a soft card on a pop-up (pop-ups are PANEL)
EDGE = "#1f2825"  # hairline borders
EDGE_STRONG = "#2b3631"  # borders that must read (inputs, pop-up frames)
HIGHLIGHT = "#28322d"  # the faint lit top edge on raised things
TEXT = "#e6ede8"
TEXT_BRIGHT = "#f2f7f3"
MUTED = "#8b9a92"
ACCENT = "#4fd88f"  # actions, focus and selection
ACCENT_HOVER = "#6be3a3"
ACCENT_TEXT = "#07130d"  # text on an ACCENT fill
SELECT_BG = "#1d3428"  # ACCENT at 13% over PANEL
SELECT_TEXT = "#bff3d6"
LIVE = "#f0477f"  # on air and recording only
LIVE_HOVER = "#f4699a"
LIVE_EDGE = "#c2305f"
# Buttons: one look everywhere, a lighter fill with a visible rounded edge and a faintly lit top, so a
# button always reads as a button on any surface (BG, CHROME, PANEL, CARD or RAISED). See button_style().
BUTTON = "#2a3731"
BUTTON_HOVER = "#34433c"
BUTTON_PRESSED = "#222d28"
BUTTON_EDGE = "#4a5a52"  # about 2.2:1 against PANEL and CARD, where RAISED and EDGE_STRONG were 1.1-1.4:1
BUTTON_EDGE_HOVER = "#62766c"
BUTTON_TOP = "#5a6b62"  # the lit top edge
BUTTON_OFF_EDGE = "#36433d"  # a disabled button keeps its shape, just quieter
# Titled sections (Settings and other pop-ups): a card that stands off the window, fields that sink into
# it, labels bright enough to read at a glance. Contrast: card 2.0:1 on BG, field edge 3.2:1 on the card,
# labels 9.5:1 on the card.
SECTION = "#1d2823"
SECTION_EDGE = "#3b4b43"
FIELD = CHROME
FIELD_EDGE = "#62776c"
LABEL = "#c3cfc8"
SECTION_TITLE_PX = 15
WARN = "#e6c55a"
# Level bars (0..1 of -60..0 dBFS, following the peak): yellow above -18 dB, pink above -3 dB (about to clip).
LOUD_LEVEL = 0.7
CLIP_LEVEL = 0.95
BAD = "#f2665e"
INFO = "#7fb8ff"

# Settings → Appearance: text size and a color preset. Both are read once, when SprigCast starts: every style in
# the app is built from the names above as it loads, so a change shows after a restart (Settings says so).
TEXT_SIZE_KEY = "appearance/text_size"
COLORS_KEY = "appearance/colors"
TEXT_SIZES = {"small": ("Small", 0.9), "normal": ("Normal", 1.0), "large": ("Large", 1.15)}
COLOR_PRESETS = {"default": "Default", "colorblind": "Color-blind friendly", "contrast": "High contrast"}
# What each preset changes. Color-blind friendly: blue, yellow and orange-red in place of green, yellow and red
# (told apart with the common kinds of color blindness). High contrast: brighter text and stronger edges.
PRESET_COLORS: dict[str, dict[str, str]] = {
    "default": {},
    "colorblind": {
        "ACCENT": "#56b4e9", "ACCENT_HOVER": "#7cc6ef", "ACCENT_TEXT": "#06121a", "SELECT_BG": "#17303f",
        "SELECT_TEXT": "#c4e4f7", "WARN": "#f0e442", "BAD": "#e8743b", "INFO": "#c9a7ff",
    },
    "contrast": {
        "BG": "#060908", "TEXT": "#ffffff", "TEXT_BRIGHT": "#ffffff", "MUTED": "#b4c2ba", "EDGE": "#3b4b43",
        "EDGE_STRONG": "#62776c", "SECTION_EDGE": "#62776c", "FIELD_EDGE": "#8da196", "LABEL": "#e3ebe6",
        "BUTTON_EDGE": "#7d9187", "BUTTON_TOP": "#8da196",
    },
}


# Sound type colors (the Sound card's groups, Build's sound strips and Type filter). Fixed, not turned with the
# theme: a theme's accent would land on another type's color (Autumn's amber on Orange, Moonlit's aqua on Teal).
# None is pink (Live's). Each reads at 3:1 or more on every surface in every theme. The color-blind set is told
# apart with red-green and blue-yellow color blindness (tests/test_sound_types.py simulates both).
TYPE_COLOR_SETS: dict[str, dict[str, str]] = {
    "default": {"green": "#4fd88f", "blue": "#7fb8ff", "yellow": "#e6c55a", "violet": "#c79bff",
                "teal": "#5fd0c5", "orange": "#f2a65a", "brown": "#b08968", "gray": "#8b9a92"},
    "colorblind": {"green": "#56b4e9", "blue": "#e69f00", "yellow": "#f0e442", "violet": "#91cba9",
                   "teal": "#dcdcdc", "orange": "#c0826c", "brown": "#9a7a3a", "gray": "#7d8681"},
}
TYPE_COLOR_NAMES: dict[str, dict[str, str]] = {  # what each swatch is called under a preset (its tooltip)
    "default": {"green": "Green", "blue": "Blue", "yellow": "Yellow", "violet": "Violet", "teal": "Teal",
                "orange": "Orange", "brown": "Brown", "gray": "Gray"},
    "colorblind": {"green": "Sky blue", "blue": "Orange", "yellow": "Yellow", "violet": "Mint", "teal": "White",
                   "orange": "Clay", "brown": "Olive", "gray": "Gray"},
}


def type_colors(preset: str | None = None) -> dict[str, str]:
    """Sound type colors for a color preset (default: the one this run is drawn with)."""
    preset = colors_in_use if preset is None else preset
    return dict(TYPE_COLOR_SETS.get(preset, TYPE_COLOR_SETS["default"]))


def type_color_names(preset: str | None = None) -> dict[str, str]:
    preset = colors_in_use if preset is None else preset
    return dict(TYPE_COLOR_NAMES.get(preset, TYPE_COLOR_NAMES["default"]))


def preset_colors(name: str) -> dict[str, str]:
    """The good / warning / bad colors a preset shows (for its preview in Settings)."""
    base = {"ACCENT": "#4fd88f", "WARN": "#e6c55a", "BAD": "#f2665e"}
    return {**base, **{k: v for k, v in PRESET_COLORS.get(name, {}).items() if k in base}}


def _settings() -> QSettings:
    """SprigCast's settings, also before the app has been given its name (this module loads before that)."""
    from PySide6.QtCore import QCoreApplication

    return QSettings() if QCoreApplication.organizationName() else QSettings("Slothbloom", "SprigCast")


def chosen_colors() -> str:
    try:
        name = str(_settings().value(COLORS_KEY, "default"))
    except Exception:  # noqa: BLE001  (no settings yet)
        return "default"
    return name if name in COLOR_PRESETS else "default"


def chosen_text_size() -> str:
    try:
        name = str(_settings().value(TEXT_SIZE_KEY, "normal"))
    except Exception:  # noqa: BLE001
        return "normal"
    return name if name in TEXT_SIZES else "normal"


# Themes: Night Canopy in other colors. Each one keeps every surface, edge and text exactly as bright as Night
# Canopy's (so buttons, sections and labels read just as well) and only turns the hue: `neutral` is the hue and
# the strength of the backgrounds and greys, `accent` the hue of the one action color. Pink stays for Live and
# Recording in all of them, and warning yellow and bad red never move. Like text size and colors, a theme is
# read once, when SprigCast starts.
THEME_KEY = "general/theme"
THEMES = {
    "slothbloom": "Night Canopy",
    "moonlit": "Moonlit Pond",
    "orchid": "Dusk Orchid",
    "autumn": "Autumn Leaf",
    "stone": "River Stone",
    "system": "System (Windows default)",
}
THEME_LOOKS: dict[str, dict[str, tuple[float, float] | None]] = {
    "slothbloom": {"neutral": None, "accent": None},
    "moonlit": {"neutral": (218.0, 1.6), "accent": (190.0, 1.2)},  # deep blue water, an aqua accent
    "orchid": {"neutral": (266.0, 1.5), "accent": (262.0, 1.4)},  # dusk violet, a lavender accent
    "autumn": {"neutral": (26.0, 1.3), "accent": (32.0, 1.5)},  # warm bark, an amber accent
    "stone": {"neutral": (215.0, 0.3), "accent": None},  # near-grey stone, Night Canopy's green accent
}
THEME_ABOUT = {
    "slothbloom": "Deep greens, a green accent",
    "moonlit": "Deep blues, an aqua accent",
    "orchid": "Dusk violets, a lavender accent",
    "autumn": "Warm bark, an amber accent",
    "stone": "Quiet greys, a green accent",
}


def _rgb(color: str) -> tuple[float, float, float]:
    return tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))  # type: ignore[return-value]


def luminance(color: str) -> float:
    """How bright a color looks, 0 .. 1 (WCAG relative luminance)."""
    r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in _rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def tint(color: str, look: dict[str, tuple[float, float] | None]) -> str:
    """A Night Canopy color in a theme's hue, exactly as bright as it was. Only Night Canopy's greens turn:
    strong ones (the accent family) to the theme's accent, soft ones (surfaces, edges, text) to its neutral."""
    import colorsys

    if not (isinstance(color, str) and len(color) == 7 and color.startswith("#")):
        return color
    r, g, b = _rgb(color)
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    if sat < 0.02 or not 90 <= hue * 360 <= 185:
        return color
    to = look.get("accent" if sat >= 0.25 else "neutral")
    if to is None:
        return color
    new_hue, new_sat = to[0] / 360, min(1.0, sat * to[1])
    want, low, high = luminance(color), 0.0, 1.0
    for _ in range(24):  # the lightness in the new hue that looks as bright as the old color did
        mid = (low + high) / 2
        out = "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hls_to_rgb(new_hue, mid, new_sat))
        if luminance(out) < want:
            low = mid
        else:
            high = mid
    return out


def chosen_theme() -> str:
    if os.environ.get("SPRIGCAST_THEME", "").lower() == "system":
        return "system"
    try:
        name = str(_settings().value(THEME_KEY, "slothbloom"))
    except Exception:  # noqa: BLE001  (no settings yet)
        return "slothbloom"
    return name if name in THEMES else "slothbloom"


_NIGHT_CANOPY: dict[str, str] = {name: value for name, value in list(globals().items())
                                 if name.isupper() and isinstance(value, str) and len(value) == 7
                                 and value.startswith("#")}


def palette(theme_name: str = "slothbloom", preset: str = "default") -> dict[str, str]:
    """Every theme color by name, for a theme and a color preset (what Settings → Appearance previews, and
    what this run is drawn with)."""
    look = THEME_LOOKS.get(theme_name) or THEME_LOOKS["slothbloom"]
    colors = {**_NIGHT_CANOPY, **PRESET_COLORS.get(preset, {})}
    return {name: tint(value, look) for name, value in colors.items()}


def _themed(name: str, color: str) -> str:
    """A color named further down this file, written in Night Canopy's greens: in the theme this run uses."""
    _NIGHT_CANOPY[name] = color
    return tint(color, THEME_LOOKS[theme_in_use])


_chosen = chosen_theme()
theme_in_use = _chosen if _chosen in THEME_LOOKS else "slothbloom"  # the colors this run is drawn with
colors_in_use = chosen_colors()  # what this run is drawn with (Settings compares it to what's saved)
text_size_in_use: str | None = None  # set when the theme is first applied
globals().update(palette(theme_in_use, colors_in_use))

# Older names, kept so plugins and older code keep working.
BARK, MOSS, LICHEN, LEAF, BLOOM, INPUT, PETAL, DUSK = BG, PANEL, EDGE_STRONG, ACCENT, LIVE, CHROME, TEXT, MUTED

ON_AIR_STYLE = (
    f"background: {LIVE}; color: #ffffff; font-weight: bold; border: 1px solid {LIVE_EDGE};"
    f" border-top-color: {LIVE_HOVER}; border-radius: 14px;"
)
GO_LIVE_STYLE = (  # Go live waits in calm green; once on air the button turns LIVE pink (ON_AIR_STYLE)
    f"QPushButton {{ background: {ACCENT}; color: {ACCENT_TEXT}; font-weight: bold; border: 1px solid {ACCENT};"
    f" border-radius: 14px; }}"
    f"QPushButton:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}"
    f"QPushButton:disabled {{ background: transparent; color: {MUTED}; border-color: {BUTTON_OFF_EDGE}; }}"
)
PRIMARY_STYLE = (  # the one big "do it" button on a panel, such as Go
    f"QPushButton {{ font-weight: bold; padding: 6px 28px; }}"
    f"QPushButton:enabled {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: 1px solid {ACCENT}; }}"
    f"QPushButton:enabled:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}"
)
NOTE_STYLE = f"background: {SELECT_BG}; color: {SELECT_TEXT}; border-radius: 12px;"  # inline notes and banners
MUTED_STYLE = f"color: {MUTED}"


def button_style(radius: int = 13, padding: str = "6px 14px", selector: str = "QPushButton", extra: str = "") -> str:
    """The one SprigCast button: a lighter rounded fill with a visible edge, so it never blends into the
    card, pop-up or panel behind it. Hover lifts it, pressed sinks it, checked turns it green-tinted,
    disabled keeps the shape but goes quiet. `selector` may list several ("QPushButton, QToolButton").
    Every one-off button style in the app builds on this rather than painting its own fill."""
    parts = [x.strip() for x in selector.split(",")]
    sel = lambda state: ", ".join(f"{x}{state}" for x in parts)  # noqa: E731
    return (
        f"{sel('')} {{ background: {BUTTON}; color: {TEXT}; border: 1px solid {BUTTON_EDGE};"
        f" border-top-color: {BUTTON_TOP}; border-radius: {radius}px; padding: {padding}; font-weight: 600; {extra} }}"
        f"{sel(':hover')} {{ background: {BUTTON_HOVER}; border-color: {BUTTON_EDGE_HOVER}; }}"
        f"{sel(':pressed')} {{ background: {BUTTON_PRESSED}; border-color: {BUTTON_EDGE}; }}"
        f"{sel(':checked')} {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}"
        f"{sel(':disabled')} {{ background: transparent; color: {MUTED}; border-color: {BUTTON_OFF_EDGE}; }}"
        + "".join(f"{x}::menu-indicator {{ width: 0; image: none; }}" for x in parts)
    )


def round_button_style(size: int = 28) -> str:
    """A round icon button (the mute and gear under each sound strip): a soft fill with no hard edge, a lighter
    fill on hover, a soft red when checked (muted: "off", never green) and a green ring only for keyboard focus.
    Its height is pinned in the style too: the app's min-height otherwise squeezed these to 20 px, too short for
    their round ends, so Qt drew them square (StreamBridge / user 2026-11-21)."""
    bad = QColor(BAD)
    off = f"rgba({bad.red()}, {bad.green()}, {bad.blue()}, 46)"
    inner = size - 4  # within the 2 px focus ring
    return (
        f"QPushButton {{ background: {BUTTON}; border: 2px solid transparent; border-radius: {size // 2}px;"
        f" padding: 0; min-height: {inner}px; max-height: {inner}px; min-width: {inner}px; max-width: {inner}px; }}"
        f"QPushButton:hover {{ background: {BUTTON_HOVER}; }}"
        f"QPushButton:pressed {{ background: {BUTTON_PRESSED}; }}"
        f"QPushButton:checked {{ background: {off}; }}"
        f"QPushButton:checked:hover {{ background: {off}; border-color: transparent; }}"
        f"QPushButton:focus {{ border-color: {ACCENT}; }}"
    )


_CROSS_ICONS: dict = {}  # (colour, size): one drawn × for every button that shows it


def cross_icon(colour: str | None = None, size: int = 10):
    """A drawn ×: two even strokes with round ends, centred and sharp at any scale. The letter × sat small
    and off centre, and its button took the letter's shape (user 2026-11-19: "fix the shape of the x that
    closes, across the app")."""
    from PySide6.QtGui import QIcon, QPixmap

    key = (colour or TEXT, size)
    if key not in _CROSS_ICONS:
        ratio = 3.0
        pixmap = QPixmap(round(size * ratio), round(size * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(key[0]), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(1.5, 1.5), QPointF(size - 1.5, size - 1.5))
        p.drawLine(QPointF(size - 1.5, 1.5), QPointF(1.5, size - 1.5))
        p.end()
        _CROSS_ICONS[key] = QIcon(pixmap)
    return _CROSS_ICONS[key]


def make_cross(button, size: int = 30, styled: bool = True, extra: str = ""):
    """Turn `button` into the app's one × button: a true circle of `size` with the drawn cross in its
    middle. styled=False leaves the look to a style sheet further up (set its radius to size // 2 - 1)."""
    from PySide6.QtCore import QSize

    button.setText("")
    button.setIcon(cross_icon())
    button.setIconSize(QSize(10, 10))
    button.setFixedSize(size, size)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    if styled:
        # The size is pinned in the style too: the app's QPushButton min-height (18 px) otherwise won over
        # setFixedSize and squeezed every × to 20 px tall, a flat rectangle instead of a circle (user 2026-11-22).
        inner = size - 2  # inside the 1 px edge
        pin = f"min-height: {inner}px; max-height: {inner}px; min-width: {inner}px; max-width: {inner}px;"
        button.setStyleSheet(button_style(size // 2 - 1, "0px", extra=f"{extra} {pin}"))
    return button


def paint_button(p: QPainter, rect: QRectF, radius: float, hover: bool = False) -> None:
    """Draw the shared button shape (fill, edge, lit top) for painted widgets such as bloom tiles."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
    p.setPen(QPen(QColor(BUTTON_EDGE_HOVER if hover else BUTTON_EDGE), 1))
    p.setBrush(QColor(BUTTON_HOVER if hover else BUTTON))
    p.drawRoundedRect(r, radius, radius)
    p.restore()


# Live screen (see live_stage.py) and pop-ups: round pill buttons in the shared button look.
TRACK = _themed("TRACK", "#1a221e")  # the empty part of thin level bars
STAGE_EMPTY = _themed("STAGE_EMPTY", "#1d3428")  # a bloom tile before it has a picture
LIVE_TEXT = "#ff8fb4"  # pink text on dark, for the on-air clock
PILL_STYLE = button_style(19, "10px 14px")
SMALL_PILL_STYLE = button_style(13, "6px 13px", "QPushButton, QToolButton")  # chips and small tools
PILL_SELECTED_STYLE = PILL_STYLE + f"QPushButton {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}"
PILL_ON_AIR_STYLE = PILL_STYLE + f"QPushButton {{ background: {LIVE}; color: #ffffff; border-color: {LIVE_EDGE}; }}" \
    f"QPushButton:hover {{ background: {LIVE_HOVER}; border-color: {LIVE_HOVER}; }}"
BIG_GO_STYLE = (
    f"QPushButton {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: none; border-radius: 24px; padding: 14px;"
    f" font-size: 16px; font-weight: 700; }}"
    f"QPushButton:hover {{ background: {ACCENT_HOVER}; }}"
    f"QPushButton:disabled {{ background: transparent; color: {MUTED}; border: 1px solid {BUTTON_OFF_EDGE}; }}"
)
BIG_END_STYLE = BIG_GO_STYLE + f"QPushButton {{ background: {LIVE}; color: #ffffff; }}" \
    f"QPushButton:hover {{ background: {LIVE_HOVER}; }}"
BLOOM_PILL_STYLE = (  # Build's bloom tabs: a swatch and a name
    f"QPushButton {{ background: transparent; color: {MUTED}; border: none; border-radius: 18px;"
    f" padding: 5px 16px 5px 5px; font-weight: 500; text-align: left; min-height: 26px; }}"
    f"QPushButton:hover {{ color: {TEXT}; background: {PANEL}; }}"
    f"QPushButton:checked {{ background: {RAISED_HOVER}; color: {TEXT_BRIGHT}; font-weight: 600; }}"
)
SOFT_FIELD_STYLE = (  # borderless inputs on the Build inspector
    f"QLineEdit, QDoubleSpinBox {{ background: {PANEL}; border: none; border-radius: 12px; padding: 8px 10px;"
    f" color: {TEXT}; }}"
    f"QLineEdit:focus, QDoubleSpinBox:focus {{ background: {RAISED}; }}"
    # No arrow buttons (type, scroll or use the arrow keys), so the number is centred on the whole box.
    # setButtonSymbols(NoButtons) can't be used: with the app stylesheet it shrinks the text field to 1 px.
    "QDoubleSpinBox { padding: 6px 0px; }"
    "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {"
    " subcontrol-origin: margin; width: 0px; height: 0px; border: none; }"
)
# Small header buttons (a card's Edit, + Add, ⚙; a pop-up's "Last streams"): a compact button, never bare text.
LINK_STYLE = button_style(12, "4px 11px")

# Every scroll bar keeps clear of what it scrolls: the bar's own left margin (8 px) is the gap, so
# rows, buttons and text never touch it, and nothing else needs its own padding. The handle is 6 px wide
# at the far edge; the top and bottom margins keep it off rounded corners.
SCROLL_GAP = 8
SCROLL_BARS = f"""
QScrollBar:vertical {{ background: transparent; width: {6 + SCROLL_GAP + 4}px; margin: 8px 4px 8px {SCROLL_GAP}px; }}
QScrollBar:horizontal {{ background: transparent; height: {6 + SCROLL_GAP + 4}px; margin: {SCROLL_GAP}px 8px 4px 8px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {EDGE_STRONG}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 30px; }}
QScrollBar::handle:horizontal {{ min-width: 30px; }}
QScrollBar::handle:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
""".strip()


STYLESHEET = f"""
QWidget {{ selection-background-color: {SELECT_BG}; selection-color: {SELECT_TEXT}; }}
QMainWindow::separator {{ background: {BG}; width: 6px; height: 6px; }}
QToolTip {{ color: {TEXT}; background: {PANEL}; border: 1px solid {EDGE_STRONG}; border-radius: 10px; padding: 5px 9px; }}
QDockWidget {{ color: {MUTED}; }}
QDockWidget::title {{ background: {CHROME}; padding: 6px 8px; border-bottom: 1px solid {EDGE}; }}
QToolBar {{ background: {CHROME}; border: none; border-bottom: 1px solid {EDGE}; spacing: 4px; padding: 2px; }}
QPushButton {{
    background: {BUTTON}; color: {TEXT};
    border: 1px solid {BUTTON_EDGE}; border-top-color: {BUTTON_TOP};
    border-radius: 13px; padding: 6px 14px; min-height: 18px; text-align: center;
}}
QPushButton:hover {{ background: {BUTTON_HOVER}; border-color: {BUTTON_EDGE_HOVER}; }}
QPushButton:pressed {{ background: {BUTTON_PRESSED}; border-color: {BUTTON_EDGE}; padding-top: 7px; padding-bottom: 5px; }}
QPushButton:checked {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}
QPushButton:default {{ background: {ACCENT}; color: {ACCENT_TEXT}; border-color: {ACCENT}; font-weight: 600; }}
QPushButton:default:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton:disabled {{ color: {MUTED}; background: transparent; border-color: {BUTTON_OFF_EDGE}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 10px; padding: 3px 6px; color: {TEXT}; }}
QToolButton:hover {{ background: {RAISED_HOVER}; border-color: {EDGE_STRONG}; }}
QToolButton[autoRaise="false"] {{
    background: {BUTTON}; border: 1px solid {BUTTON_EDGE}; border-top-color: {BUTTON_TOP}; padding: 3px 10px;
}}
QToolButton:pressed {{ background: {PRESSED}; }}
QToolButton:checked {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}
QDialog, QMessageBox {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; }}
QGroupBox {{
    background: {SECTION}; border: 1px solid {SECTION_EDGE}; border-radius: 18px; margin-top: 0px;
    padding: 46px 16px 14px 16px;
}}
QGroupBox[title=""] {{ padding-top: 14px; }}
QGroupBox::title {{ subcontrol-origin: padding; subcontrol-position: top left; left: 18px; top: 14px; padding: 0px;
    color: {TEXT_BRIGHT}; font-size: {SECTION_TITLE_PX}px; font-weight: 600; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {FIELD}; color: {TEXT}; border: 1px solid {FIELD_EDGE}; border-radius: 10px; padding: 3px 8px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; border-radius: 12px; outline: none;
    padding: 4px; }}
QComboBox QAbstractItemView::item {{ padding: 6px 10px; min-height: 18px; border-radius: 9px; border: none; }}
QComboBox QAbstractItemView::item:hover {{ background: {RAISED}; }}
QComboBox QAbstractItemView::item:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {EDGE_STRONG}; background: {CHROME};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox, QRadioButton {{ spacing: 8px; }}
QListWidget, QTreeWidget, QListView, QTreeView, QTableView {{
    background: {PANEL}; border: 1px solid {EDGE}; border-radius: 14px; outline: none;
}}
QListWidget::item, QTreeWidget::item {{ padding: 3px 6px; border-radius: 7px; }}
QTreeView {{ show-decoration-selected: 0; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {RAISED}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}
QListWidget::indicator {{ width: 12px; height: 12px; border: 1px solid {EDGE_STRONG}; border-radius: 3px; background: {CHROME}; }}
QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QHeaderView::section {{ background: {CHROME}; color: {MUTED}; border: none; border-bottom: 1px solid {EDGE}; padding: 4px; }}
QTabWidget::pane {{ border: 1px solid {EDGE}; border-radius: 14px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 6px 14px; margin: 2px; border: none; border-radius: 12px;
    min-height: 16px; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border: none; }}
QMenuBar {{ background: {CHROME}; color: {MUTED}; border-bottom: 1px solid {EDGE}; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 9px; }}
QMenuBar::item:selected {{ background: {RAISED}; color: {TEXT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; border-radius: 14px; padding: 7px; }}
QMenu::item {{ padding: 7px 26px 7px 14px; border-radius: 9px; color: {TEXT}; }}
QMenu::icon {{ padding-left: 10px; }}
QMenu::right-arrow {{ width: 8px; height: 8px; margin-right: 6px; }}
QMenu::item:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {EDGE}; margin: 4px 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: {EDGE_STRONG}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT_BRIGHT}; border: 2px solid {ACCENT}; width: 10px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QProgressBar {{ background: {CHROME}; border: 1px solid {EDGE}; border-radius: 7px; text-align: center; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 6px; }}
{SCROLL_BARS}
QStatusBar {{ background: {CHROME}; color: {MUTED}; border-top: 1px solid {EDGE}; }}
QSplitter::handle {{ background: {BG}; }}
"""


looks_applied = 0  # how many times a theme was applied: a font may have changed since a widget measured itself
_applied: str | None = None  # the theme last applied: re-applying it restyles every widget (~1 s)

_system_style: str | None = None


FONT_ENGINE = "SPRIGCAST_FONT_ENGINE"  # "directwrite" keeps Qt's default font engine


def use_light_font_engine() -> None:
    """Windows: draw text with Qt's GDI font engine. Call before the QApplication is made.

    Qt's default DirectWrite engine took 16 MB more on first use (memcheck --text: 40 MB against
    24 MB for the same text). GDI draws the same fonts; emoji come out in one colour instead of
    full colour, which suits SprigCast's small icons. SPRIGCAST_FONT_ENGINE=directwrite opts out,
    and a QT_QPA_PLATFORM set by hand always wins.
    """
    if sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get(FONT_ENGINE, "").strip().lower() == "directwrite":
        return
    os.environ["QT_QPA_PLATFORM"] = "windows:fontengine=gdi"


def _set_symbol_fallbacks(app: QApplication) -> None:
    """Point Qt straight at Windows' symbol and emoji fonts for icons like ● ⚙ ▲ 🩺.

    Without this, Qt looks for a missing glyph through its generic fallback list, which loads large
    Chinese and Japanese fonts (SimSun, MingLiU, Microsoft YaHei, ~120 MB of RAM) on the way.
    """
    if sys.platform != "win32" or os.environ.get("SPRIGCAST_NO_SYMBOL_FONTS"):
        return
    font = app.font()
    font.setFamilies([font.family(), "Segoe UI Symbol", "Segoe UI Emoji"])
    app.setFont(font)


def _set_text_size(app: QApplication) -> None:
    """Settings → Appearance → Text size: the app's font, a little smaller or larger. Once, at start."""
    global text_size_in_use
    text_size_in_use = chosen_text_size()
    scale = TEXT_SIZES[text_size_in_use][1]
    if scale != 1.0:
        font = app.font()
        if font.pointSizeF() > 0:
            font.setPointSizeF(font.pointSizeF() * scale)
        elif font.pixelSize() > 0:
            font.setPixelSize(round(font.pixelSize() * scale))
        app.setFont(font)


def guarded_style(base: str):
    """The system look. A plain Qt style: no Python in it (see roomy_style)."""
    from PySide6.QtWidgets import QStyleFactory

    fit_widgets()
    return QStyleFactory.create(base)


def roomy_style():
    """Fusion. A plain Qt style with no Python in it: what every drop-down, number box and menu needs is done
    as each one is made (fit_widgets), not in the style.

    The style used to be a Python subclass whose polish() Qt called for every widget: ~3,600 calls for
    Settings → Stream, each waiting for Python's lock while the render thread and workers ran. Opening a
    pop-up froze the window and the canvases for a second (PC 2026-11-18: Settings → Stream 1.5 s, the minute
    at 26 fps). Measured with two busy Python threads: 1,348 ms with the Python style, 291 ms without.
    """
    from PySide6.QtWidgets import QStyleFactory

    fit_widgets()
    # A new one each time: QApplication.setStyle takes ownership and deletes the style it replaces.
    return QStyleFactory.create("Fusion")


_fitted = False


def fit_widgets() -> None:
    """Once: each drop-down and number box gets the wheel guard as it's made (and a number box room for its
    number, see wheel_guard), and each menu is a rounded card before it's first shown.
    These hook the Python calls that make or show them, so Qt never calls into Python for every widget."""
    global _fitted
    if _fitted:
        return
    _fitted = True
    from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QDoubleSpinBox, QMenu, QMenuBar, QPushButton,
                                   QSpinBox, QSystemTrayIcon, QToolButton)

    from sprigcast.app import wheel_guard

    for cls in (QComboBox, QSpinBox, QDoubleSpinBox):
        made = cls.__init__

        def init(self, *args, _made=made, **kwargs):
            _made(self, *args, **kwargs)
            wheel_guard.guard_new(self)
        cls.__init__ = init
    aligned = QAbstractSpinBox.setAlignment

    def set_alignment(self, *args, **kwargs):
        aligned(self, *args, **kwargs)
        wheel_guard.number_room(self)  # centred: room on both sides
    QAbstractSpinBox.setAlignment = set_alignment

    def before(cls, name: str, menu_at) -> None:
        call = getattr(cls, name)

        def hooked(self, *args, _call=call, **kwargs):
            menu = menu_at(self, args)
            if menu is not None:
                rounded_menus(menu)
            result = _call(self, *args, **kwargs)
            if isinstance(result, QMenu):  # addMenu("title") makes the submenu
                rounded_menus(result)
            return result
        setattr(cls, name, hooked)

    itself = lambda self, args: self if isinstance(self, QMenu) else None  # noqa: E731
    given = lambda self, args: next((a for a in args if isinstance(a, QMenu)), None)  # noqa: E731
    before(QMenu, "exec", itself)
    before(QMenu, "popup", itself)
    before(QMenu, "addMenu", given)
    before(QMenuBar, "addMenu", given)
    before(QPushButton, "setMenu", given)
    before(QToolButton, "setMenu", given)
    before(QSystemTrayIcon, "setContextMenu", given)


def rounded_menus(menu) -> None:
    """Every menu a true rounded card: no square window corners or square shadow behind it (and its submenus)."""
    from PySide6.QtWidgets import QMenu

    for each in [menu, *menu.findChildren(QMenu)]:
        try:
            if each.isVisible() or each.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
                continue  # done already (or showing: new window flags would hide it)
            each.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            each.setWindowFlags(each.windowFlags() | Qt.WindowType.FramelessWindowHint
                                | Qt.WindowType.NoDropShadowWindowHint)
        except RuntimeError:  # deleted meanwhile
            continue


# While the window builds widgets (Settings, any pop-up), Qt calls back into Python thousands of times
# (style polish, ~7,000 calls for Settings). Each call needs Python's lock, and while streaming and recording
# other threads hold it: with the usual 1 ms hand-over each call could wait about that long, so Settings
# took seconds to open and the whole window froze (PC test, 2026-11-17). For the moment widgets are being
# made, the lock changes hands every 0.1 ms instead; then the usual interval comes back.
#
# Only a window or pop-up being shown starts one (the window itself is polished before its widgets), and one
# lasts at most BURST_MAX_S. Started by any restyle, a label recoloured every second on the Live screen kept
# it on for good: every thread then swapped the lock every 0.1 ms, and the 2026-11-17 heavy run on build
# 44094725157 fell from ~55 to 14-41 fps with SprigCast's CPU doubled (10 to 22%).
BURST_SWITCH_S = 0.0001
BURST_HOLD_S = 0.4
BURST_MAX_S = 3.0
_burst_until = 0.0
_burst_began = 0.0
_usual_switch: float | None = None


def ui_burst(starts: bool = True) -> None:
    """Widgets are being made on the window's thread: hand Python's lock over quickly until it settles.

    starts=False only keeps a burst already running going (any widget polished meanwhile), up to BURST_MAX_S."""
    global _burst_until, _burst_began, _usual_switch
    now = time.monotonic()
    starting = _usual_switch is None
    if starting and not starts:
        return
    if starting:
        _burst_began = now
    elif now - _burst_began > BURST_MAX_S:
        return  # long enough: let it end on time, whatever keeps restyling
    _burst_until = now + BURST_HOLD_S
    if starting:
        _usual_switch = sys.getswitchinterval()
        sys.setswitchinterval(min(_usual_switch, BURST_SWITCH_S))
        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(BURST_HOLD_S * 1000), _burst_over)


def bursting() -> bool:
    """Widgets are being made (a pop-up opening): the window's own repaints can wait a moment (meters)."""
    return _usual_switch is not None


def _burst_over() -> None:
    global _usual_switch
    left = _burst_until - time.monotonic()
    if left > 0:  # still making widgets: look again then
        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(left * 1000) + 10, _burst_over)
        return
    if _usual_switch is not None:
        sys.setswitchinterval(_usual_switch)
        _usual_switch = None


def ticked(action, on: bool) -> None:
    """Mark the one that's on with a tick at the right end of its row, not a checkbox column.

    A checkable item makes Qt keep an empty tick column on the left of every row in the menu,
    so the lists in the top menus use plain items with the tick where a shortcut would go."""
    text = action.text().partition("\t")[0]
    action.setText(f"{text}\t✓" if on else text)


NUMBER_PAD = 12  # px between a number and the left edge of its box, the same as text fields


_ACTIVATION = frozenset((QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate))


def same_when_inactive(palette: QPalette) -> None:
    """The window's colours don't change when another window (a pop-up) takes the focus. Qt repaints a whole
    window on that change unless both colour groups match, and Windows' own palette (its accent, inactive
    selections) doesn't: every pop-up repainted the main window as it opened and again as it closed, its bloom
    tiles and panels painting in Python (76-214 ms of opening Settings or the Plugin store, PC 2026-11-18)."""
    active, inactive = QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive
    for role in QPalette.ColorRole:
        if role != QPalette.ColorRole.NColorRoles:
            palette.setBrush(inactive, role, palette.brush(active, role))


def view_activation(view: QWidget, event) -> bool | None:
    """For an item view's event(): a pop-up taking or giving back the focus doesn't repaint it. Qt's item views
    repaint their whole viewport on that change, whatever the colours; the Library's rows paint in Python, and
    every pop-up opened over Build mode repainted them twice (101-156 ms, PC 2026-11-18). Their colours don't change
    (same_when_inactive), so nothing needs it. Returns the answer, or None to go on as usual."""
    if event.type() in _ACTIVATION:
        return True  # taken here: passed on, the view (and its viewport, sent it next) would repaint
    return None


def apply_theme(app: QApplication, name: str | None = None) -> bool:
    """Apply a theme (default: the saved choice). Returns True when the dark theme is on."""
    global _system_style, _applied
    # Every theme but "system" is the same style sheet in this run's colors (a new theme's colors come in
    # at the next start), so picking another one mustn't restyle the app.
    wanted = "system" if (name or chosen_theme()) == "system" else "slothbloom"
    if wanted == _applied:
        return wanted != "system"
    _applied = wanted
    global looks_applied
    looks_applied += 1  # widgets that remember their measurements measure again (live_stage.Elided)
    if _system_style is None:
        _system_style = app.style().name()
        _set_symbol_fallbacks(app)
        _set_text_size(app)
    if wanted == "system":
        app.setStyleSheet("")
        app.setStyle(guarded_style(_system_style))  # the system look, still with the wheel guard
        app.setPalette(app.style().standardPalette())
        return False
    app.setStyle(roomy_style())
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: CHROME,  # checkboxes, spin boxes and combo boxes
        QPalette.ColorRole.AlternateBase: PANEL,
        QPalette.ColorRole.ToolTipBase: PANEL,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: RAISED,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: LIVE,
        QPalette.ColorRole.Highlight: SELECT_BG,
        QPalette.ColorRole.HighlightedText: SELECT_TEXT,
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.PlaceholderText: MUTED,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(MUTED))
    same_when_inactive(palette)
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET + nature_rules())
    return True


# Nature shapes ---------------------------------------------------------------------------------
# SprigCast's controls grow instead of click: a tick box is a leaf (an outline when off, a filled
# leaf when on), a radio choice is a seed that sprouts, and sliders and switches carry a leaf. The
# style sheet can only show pictures for checkbox, radio and slider parts, so those are small SVGs
# written once to the cache folder; painted widgets use leaf_path()/paint_leaf().

LEAF_OFF = _themed("LEAF_OFF", "#5d6b64")  # an unpicked leaf's outline: reads on PANEL and CARD without shouting

# One leaf in unit space: stem end bottom-left, tip top-right, both edges curved.
_LEAF = ((-0.5, 0.5), (-0.6, -0.2), (0.1, -0.6), (0.55, -0.55), (0.6, 0.05), (0.1, 0.6))
_VEIN_END = (0.3, -0.3)


def _leaf_map(rect: QRectF, mirror: bool):
    sx = rect.width() / 1.2 * (-1 if mirror else 1)
    sy = rect.height() / 1.2
    cx, cy = rect.center().x(), rect.center().y()
    return lambda x, y: QPointF(cx + x * sx, cy + y * sy)


def leaf_path(rect: QRectF, tilt: float = 0.0, mirror: bool = False) -> QPainterPath:
    """A leaf filling `rect`: stem end at the bottom-left corner, tip at the top-right (or the other
    way round with mirror). `tilt` turns it about the rect's centre, in degrees clockwise."""
    at = _leaf_map(rect, mirror)
    (x0, y0), c1, c2, end, c3, c4 = _LEAF
    path = QPainterPath(at(x0, y0))
    path.cubicTo(at(*c1), at(*c2), at(*end))
    path.cubicTo(at(*c3), at(*c4), at(x0, y0))
    if tilt:
        c = rect.center()
        path = QTransform().translate(c.x(), c.y()).rotate(tilt).translate(-c.x(), -c.y()).map(path)
    return path


def paint_leaf(p: QPainter, rect: QRectF, filled: bool, colour: str | QColor = ACCENT,
               vein: str | QColor | None = None, width: float = 1.6, tilt: float = 0.0,
               mirror: bool = False) -> None:
    """Draw a leaf: filled in `colour` with a dark vein, or as an outline with a matching vein."""
    colour = QColor(colour)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    p.setBrush(colour if filled else Qt.BrushStyle.NoBrush)
    p.drawPath(leaf_path(rect, tilt, mirror))
    at = _leaf_map(rect, mirror)
    line = QPainterPath(at(*_LEAF[0]))
    line.lineTo(at(*_VEIN_END))
    if tilt:
        c = rect.center()
        line = QTransform().translate(c.x(), c.y()).rotate(tilt).translate(-c.x(), -c.y()).map(line)
    p.setPen(QPen(QColor(vein or (ACCENT_TEXT if filled else colour)), width * 0.85, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(line)
    p.restore()


ACCENT_DARK = _themed("ACCENT_DARK", "#2a9c5e")  # a deeper leaf green, for a sprout drawn on a light knob


def paint_sprout(p: QPainter, rect: QRectF, colour: str | QColor = ACCENT_DARK) -> None:
    """Two small leaves on a stem, filling `rect` (the same sprout as a picked radio seed)."""
    colour = QColor(colour)
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    at = lambda u, v: QPointF(x + u / 24 * w, y + v / 24 * h)  # noqa: E731 (24 px design grid)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(colour, max(1.0, w / 14), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.drawLine(at(12, 18), at(12, 11))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    right = QPainterPath(at(12, 12))
    right.cubicTo(at(12, 8), at(15, 6), at(18, 6))
    right.cubicTo(at(18, 9.5), at(15.5, 11.5), at(12, 12))
    left = QPainterPath(at(12, 13.5))
    left.cubicTo(at(12, 10.5), at(9.5, 9), at(6.5, 9))
    left.cubicTo(at(6.5, 12), at(9, 13.5), at(12, 13.5))
    p.drawPath(right)
    p.drawPath(left)
    p.restore()


def _seed_handle_svg() -> str:
    """The slider handle: a light seed with a green ring and a sprout, round so nothing clips it."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            f'<circle cx="12" cy="12" r="10" fill="{TEXT_BRIGHT}" stroke="{ACCENT}" stroke-width="2.5"/>'
            f'<path d="M12,18 L12,11" stroke="{ACCENT_DARK}" stroke-width="1.8" stroke-linecap="round"/>'
            f'<path d="M12,12 C12,8 15,6 18,6 C18,9.5 15.5,11.5 12,12 Z" fill="{ACCENT_DARK}"/>'
            f'<path d="M12,13.5 C12,10.5 9.5,9 6.5,9 C6.5,12 9,13.5 12,13.5 Z" fill="{ACCENT_DARK}"/></svg>')


# The tick box: a rounded square cut like a leaf, two opposite corners fully round (top-left, bottom-right)
# and two nearly square. Ticked, it fills green with a dark tick. 24 px design grid.
LEAFCUT_D = ("M2.5,11 Q2.5,2.5 11,2.5 L19.5,2.5 Q21.5,2.5 21.5,4.5 L21.5,13 Q21.5,21.5 13,21.5 "
             "L4.5,21.5 Q2.5,21.5 2.5,19.5 Z")
TICK_D = "M7,12.5 L10.5,16 L17,8.5"


def _leafcut_svg(fill: str, stroke: str, tick: str | None) -> str:
    mark = (f'<path d="{TICK_D}" fill="none" stroke="{tick}" stroke-width="2.4" stroke-linecap="round" '
            'stroke-linejoin="round"/>') if tick else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            f'<path d="{LEAFCUT_D}" fill="{fill}" stroke="{stroke}" stroke-width="1.6" stroke-linejoin="round"/>'
            f'{mark}</svg>')


def leafcut_path(rect: QRectF) -> QPainterPath:
    """The leaf-cut tick box filling `rect` (for painted widgets; the style sheet uses the SVG)."""
    sx, sy = rect.width() / 24, rect.height() / 24
    at = lambda x, y: QPointF(rect.x() + x * sx, rect.y() + y * sy)  # noqa: E731
    path = QPainterPath(at(2.5, 11))
    path.quadTo(at(2.5, 2.5), at(11, 2.5))
    path.lineTo(at(19.5, 2.5))
    path.quadTo(at(21.5, 2.5), at(21.5, 4.5))
    path.lineTo(at(21.5, 13))
    path.quadTo(at(21.5, 21.5), at(13, 21.5))
    path.lineTo(at(4.5, 21.5))
    path.quadTo(at(2.5, 21.5), at(2.5, 19.5))
    path.closeSubpath()
    return path


def paint_tick_box(p: QPainter, rect: QRectF, on: bool, enabled: bool = True) -> None:
    """Draw the leaf-cut tick box, the same as every QCheckBox: green with a dark tick when on."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    width = max(1.2, rect.width() / 15)
    if on:
        colour = QColor(ACCENT if enabled else MUTED)
        p.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(colour)
    else:
        p.setPen(QPen(QColor(LEAF_OFF if enabled else EDGE_STRONG), width, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(QColor(CHROME if enabled else PANEL))
    p.drawPath(leafcut_path(rect))
    if on:
        sx, sy = rect.width() / 24, rect.height() / 24
        at = lambda x, y: QPointF(rect.x() + x * sx, rect.y() + y * sy)  # noqa: E731
        tick = QPainterPath(at(7, 12.5))
        tick.lineTo(at(10.5, 16))
        tick.lineTo(at(17, 8.5))
        p.setPen(QPen(QColor(ACCENT_TEXT if enabled else PANEL), rect.width() / 10, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(tick)
    p.restore()


def _seed_svg(ring: str, fill: str, sprout: str | None) -> str:
    """A round seed; picked, it sprouts two small leaves."""
    leaves = ""
    if sprout:
        leaves = (f'<path d="M12,17 L12,11" stroke="{sprout}" stroke-width="1.6" stroke-linecap="round"/>'
                  f'<path d="M12,12 C12,8 15,6.5 17,6.5 C17,9.5 15,11.5 12,12 Z" fill="{sprout}"/>'
                  f'<path d="M12,13.5 C12,10.5 9.5,9.5 7.5,9.5 C7.5,12 9.5,13.5 12,13.5 Z" fill="{sprout}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            f'<circle cx="12" cy="12" r="10" fill="{fill}" stroke="{ring}" stroke-width="1.8"/>{leaves}</svg>')


NATURE_PICTURES = {
    # Tick boxes: the leaf-cut box (the names stay "leaf" for the style-sheet rules below).
    "leaf_off": lambda: _leafcut_svg(CHROME, LEAF_OFF, None),
    "leaf_hover": lambda: _leafcut_svg(CHROME, ACCENT, None),
    "leaf_on": lambda: _leafcut_svg(ACCENT, ACCENT, ACCENT_TEXT),
    "leaf_off_disabled": lambda: _leafcut_svg(PANEL, EDGE_STRONG, None),
    "leaf_on_disabled": lambda: _leafcut_svg(MUTED, MUTED, PANEL),
    "seed_off": lambda: _seed_svg(LEAF_OFF, CHROME, None),
    "seed_hover": lambda: _seed_svg(ACCENT, CHROME, None),
    "seed_on": lambda: _seed_svg(ACCENT, ACCENT, ACCENT_TEXT),
    "seed_off_disabled": lambda: _seed_svg(EDGE_STRONG, PANEL, None),
    "seed_on_disabled": lambda: _seed_svg(MUTED, MUTED, PANEL),
    "seed_handle": _seed_handle_svg,
    # A section title's mark: a small filled leaf before the title.
    "section_leaf": lambda: ('<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 16 16">'
                             f'<path d="M2 14C2 6 7 2 14 2c0 7-4 12-12 12z" fill="{ACCENT}"/>'
                             f'<path d="M3.5 12.5L10 6" stroke="{ACCENT_TEXT}" stroke-width="1.3"'
                             ' stroke-linecap="round"/></svg>'),
    "chevron": lambda: (f'<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
                        f'<path d="M3,4.5 L6,7.5 L9,4.5" fill="none" stroke="{MUTED}" stroke-width="1.6"'
                        ' stroke-linecap="round" stroke-linejoin="round"/></svg>'),
}


def nature_pictures(folder: str | None = None) -> dict[str, str]:
    """Write the control pictures (once) and return name -> path with forward slashes for the style sheet."""
    import hashlib
    import tempfile
    from pathlib import Path

    base = Path(folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
                or tempfile.gettempdir()) / "theme"
    files = {name: f"{name}-{hashlib.sha1(make().encode()).hexdigest()[:8]}.svg"  # new colours, new file
             for name, make in NATURE_PICTURES.items()}
    key = (str(base), tuple(files.values()))
    if key in _NATURE_WRITTEN:  # written already this run: no disk work (a mkdir on every Last streams open
        return dict(_NATURE_WRITTEN[key])  # held the window 137 ms on the PC while live, 2026-11-18)
    base.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, file in files.items():
        path = base / file
        if not path.exists():
            path.write_text(NATURE_PICTURES[name](), encoding="utf-8")
        out[name] = path.as_posix()
    _NATURE_WRITTEN[key] = dict(out)
    return out


_NATURE_WRITTEN: dict[tuple, dict[str, str]] = {}  # nature_pictures' answers, by folder and picture files


def section_title_rule(pic: dict[str, str], selector: str = "QGroupBox::title") -> str:
    """A titled section's leaf mark, drawn before the title (the room for it is the title's left padding)."""
    return (f"{selector} {{ padding-left: 20px; background-image: url({pic['section_leaf']});"
            " background-repeat: no-repeat; background-position: left center; }")


def combo_arrow_rule(selector: str = "QComboBox") -> str:
    """A drop-down's mark: the drawn chevron at its right end (the stock arrow is a tiny triangle, or nothing
    at all once the button part is styled). "" when the picture can't be written."""
    try:
        pic = nature_pictures()
    except OSError:
        return ""
    return (f"{selector}::down-arrow {{ image: url({pic['chevron']}); width: 12px; height: 12px;"
            " margin-right: 8px; }")


def nature_rules(folder: str | None = None) -> str:
    """Style sheet rules that swap tick boxes, radio buttons and slider handles for leaf-cut boxes and seeds."""
    try:
        pic = nature_pictures(folder)
    except OSError:  # can't write the pictures: keep the plain shapes
        return ""
    rules = []
    clear = "border: none; background: transparent;"  # the plain rules above fill a checked box green
    for widget, kind in (("QCheckBox", "leaf"), ("QRadioButton", "seed"), ("QListWidget", "leaf"),
                         ("QTreeWidget", "leaf")):
        size = 16 if widget == "QCheckBox" else 18 if widget == "QRadioButton" else 15
        rules += [
            f"{widget}::indicator {{ width: {size}px; height: {size}px; border: none; background: transparent;"
            f" border-radius: 0; image: url({pic[kind + '_off']}); }}",
            f"{widget}::indicator:hover {{ {clear} image: url({pic[kind + '_hover']}); }}",
            f"{widget}::indicator:checked {{ {clear} image: url({pic[kind + '_on']}); }}",
            f"{widget}::indicator:disabled {{ {clear} image: url({pic[kind + '_off_disabled']}); }}",
            f"{widget}::indicator:checked:disabled {{ {clear} image: url({pic[kind + '_on_disabled']}); }}",
        ]
    # Sliders: a round seed handle. The slider is made tall enough for it, so it's never cut off.
    rules.append("QSlider:horizontal { min-height: 22px; } QSlider:vertical { min-width: 22px; }")
    rules.append(f"QSlider::handle:horizontal {{ background: transparent; border: none; width: 18px; height: 18px;"
                 f" margin: -7px 0; border-radius: 9px; image: url({pic['seed_handle']}); }}")
    rules.append(f"QSlider::handle:vertical {{ background: transparent; border: none; width: 18px; height: 18px;"
                 f" margin: 0 -7px; border-radius: 9px; image: url({pic['seed_handle']}); }}")
    rules.append(section_title_rule(pic))
    # Tick lists get room to breathe: nothing touches the box's edge, rows aren't packed together.
    rules.append("QListWidget, QTreeWidget { padding: 6px; }")
    rules.append("QListWidget::item, QTreeWidget::item { padding: 5px 8px; min-height: 22px; }")
    rules.append("QListWidget::indicator, QTreeWidget::indicator { margin-right: 6px; }")
    return "\n".join(rules) + "\n"


# A pop-up card's soft shadow: one small picture, made once, whose corners are copied and whose edges are stretched
# to any card's size. A QGraphicsDropShadowEffect on the card drew the whole card into a picture and blurred it on
# every repaint of anything in it (a Garden check's numbers each second, a busy bar, hover): part of every pop-up's
# open on the PC while live (2026-11-18). A picture per card size still blurred a whole card the first time each
# size was shown, and a card that fits its contents changes size as it fills (Garden check and Stream details,
# about 55 ms each on the PC, 2026-11-19).
CARD_SHADOW = (36, 10, 190)  # blur, drop and darkness, as the effect had them
CARD_RADIUS = 24
SHADOW_CORNER = CARD_RADIUS + CARD_SHADOW[0]  # this far in from a card's corner its shadow is the same all along
_SHADOW_CARD = 2 * SHADOW_CORNER + 8  # the small card the picture is made from
_shadow_pictures: dict[tuple, tuple] = {}


def _shadow_picture(ratio: float, colour: str) -> tuple:
    """(picture, left, top, right, bottom): the shadow of a small rounded card, and how far it reaches past each of
    the card's sides."""
    from PySide6.QtGui import QBrush, QImage, QPixmap
    from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsPathItem, QGraphicsScene

    key = (ratio, colour)
    found = _shadow_pictures.get(key)
    if found is not None:
        return found
    blur, drop, dark = CARD_SHADOW
    side = _SHADOW_CARD
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, side, side), CARD_RADIUS, CARD_RADIUS)
    scene = QGraphicsScene()
    # No index: a scene with one keeps a 2-second timer going after an item is added, for a scene that lives a
    # few milliseconds. That timer's tick, handed to the scene's freed index, was the start-up check's rare
    # access violation (2026-11-19/20; the scene had been freed on another thread, see pacing.thread_stacks).
    scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.NoIndex)
    item = QGraphicsPathItem(path)
    item.setPen(QPen(Qt.PenStyle.NoPen))
    item.setBrush(QBrush(QColor(colour)))
    effect = QGraphicsDropShadowEffect()
    effect.setBlurRadius(blur)
    effect.setOffset(0, drop)
    effect.setColor(QColor(0, 0, 0, dark))
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    area = effect.boundingRectFor(QRectF(0, 0, side, side)).toAlignedRect()
    image = QImage(round(area.width() * ratio), round(area.height() * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, image.width(), image.height()), QRectF(area))
    painter.end()
    picture = QPixmap.fromImage(image)
    if len(_shadow_pictures) > 4:  # a screen scale and a theme or two: the oldest go
        _shadow_pictures.pop(next(iter(_shadow_pictures)))
    found = _shadow_pictures[key] = (picture, -area.left(), -area.top(), area.right() + 1 - side,
                                     area.bottom() + 1 - side)
    return found


def _no_shadows() -> bool:
    """No card shadows on a PC with little memory (core/pacing.py low_memory): each size is a picture kept in
    memory, and on a 4 GB PC pop-ups painting theirs were in 12.6 s and 7.5 s waits for the disk (Mikedog151,
    2026-11-25). The cards keep their edge."""
    from sprigcast.core import pacing

    return pacing.low_memory()


def warm_card_shadow(window) -> None:
    """Once, just after start: make the small shadow picture. The first pop-up with a card made it as it opened, and
    making it is Qt's graphics scene and blur for the first time in the run (their types and enums made on the spot):
    69 ms and a late frame on the first Garden check while live (PC 2026-11-19). The same one picture every pop-up
    uses, so nothing extra is kept."""
    try:
        if not _no_shadows():
            _shadow_picture(window.devicePixelRatioF(), PANEL)
    except Exception:  # noqa: BLE001 - a warm-up only: the first pop-up makes it then
        pass


def shadow_pieces(w: int, h: int, left: int, top: int, right: int, bottom: int) -> list[tuple]:
    """Nine (target, source) rectangles (x, y, w, h each; the target from the card's top left, the source in the
    small picture, both in the card's own units): corners copied as they are, edges and middle stretched."""
    corner = min(SHADOW_CORNER, w // 2, h // 2)
    side = _SHADOW_CARD
    xs = ((-left, left + corner, 0, left + corner),  # (target x, width, source x, source width)
          (corner, w - 2 * corner, left + corner, side - 2 * corner),
          (w - corner, corner + right, left + side - corner, corner + right))
    ys = ((-top, top + corner, 0, top + corner),
          (corner, h - 2 * corner, top + corner, side - 2 * corner),
          (h - corner, corner + bottom, top + side - corner, corner + bottom))
    return [((tx, ty, tw, th), (sx, sy, sw, sh)) for ty, th, sy, sh in ys for tx, tw, sx, sw in xs if tw > 0 and th > 0]


_shadows_put_together: dict[tuple, tuple] = {}
SHADOWS_KEPT = 6


def card_shadow(w: int, h: int, ratio: float, colour: str) -> tuple:
    """(picture, left, top): a w x h card's whole shadow, put together from the small picture's nine pieces (no
    blurring) the first time a size is asked for, and kept for the last few sizes. Painting the nine pieces each
    time was a dozen calls into Qt per paint, each handing Python's lock over and waiting for it back while live;
    this way a paint is one."""
    key = (w, h, ratio, colour)
    found = _shadows_put_together.get(key)
    if found is not None:
        return found
    from PySide6.QtGui import QPixmap

    picture, left, top, right, bottom = _shadow_picture(ratio, colour)
    whole = QPixmap(round((w + left + right) * ratio), round((h + top + bottom) * ratio))
    whole.fill(Qt.GlobalColor.transparent)
    painter = QPainter(whole)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    for (tx, ty, tw, th), (sx, sy, sw, sh) in shadow_pieces(w, h, left, top, right, bottom):
        painter.drawPixmap(QRectF((left + tx) * ratio, (top + ty) * ratio, tw * ratio, th * ratio), picture,
                           QRectF(sx * ratio, sy * ratio, sw * ratio, sh * ratio))
    painter.end()
    whole.setDevicePixelRatio(ratio)
    if len(_shadows_put_together) >= SHADOWS_KEPT:  # pop-ups that grow: the oldest go
        _shadows_put_together.pop(next(iter(_shadows_put_together)))
    found = _shadows_put_together[key] = (whole, -left, -top)
    return found


def paint_card_shadow(window, card) -> None:
    """From a pop-up's paintEvent: the soft shadow under its card (see CARD_SHADOW)."""
    rect = card.geometry().getRect()
    x, y, w, h = rect
    if w <= 0 or h <= 0 or _no_shadows():
        return
    picture, left, top = card_shadow(w, h, window.devicePixelRatioF(), PANEL)
    painter = QPainter(window)
    painter.drawPixmap(x + left, y + top, picture)
    painter.end()
