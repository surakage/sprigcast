"""The Slothbloom look, "Night Canopy": calm near-neutral greens, one green accent, pink only for Live.

Uses Qt's Fusion style so it looks the same on every Windows version. Settings → General picks
the theme (saved in QSettings "general/theme"); the environment variable SPRIGCAST_THEME=system
overrides it.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QPointF, QRectF, QSettings, QStandardPaths, Qt
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
    # No arrow buttons (type, scroll or use the arrow keys), so the number is centred on the whole box.
    # setButtonSymbols(NoButtons) can't be used: with the app stylesheet it shrinks the text field to 1 px.
    "QDoubleSpinBox { padding: 6px 0px; }"
    "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {"
    " subcontrol-origin: margin; width: 0px; height: 0px; border: none; }"
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
QCheckBox, QRadioButton {{ spacing: 8px; }}
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


def apply_theme(app: QApplication, name: str | None = None) -> bool:
    """Apply a theme (default: the saved choice). Returns True when the dark theme is on."""
    global _system_style, _applied
    wanted = name or chosen_theme()
    if wanted == _applied:
        return wanted != "system"
    _applied = wanted
    if _system_style is None:
        _system_style = app.style().name()
        _set_symbol_fallbacks(app)
    if wanted == "system":
        app.setStyleSheet("")
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


def _leaf_svg(fill: str, stroke: str, vein: str) -> str:
    def pt(x, y):  # unit leaf -> a 24 px box with a 1 px margin
        return f"{12 + x * 18.3:.2f},{12 + y * 18.3:.2f}"

    (x0, y0), c1, c2, end, c3, c4 = _LEAF
    d = f"M{pt(x0, y0)} C{pt(*c1)} {pt(*c2)} {pt(*end)} C{pt(*c3)} {pt(*c4)} {pt(x0, y0)} Z"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="1.8" stroke-linejoin="round"/>'
            f'<path d="M{pt(x0, y0)} L{pt(*_VEIN_END)}" stroke="{vein}" stroke-width="1.5" '
            f'stroke-linecap="round" fill="none"/></svg>')


def _seed_svg(ring: str, fill: str, sprout: str | None) -> str:
    """A round seed; picked, it sprouts two small leaves."""
    leaves = ""
    if sprout:
        leaves = (f'<path d="M12,17 L12,11" stroke="{sprout}" stroke-width="1.6" stroke-linecap="round"/>'
                  f'<path d="M12,12 C12,8 15,6.5 17,6.5 C17,9.5 15,11.5 12,12 Z" fill="{sprout}"/>'
                  f'<path d="M12,13.5 C12,10.5 9.5,9.5 7.5,9.5 C7.5,12 9.5,13.5 12,13.5 Z" fill="{sprout}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">'
            f'<circle cx="12" cy="12" r="10" fill="{fill}" stroke="{ring}" stroke-width="1.8"/>{leaves}</svg>')


def _vine_svg() -> str:
    """One stretch of vine for progress bars: a gently wavy stem with a leaf up and a leaf down. Tiles sideways."""
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="14" viewBox="0 0 40 14">'
            f'<path d="M0,7 C7,5 13,5 20,7 C27,9 33,9 40,7" stroke="{ACCENT_DARK}" stroke-width="2" fill="none"/>'
            f'<path d="M9,6 C9.5,2.5 13,0.8 17,1 C16,4.2 13,6 9,6 Z" fill="{ACCENT}"/>'
            f'<path d="M29,8 C28.5,11.5 25,13.2 21,13 C22,9.8 25,8 29,8 Z" fill="{ACCENT}"/></svg>')


NATURE_PICTURES = {
    "leaf_off": lambda: _leaf_svg(CHROME, LEAF_OFF, LEAF_OFF),
    "leaf_hover": lambda: _leaf_svg(CHROME, ACCENT, ACCENT),
    "leaf_on": lambda: _leaf_svg(ACCENT, ACCENT, ACCENT_TEXT),
    "leaf_off_disabled": lambda: _leaf_svg(PANEL, EDGE_STRONG, EDGE_STRONG),
    "leaf_on_disabled": lambda: _leaf_svg(MUTED, MUTED, PANEL),
    "seed_off": lambda: _seed_svg(LEAF_OFF, CHROME, None),
    "seed_hover": lambda: _seed_svg(ACCENT, CHROME, None),
    "seed_on": lambda: _seed_svg(ACCENT, ACCENT, ACCENT_TEXT),
    "seed_off_disabled": lambda: _seed_svg(EDGE_STRONG, PANEL, None),
    "seed_on_disabled": lambda: _seed_svg(MUTED, MUTED, PANEL),
    "seed_handle": _seed_handle_svg,
    "vine": _vine_svg,
}


def nature_pictures(folder: str | None = None) -> dict[str, str]:
    """Write the control pictures (once) and return name -> path with forward slashes for the style sheet."""
    import hashlib
    import tempfile
    from pathlib import Path

    base = Path(folder or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)
                or tempfile.gettempdir()) / "theme"
    base.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, make in NATURE_PICTURES.items():
        svg = make()
        path = base / f"{name}-{hashlib.sha1(svg.encode()).hexdigest()[:8]}.svg"  # new colours, new file
        if not path.exists():
            path.write_text(svg, encoding="utf-8")
        out[name] = path.as_posix()
    return out


def nature_rules(folder: str | None = None) -> str:
    """Style sheet rules that swap tick boxes, radio buttons and slider handles for leaves and seeds."""
    try:
        pic = nature_pictures(folder)
    except OSError:  # can't write the pictures: keep the plain shapes
        return ""
    rules = []
    clear = "border: none; background: transparent;"  # the plain rules above fill a checked box green
    for widget, kind in (("QCheckBox", "leaf"), ("QRadioButton", "seed"), ("QListWidget", "leaf"),
                         ("QTreeWidget", "leaf")):
        size = 18 if widget in ("QCheckBox", "QRadioButton") else 16
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
    # Progress grows as a vine along a soft track.
    rules.append(f"QProgressBar {{ background: {RAISED}; border: none; border-radius: 7px; min-height: 14px;"
                 f" max-height: 14px; color: transparent; }}")
    rules.append(f"QProgressBar::chunk {{ background-color: transparent; background-image: url({pic['vine']});"
                 f" background-repeat: repeat-x; background-position: left center; border-radius: 7px; }}")
    # Tick lists get room to breathe: nothing touches the box's edge, rows aren't packed together.
    rules.append("QListWidget, QTreeWidget { padding: 6px; }")
    rules.append("QListWidget::item, QTreeWidget::item { padding: 5px 8px; min-height: 22px; }")
    rules.append("QListWidget::indicator, QTreeWidget::indicator { margin-right: 6px; }")
    return "\n".join(rules) + "\n"
