"""The Slothbloom look, "Night Canopy": calm near-neutral greens, one green accent, pink only for Live.

Uses Qt's Fusion style so it looks the same on every Windows version. Settings → General picks
the theme (saved in QSettings "general/theme"); the environment variable SPRIGCAST_THEME=system
overrides it.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPropertyAnimation, QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog

# Night Canopy: near-neutral deep greens, hairline edges, one green accent for actions and
# selection, and pink kept for the things that matter on stream: Live and Recording.
BG = "#0e1311"  # window background
CHROME = "#0b0f0d"  # menu bar, toolbars, status bar and wells (meters, text boxes)
PANEL = "#151c19"  # docks, lists, cards and pop-ups
RAISED = "#1c2420"  # buttons
RAISED_HOVER = "#232d28"
PRESSED = "#18201c"
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
WARN = "#e6c55a"
BAD = "#f2665e"
INFO = "#7fb8ff"

# Older names, kept so plugins and older code keep working.
BARK, MOSS, LICHEN, LEAF, BLOOM, INPUT, PETAL, DUSK = BG, PANEL, EDGE_STRONG, ACCENT, LIVE, CHROME, TEXT, MUTED

ON_AIR_STYLE = (
    f"background: {LIVE}; color: #ffffff; font-weight: bold; border: 1px solid {LIVE_EDGE};"
    f" border-top-color: {LIVE_HOVER}; border-radius: 8px;"
)
GO_LIVE_STYLE = (  # Go live waits in calm green; once on air the button turns LIVE pink (ON_AIR_STYLE)
    f"QPushButton {{ background: {ACCENT}; color: {ACCENT_TEXT}; font-weight: bold; border: 1px solid {ACCENT};"
    f" border-radius: 8px; }}"
    f"QPushButton:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}"
    f"QPushButton:disabled {{ background: {PANEL}; color: {MUTED}; border-color: {EDGE}; }}"
)
PRIMARY_STYLE = (  # the one big "do it" button on a panel, such as Go
    f"QPushButton {{ font-weight: bold; padding: 6px 28px; }}"
    f"QPushButton:enabled {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: 1px solid {ACCENT}; }}"
    f"QPushButton:enabled:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}"
)
NOTE_STYLE = f"background: {SELECT_BG}; color: {SELECT_TEXT}; border-radius: 8px;"  # inline notes and banners
MUTED_STYLE = f"color: {MUTED}"

# Live screen (see live_stage.py): pill buttons, no borders.
TRACK = "#1a221e"  # the empty part of thin level bars
STAGE_EMPTY = "#1d3428"  # a bloom tile before it has a picture
LIVE_TEXT = "#ff8fb4"  # pink text on dark, for the on-air clock
PILL_STYLE = (
    f"QPushButton {{ background: {PANEL}; color: {TEXT}; border: none; border-radius: 19px; padding: 10px 14px;"
    f" font-weight: 600; }}"
    f"QPushButton:hover {{ background: {RAISED}; }}"
    f"QPushButton:pressed {{ background: {PRESSED}; }}"
    f"QPushButton:disabled {{ color: {MUTED}; }}"
)
SMALL_PILL_STYLE = (  # chips and small tools; Qt draws square corners if the radius is over half the height
    f"QPushButton, QToolButton {{ background: {PANEL}; color: {TEXT}; border: none; border-radius: 13px;"
    f" padding: 6px 13px; font-weight: 600; }}"
    f"QPushButton:hover, QToolButton:hover {{ background: {RAISED}; }}"
    f"QPushButton:disabled {{ color: {MUTED}; }}"
    "QPushButton::menu-indicator, QToolButton::menu-indicator { width: 0; image: none; }"
)
PILL_SELECTED_STYLE = PILL_STYLE + f"QPushButton {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}"
PILL_ON_AIR_STYLE = PILL_STYLE + f"QPushButton {{ background: {LIVE}; color: #ffffff; }}" \
    f"QPushButton:hover {{ background: {LIVE_HOVER}; }}"
BIG_GO_STYLE = (
    f"QPushButton {{ background: {ACCENT}; color: {ACCENT_TEXT}; border: none; border-radius: 24px; padding: 14px;"
    f" font-size: 16px; font-weight: 700; }}"
    f"QPushButton:hover {{ background: {ACCENT_HOVER}; }}"
    f"QPushButton:disabled {{ background: {PANEL}; color: {MUTED}; }}"
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
    "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 14px; border: none; background: transparent; }"
    "QDoubleSpinBox { padding: 6px 0 6px 14px; }"  # balances the (invisible) arrows so numbers sit centred
)
LINK_STYLE = (
    f"QPushButton {{ background: transparent; border: none; color: {MUTED}; padding: 2px 4px; }}"
    f"QPushButton:hover {{ color: {ACCENT}; }}"
)

STYLESHEET = f"""
QWidget {{ selection-background-color: {SELECT_BG}; selection-color: {SELECT_TEXT}; }}
QMainWindow::separator {{ background: {BG}; width: 6px; height: 6px; }}
QToolTip {{ color: {TEXT}; background: {PANEL}; border: 1px solid {EDGE_STRONG}; border-radius: 6px; padding: 4px 6px; }}
QDockWidget {{ color: {MUTED}; }}
QDockWidget::title {{ background: {CHROME}; padding: 6px 8px; border-bottom: 1px solid {EDGE}; }}
QToolBar {{ background: {CHROME}; border: none; border-bottom: 1px solid {EDGE}; spacing: 4px; padding: 2px; }}
QPushButton {{
    background: {RAISED}; color: {TEXT};
    border: 1px solid {EDGE_STRONG}; border-top-color: {HIGHLIGHT}; border-bottom-color: {CHROME};
    border-radius: 8px; padding: 6px 14px; min-height: 18px; text-align: center;
}}
QPushButton:hover {{ background: {RAISED_HOVER}; }}
QPushButton:pressed {{ background: {PRESSED}; border-top-color: {CHROME}; padding-top: 7px; padding-bottom: 5px; }}
QPushButton:checked {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}
QPushButton:default {{ background: {ACCENT}; color: {ACCENT_TEXT}; border-color: {ACCENT}; font-weight: 600; }}
QPushButton:default:hover {{ background: {ACCENT_HOVER}; border-color: {ACCENT_HOVER}; }}
QPushButton:disabled {{ color: {MUTED}; background: {PANEL}; border-color: {EDGE}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 3px 6px; color: {TEXT}; }}
QToolButton:hover {{ background: {RAISED_HOVER}; border-color: {EDGE_STRONG}; }}
QToolButton[autoRaise="false"] {{
    background: {RAISED}; border: 1px solid {EDGE_STRONG}; border-top-color: {HIGHLIGHT}; padding: 3px 10px;
}}
QToolButton:pressed {{ background: {PRESSED}; }}
QToolButton:checked {{ background: {SELECT_BG}; color: {SELECT_TEXT}; border-color: {ACCENT}; }}
QDialog, QMessageBox {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; }}
QGroupBox {{
    background: {BG}; border: 1px solid {EDGE}; border-radius: 10px; margin-top: 16px; padding: 12px 10px 10px 10px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; font-weight: 600; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {CHROME}; color: {TEXT}; border: 1px solid {EDGE_STRONG}; border-radius: 6px; padding: 3px 6px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox QAbstractItemView {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; outline: none; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px; border: 1px solid {EDGE_STRONG}; background: {CHROME};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
QListWidget, QTreeWidget, QListView, QTreeView, QTableView {{
    background: {PANEL}; border: 1px solid {EDGE}; border-radius: 8px; outline: none;
}}
QListWidget::item, QTreeWidget::item {{ padding: 3px 4px; border-radius: 5px; }}
QListWidget::item:hover, QTreeWidget::item:hover {{ background: {RAISED}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}
QListWidget::indicator {{ width: 12px; height: 12px; border: 1px solid {EDGE_STRONG}; border-radius: 3px; background: {CHROME}; }}
QListWidget::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QHeaderView::section {{ background: {CHROME}; color: {MUTED}; border: none; border-bottom: 1px solid {EDGE}; padding: 4px; }}
QTabWidget::pane {{ border: 1px solid {EDGE}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 6px 14px; border: none; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT_BRIGHT}; border-bottom: 2px solid {ACCENT}; }}
QMenuBar {{ background: {CHROME}; color: {MUTED}; border-bottom: 1px solid {EDGE}; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 5px; }}
QMenuBar::item:selected {{ background: {RAISED}; color: {TEXT}; }}
QMenu {{ background: {PANEL}; border: 1px solid {EDGE_STRONG}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 5px 20px 5px 14px; border-radius: 5px; color: {TEXT}; }}
QMenu::item:selected {{ background: {SELECT_BG}; color: {SELECT_TEXT}; }}
QMenu::item:disabled {{ color: {MUTED}; }}
QMenu::separator {{ height: 1px; background: {EDGE}; margin: 4px 8px; }}
QSlider::groove:horizontal {{ height: 4px; background: {EDGE_STRONG}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {TEXT_BRIGHT}; border: 2px solid {ACCENT}; width: 10px; margin: -6px 0; border-radius: 7px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QProgressBar {{ background: {CHROME}; border: 1px solid {EDGE}; border-radius: 4px; text-align: center; color: {TEXT}; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background: {EDGE_STRONG}; border-radius: 3px; }}
QScrollBar::handle:vertical {{ min-height: 30px; }}
QScrollBar::handle:horizontal {{ min-width: 30px; }}
QScrollBar::handle:hover {{ background: {MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ background: {CHROME}; color: {MUTED}; border-top: 1px solid {EDGE}; }}
QSplitter::handle {{ background: {BG}; }}
"""


class PopIn(QObject):
    """Fades dialogs in as they open (150 ms), so a pop-up reads as arriving on top of the window."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.enabled = True
        self._running: dict[int, QPropertyAnimation] = {}

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt naming)
        if self.enabled and event.type() == QEvent.Type.Show and isinstance(obj, QDialog) and obj.isWindow():
            anim = QPropertyAnimation(obj, b"windowOpacity", obj)
            anim.setDuration(150)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            key = id(obj)
            self._running[key] = anim
            anim.finished.connect(lambda: self._running.pop(key, None))
            anim.start()
        return False


_pop_in: PopIn | None = None

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


def apply_theme(app: QApplication, name: str | None = None) -> bool:
    """Apply a theme (default: the saved choice). Returns True when the dark theme is on."""
    global _system_style, _pop_in
    if _system_style is None:
        _system_style = app.style().name()
        _set_symbol_fallbacks(app)
    if (name or chosen_theme()) == "system":
        app.setStyleSheet("")
        if _pop_in is not None:
            _pop_in.enabled = False
        app.setStyle(_system_style)
        app.setPalette(app.style().standardPalette())
        return False
    app.setStyle("Fusion")
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
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    if _pop_in is None:
        _pop_in = PopIn(app)
        app.installEventFilter(_pop_in)
    _pop_in.enabled = True
    return True
