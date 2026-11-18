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
WARN = "#e6c55a"
BAD = "#f2665e"
INFO = "#7fb8ff"

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
TRACK = "#1a221e"  # the empty part of thin level bars
STAGE_EMPTY = "#1d3428"  # a bloom tile before it has a picture
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
    background: {BG}; border: 1px solid {EDGE}; border-radius: 16px; margin-top: 16px; padding: 12px 10px 10px 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; font-weight: 600; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {CHROME}; color: {TEXT}; border: 1px solid {EDGE_STRONG}; border-radius: 10px; padding: 3px 8px;
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

THEMES = {"slothbloom": "Slothbloom (dark)", "system": "System (Windows default)"}
_system_style: str | None = None


def chosen_theme() -> str:
    if os.environ.get("SPRIGCAST_THEME", "").lower() == "system":
        return "system"
    name = str(QSettings().value("general/theme", "slothbloom"))
    return name if name in THEMES else "slothbloom"


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
    wanted = name or chosen_theme()
    if wanted == _applied:
        return wanted != "system"
    _applied = wanted
    global looks_applied
    looks_applied += 1  # widgets that remember their measurements measure again (live_stage.Elided)
    if _system_style is None:
        _system_style = app.style().name()
        _set_symbol_fallbacks(app)
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

LEAF_OFF = "#5d6b64"  # an unpicked leaf's outline: reads on PANEL and CARD without shouting

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


ACCENT_DARK = "#2a9c5e"  # a deeper leaf green, for a sprout drawn on a light knob


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
    # Tick lists get room to breathe: nothing touches the box's edge, rows aren't packed together.
    rules.append("QListWidget, QTreeWidget { padding: 6px; }")
    rules.append("QListWidget::item, QTreeWidget::item { padding: 5px 8px; min-height: 22px; }")
    rules.append("QListWidget::indicator, QTreeWidget::indicator { margin-right: 6px; }")
    return "\n".join(rules) + "\n"


# A pop-up card's soft shadow, drawn once per size and then copied. A QGraphicsDropShadowEffect on the card drew the
# whole card into a picture and blurred it on every repaint of anything in it (a Garden check's numbers each second,
# a busy bar, hover): part of every pop-up's open on the PC while live (2026-11-18).
CARD_SHADOW = (36, 10, 190)  # blur, drop and darkness, as the effect had them
CARD_RADIUS = 24
_shadow_pictures: dict[tuple, tuple] = {}


def _shadow_picture(w: int, h: int, ratio: float, colour: str) -> tuple:
    """(picture, left, top): the shadow of a w x h rounded card, and where its top left sits from the card's."""
    from PySide6.QtGui import QBrush, QImage, QPixmap
    from PySide6.QtWidgets import QGraphicsDropShadowEffect, QGraphicsPathItem, QGraphicsScene

    key = (w, h, ratio, colour)
    found = _shadow_pictures.get(key)
    if found is not None:
        return found
    blur, drop, dark = CARD_SHADOW
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), CARD_RADIUS, CARD_RADIUS)
    scene = QGraphicsScene()
    item = QGraphicsPathItem(path)
    item.setPen(QPen(Qt.PenStyle.NoPen))
    item.setBrush(QBrush(QColor(colour)))
    effect = QGraphicsDropShadowEffect()
    effect.setBlurRadius(blur)
    effect.setOffset(0, drop)
    effect.setColor(QColor(0, 0, 0, dark))
    item.setGraphicsEffect(effect)
    scene.addItem(item)
    area = effect.boundingRectFor(QRectF(0, 0, w, h)).toAlignedRect()
    image = QImage(round(area.width() * ratio), round(area.height() * ratio), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    scene.render(painter, QRectF(0, 0, image.width(), image.height()), QRectF(area))
    painter.end()
    picture = QPixmap.fromImage(image)
    picture.setDevicePixelRatio(ratio)
    if len(_shadow_pictures) > 8:  # a few sizes at most (pop-ups that grow): the oldest go
        _shadow_pictures.pop(next(iter(_shadow_pictures)))
    found = _shadow_pictures[key] = (picture, area.left(), area.top())
    return found


def paint_card_shadow(window, card) -> None:
    """From a pop-up's paintEvent: the soft shadow under its card (see CARD_SHADOW)."""
    rect = card.geometry()
    if rect.width() <= 0 or rect.height() <= 0:
        return
    picture, left, top = _shadow_picture(rect.width(), rect.height(), window.devicePixelRatioF(), PANEL)
    painter = QPainter(window)
    painter.drawPixmap(rect.left() + left, rect.top() + top, picture)
    painter.end()
