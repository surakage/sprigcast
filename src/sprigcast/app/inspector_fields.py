"""Build inspector pieces: number pills with their label inside, and a foldable rounded card.

    ╭─ Place ───────────────── ▾ ╮
    │ ( X   970 ) ( Y   560 )    │
    │ ( W   900 ) ⛓ ( H   640 )  │
    ╰────────────────────────────╯
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QObject, QSettings, Qt
from PySide6.QtWidgets import QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import icons, theme

FIELD_H = 36


class _Focus(QObject):
    """Lights the pill's edge while its number has the keyboard."""

    def __init__(self, frame: QFrame):
        super().__init__(frame)
        self.frame = frame

    def eventFilter(self, _obj, event) -> bool:  # noqa: N802
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.frame.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.frame.style().unpolish(self.frame)
            self.frame.style().polish(self.frame)
        return False


class NumberField(QFrame):
    """A rounded pill: a muted label on the left, the number after it, an optional unit at the end."""

    def __init__(self, label: str, low: float, high: float, decimals: int = 0, suffix: str = "",
                 on_change: Callable[[float], None] | None = None):
        super().__init__()
        self.setObjectName("numField")
        self.setFixedHeight(FIELD_H)
        self.setStyleSheet(
            f"#numField {{ background: {theme.CARD}; border: 1px solid {theme.EDGE}; border-radius: 18px; }}"
            f"#numField:hover {{ border-color: {theme.EDGE_STRONG}; }}"
            f"#numField[focused=\"true\"] {{ border-color: {theme.ACCENT}; }}"
            "QLabel { background: transparent; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 12, 0)
        row.setSpacing(8)
        self.label = QLabel(label)
        self.label.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600; font-size: 12px;")
        row.addWidget(self.label)
        self.box = QDoubleSpinBox()
        self.box.setRange(low, high)
        self.box.setDecimals(decimals)
        self.box.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if suffix:
            self.box.setSuffix(suffix)
        self.box.setStyleSheet(
            f"QDoubleSpinBox {{ background: transparent; border: none; padding: 0; color: {theme.TEXT_BRIGHT};"
            " font-weight: 600; }"
            "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {"
            " subcontrol-origin: margin; width: 0px; height: 0px; border: none; }")
        self.box.installEventFilter(_Focus(self))
        row.addWidget(self.box, 1)
        if on_change is not None:
            self.box.valueChanged.connect(on_change)
        self.setCursor(Qt.CursorShape.IBeamCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.box.setFocus()
        self.box.selectAll()
        super().mousePressEvent(event)


def link_button(on: bool, on_toggle: Callable[[bool], None]) -> QPushButton:
    """The round chain between Width and Height: lit, they keep their shape together."""
    button = QPushButton()
    button.setCheckable(True)
    button.setChecked(on)
    button.setFixedSize(28, 28)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(
        f"QPushButton {{ background: transparent; border: none; border-radius: 14px; }}"
        f"QPushButton:hover {{ background: {theme.RAISED}; }}"
        f"QPushButton:checked {{ background: {theme.SELECT_BG}; }}")

    def paint(checked: bool) -> None:
        button.setIcon(icons.icon("link", theme.ACCENT if checked else theme.MUTED, 16))
        button.setToolTip("Keep shape" if checked else "Free shape")

    button.toggled.connect(paint)
    button.toggled.connect(on_toggle)
    paint(on)
    return button


class Card(QFrame):
    """A foldable rounded card: click its title row to open or close it (remembered)."""

    def __init__(self, title: str, open_by_default: bool = False):
        super().__init__()
        self.setObjectName("inspCard")
        self.setStyleSheet(f"#inspCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE};"
                           " border-radius: 20px; }")
        self.key = f"inspector/open/{title}"
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 8, 14, 14)
        outer.setSpacing(10)
        self.header = QPushButton(title)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.TEXT}; font-weight: 600;"
            f" text-align: left; padding: 4px 0; }} QPushButton:hover {{ color: {theme.TEXT_BRIGHT}; }}")
        self.chevron = QLabel()
        self.chevron.setStyleSheet("background: transparent;")
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.header, 1)
        top.addWidget(self.chevron)
        outer.addLayout(top)
        self.body = QWidget()
        self.body.setObjectName("inspCardBody")
        self.body.setStyleSheet("#inspCardBody { background: transparent; }")
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(10)
        outer.addWidget(self.body)
        self.header.clicked.connect(self.toggle)
        self.show_open(QSettings().value(self.key, open_by_default, type=bool))

    def show_open(self, on: bool) -> None:
        self.body.setVisible(on)
        self.chevron.setText("▾" if on else "▸")
        self.chevron.setStyleSheet(f"background: transparent; color: {theme.MUTED};")

    def toggle(self) -> None:
        on = not self.body.isVisible()
        QSettings().setValue(self.key, on)
        self.show_open(on)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.position().y() < 40:  # anywhere on the title row
            self.toggle()
        super().mousePressEvent(event)
