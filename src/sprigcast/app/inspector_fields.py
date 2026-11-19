"""Build inspector pieces: number pills with their label inside, and a foldable rounded card.

    ╭─ Place ───────────────── ▾ ╮
    │ ( X   970 ) ( Y   560 )    │
    │ ( W   900 ) ⛓ ( H   640 )  │
    ╰────────────────────────────╯
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app import icons, theme

FIELD_H = 36


class _NumberBox(QDoubleSpinBox):
    """The pill's number: lights the pill's edge while it has the keyboard. Its own focus handlers, not an event
    filter: a filter ran Python for every event of every field (the Garden check's open among them, sweep 13)."""

    frame: QFrame | None = None

    def _light(self, on: bool) -> None:
        frame = self.frame
        if frame is not None:
            frame.setProperty("focused", on)
            frame.style().unpolish(frame)
            frame.style().polish(frame)

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self._light(True)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self._light(False)


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
        self.box = _NumberBox()
        self.box.frame = self
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
        theme.button_style(13, "0"))

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


class InspectorTabs(QWidget):
    """The inspector's tabs (Place, Look, Shapes, Device) as one rounded segmented row, then the open tab's
    groups. The open tab is remembered."""

    KEY = "inspector/tab"

    def __init__(self, names: tuple[str, ...]):
        super().__init__()
        self.names = names
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 4, 0, 0)
        col.setSpacing(16)
        bar = QFrame()
        bar.setObjectName("inspTabs")
        bar.setStyleSheet(
            f"#inspTabs {{ background: {theme.CARD}; border-radius: 18px; }}"
            f"#inspTabs QPushButton {{ background: transparent; border: none; border-radius: 14px; padding: 7px 0;"
            f" color: {theme.MUTED}; font-weight: 600; }}"
            f"#inspTabs QPushButton:hover {{ color: {theme.TEXT}; }}"
            f"#inspTabs QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}")
        row = QHBoxLayout(bar)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(2)
        self.buttons: dict[str, QPushButton] = {}
        self.pages: dict[str, QWidget] = {}
        self.stack = QVBoxLayout()
        self.stack.setContentsMargins(0, 0, 0, 0)
        for name in names:
            button = QPushButton(name)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, n=name: self.show_tab(n))
            row.addWidget(button, 1)
            self.buttons[name] = button
            page = QWidget()
            page.setObjectName("inspPage")
            page.setStyleSheet("#inspPage { background: transparent; }")
            box = QVBoxLayout(page)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(18)
            self.pages[name] = page
            self.stack.addWidget(page)
        col.addWidget(bar)
        col.addLayout(self.stack)
        saved = str(QSettings().value(self.KEY, names[0]) or names[0])
        self.show_tab(saved if saved in names else names[0], remember=False)

    def group(self, tab: str, title: str | None) -> QWidget:
        """A group of rows on `tab`, under a small quiet title (none for the tab's first group)."""
        holder = QWidget()
        holder.setObjectName("inspGroup")
        holder.setStyleSheet("#inspGroup { background: transparent; }")
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)
        if title:
            label = QLabel(title.upper())
            label.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px;"
                                " background: transparent;")
            box.addWidget(label)
        self.pages[tab].layout().addWidget(holder)
        return holder

    def show_tab(self, name: str, remember: bool = True) -> None:
        for key, button in self.buttons.items():
            button.setChecked(key == name)
            self.pages[key].setVisible(key == name)
        if remember:
            QSettings().setValue(self.KEY, name)

    def set_tab_visible(self, name: str, on: bool) -> None:
        self.buttons[name].setVisible(on)
        if not on and self.buttons[name].isChecked():
            self.show_tab(self.names[0], remember=False)
