"""The Slothbloom look: a rainforest palette. Deep canopy greens, a vivid emerald and an orchid-pink accent.

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

BARK = "#0c1d14"  # window background: the forest floor in shade
MOSS = "#13301f"  # panels and inputs
LICHEN = "#1f5236"  # borders, hover
LEAF = "#3ddc84"  # secondary accent: sunlit emerald leaf
BLOOM = "#ff5fa2"  # main accent: orchid pink
INPUT = "#16402a"  # checkbox and number-box fill, a step lighter so they stand out
PETAL = "#f0fff5"  # text
DUSK = "#8cc4a3"  # muted text
# Light from above: raised surfaces are lighter at the top, with a bright rim and a dark lower edge.
RAISED_TOP, RAISED_BOTTOM = "#1f5a3a", "#10291b"
RIM, SHADE = "#2c7a4f", "#04100a"
BLOOM_TOP, BLOOM_BOTTOM, BLOOM_EDGE = "#ff8fc4", "#f23f8f", "#c02670"
POP_TOP, POP_BOTTOM = "#173d28", "#0f2a1b"  # dialogs and menus sit a step above the window

STYLESHEET = f"""
QToolTip {{ color: {PETAL}; background: {MOSS}; border: 1px solid {LICHEN}; }}
QDockWidget::title {{ background: {MOSS}; padding: 4px 6px; }}
QPushButton {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {RAISED_TOP}, stop:1 {RAISED_BOTTOM});
    border: 1px solid {LICHEN}; border-top-color: {RIM}; border-bottom-color: {SHADE};
    border-radius: 8px; padding: 6px 14px; min-height: 18px; text-align: center;
}}
QPushButton:hover {{ border-color: {LEAF}; }}
QPushButton:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {RAISED_BOTTOM}, stop:1 {RAISED_TOP});
    border-top-color: {SHADE}; border-bottom-color: {RIM}; padding-top: 7px; padding-bottom: 5px;
}}
QPushButton:checked {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {BLOOM_TOP}, stop:1 {BLOOM_BOTTOM});
    color: {BARK}; border-color: {BLOOM_EDGE}; border-top-color: {BLOOM_TOP};
}}
QPushButton:default {{ border-color: {BLOOM}; }}
QPushButton:disabled {{ color: {DUSK}; background: {MOSS}; border-color: {LICHEN}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 6px; }}
QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 3px 6px; }}
QToolButton:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {RAISED_TOP}, stop:1 {RAISED_BOTTOM});
    border-color: {LICHEN}; border-top-color: {RIM};
}}
QToolButton[autoRaise="false"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {RAISED_TOP}, stop:1 {RAISED_BOTTOM});
    border: 1px solid {LICHEN}; border-top-color: {RIM}; border-bottom-color: {SHADE}; padding: 3px 10px;
}}
QToolButton:pressed {{ background: {LICHEN}; }}
QToolButton:checked {{ background: {BLOOM}; color: {BARK}; }}
QDialog, QMessageBox {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {POP_TOP}, stop:1 {POP_BOTTOM});
    border: 1px solid {LICHEN}; border-top: 3px solid {BLOOM};
}}
QGroupBox {{
    background: rgba(255, 255, 255, 8); border: 1px solid {LICHEN}; border-top-color: {RIM};
    border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px;
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {BLOOM}; }}
QLineEdit, QPlainTextEdit {{ background: {BARK}; border: 1px solid {LICHEN}; border-radius: 3px; padding: 2px 4px; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {BLOOM}; }}
QListWidget, QTreeWidget {{ background: {BARK}; border: 1px solid {LICHEN}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {BLOOM}; color: {BARK}; }}
QListWidget::indicator {{ width: 12px; height: 12px; border: 1px solid {DUSK}; border-radius: 3px; background: {INPUT}; }}
QListWidget::indicator:checked {{ background: {LEAF}; border-color: {BARK}; }}
QMenuBar {{ background: {BARK}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {BLOOM}; color: {BARK}; }}
QMenu {{ background: {POP_TOP}; border: 1px solid {LICHEN}; border-top: 2px solid {BLOOM}; padding: 4px; }}
QMenu::item {{ padding: 5px 20px 5px 14px; border-radius: 5px; }}
QSlider::groove:horizontal {{ height: 4px; background: {LICHEN}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {BLOOM}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: {LEAF}; border-radius: 2px; }}
QStatusBar {{ color: {DUSK}; }}
QSplitter::handle {{ background: {LICHEN}; }}
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
        QPalette.ColorRole.Window: BARK,
        QPalette.ColorRole.WindowText: PETAL,
        QPalette.ColorRole.Base: INPUT,  # checkboxes, spin boxes and combo boxes
        QPalette.ColorRole.AlternateBase: MOSS,
        QPalette.ColorRole.ToolTipBase: MOSS,
        QPalette.ColorRole.ToolTipText: PETAL,
        QPalette.ColorRole.Text: PETAL,
        QPalette.ColorRole.Button: MOSS,
        QPalette.ColorRole.ButtonText: PETAL,
        QPalette.ColorRole.BrightText: BLOOM,
        QPalette.ColorRole.Highlight: BLOOM,
        QPalette.ColorRole.HighlightedText: BARK,
        QPalette.ColorRole.Link: LEAF,
        QPalette.ColorRole.PlaceholderText: DUSK,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(DUSK))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    if _pop_in is None:
        _pop_in = PopIn(app)
        app.installEventFilter(_pop_in)
    _pop_in.enabled = True
    return True
