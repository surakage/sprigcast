"""The Slothbloom look: a dark moss-and-bark palette with a blossom-pink accent.

Uses Qt's Fusion style so it looks the same on every Windows version. Set the environment
variable SPRIGCAST_THEME=system to keep the plain system look instead.
"""

from __future__ import annotations

import os

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

BARK = "#1e1f1c"  # window background
MOSS = "#2a2d27"  # panels and inputs
LICHEN = "#3a3f36"  # borders, hover
LEAF = "#9cc89a"  # secondary accent (moss green)
BLOOM = "#e8a4b8"  # main accent (blossom pink)
INPUT = "#363a32"  # checkbox and number-box fill, a step lighter so they stand out
PETAL = "#ece8e1"  # text
DUSK = "#8d8a83"  # muted text

STYLESHEET = f"""
QToolTip {{ color: {PETAL}; background: {MOSS}; border: 1px solid {LICHEN}; }}
QDockWidget::title {{ background: {MOSS}; padding: 4px 6px; }}
QPushButton {{
    background: {MOSS}; border: 1px solid {LICHEN}; border-radius: 4px; padding: 4px 2px;
}}
QPushButton:hover {{ border-color: {LEAF}; }}
QPushButton:pressed {{ background: {LICHEN}; }}
QPushButton:checked {{ background: {BLOOM}; color: {BARK}; }}
QPushButton:disabled {{ color: {DUSK}; }}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 4px; }}
QLineEdit, QPlainTextEdit {{ background: {BARK}; border: 1px solid {LICHEN}; border-radius: 3px; padding: 2px 4px; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {BLOOM}; }}
QListWidget, QTreeWidget {{ background: {BARK}; border: 1px solid {LICHEN}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{ background: {BLOOM}; color: {BARK}; }}
QMenuBar {{ background: {BARK}; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {BLOOM}; color: {BARK}; }}
QMenu {{ background: {MOSS}; border: 1px solid {LICHEN}; }}
QSlider::groove:horizontal {{ height: 4px; background: {LICHEN}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {BLOOM}; width: 12px; margin: -5px 0; border-radius: 6px; }}
QSlider::sub-page:horizontal {{ background: {LEAF}; border-radius: 2px; }}
QStatusBar {{ color: {DUSK}; }}
QSplitter::handle {{ background: {LICHEN}; }}
"""


def apply_theme(app: QApplication) -> bool:
    """Apply the dark theme unless the user opted out. Returns True when applied."""
    if os.environ.get("SPRIGCAST_THEME", "").lower() == "system":
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
    return True
