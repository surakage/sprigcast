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
                 on_change: Callable[[float], None] | None = None, label_width: int = 0, centred: bool = False):
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
        if label_width:
            self.label.setFixedWidth(label_width)  # every value in a card starts at the same place
        row.addWidget(self.label)
        if not label:
            self.label.hide()
        self.box = _NumberBox()
        self.box.frame = self
        self.box.setRange(low, high)
        self.box.setMinimumWidth(40)  # the range's longest number made every pill wide enough for "-20000"
        self.box.setDecimals(decimals)
        # The number right after its label (it sat at the far end, looking pushed off to the side).
        self.box.setAlignment((Qt.AlignmentFlag.AlignHCenter if centred else Qt.AlignmentFlag.AlignLeft)
                              | Qt.AlignmentFlag.AlignVCenter)
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
    """The inspector's tabs as one rounded segmented row (none when there is one page), then the open tab's
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
            box.setSpacing(12)
            self.pages[name] = page
            self.stack.addWidget(page)
        col.addWidget(bar)
        bar.setVisible(len(names) > 1)  # one page needs no tabs
        col.addLayout(self.stack)
        saved = str(QSettings().value(self.KEY, names[0]) or names[0])
        self.show_tab(saved if saved in names else names[0], remember=False)

    def group(self, tab: str, title: str | None) -> QWidget:
        """A titled rounded card of rows on `tab`."""
        holder = QFrame()
        holder.setObjectName("inspGroup")
        holder.setStyleSheet(f"#inspGroup {{ background: {theme.SECTION}; border: 1px solid {theme.EDGE_STRONG};"
                             " border-radius: 18px; }")
        box = QVBoxLayout(holder)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
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


class UsedIn(QWidget):
    """The other blooms a sprig is in, without a wall of links.

    chips: "Also in" over up to SHOWN chips, then a "+N more" chip with the rest in a menu.
    menu: one quiet pill, "Also in 5 blooms ▾", with every bloom in its menu.
    Clicking a bloom calls on_pick(bloom id)."""

    SHOWN = 2

    def __init__(self, on_pick: Callable[[str], None], style: str = "chips", word: str = "bloom"):
        super().__init__()
        self.on_pick, self.style, self.word = on_pick, style, word
        self.setObjectName("usedIn")
        self.setStyleSheet("#usedIn { background: transparent; }")
        self.col = QVBoxLayout(self)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(6)
        self.blooms: list[tuple[str, str]] = []
        self._text = ""

    def text(self) -> str:
        """What it says, links as href="id" (for tests and the log)."""
        return self._text

    def _clear(self) -> None:
        while self.col.count():
            item = self.col.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
            elif item.layout() is not None:
                inner = item.layout()
                while inner.count():
                    w = inner.takeAt(0).widget()
                    if w is not None:
                        w.hide()
                        w.deleteLater()

    def set_blooms(self, blooms: list[tuple[str, str]]) -> None:
        self.blooms = list(blooms)
        self._clear()
        if not blooms:
            self._text = f"Only in this {self.word} so far."
            note = QLabel(self._text)
            note.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
            self.col.addWidget(note)
            return
        self._text = "Also in " + ", ".join(f'<a href="{i}">{n}</a>' for i, n in blooms)
        if self.style == "menu":
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            many = f"{len(blooms)} {self.word}s" if len(blooms) > 1 else blooms[0][1]
            button = self._chip(f"Also in {many}  ▾", quiet=True)
            button.clicked.connect(lambda: self._menu(button, self.blooms))
            row.addWidget(button)
            row.addStretch(1)
            self.col.addLayout(row)
            return
        title = QLabel("Also in")
        title.setStyleSheet(f"color: {theme.MUTED}; font-size: 12px; font-weight: 600; background: transparent;")
        self.col.addWidget(title)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        shown = blooms if len(blooms) <= self.SHOWN + 1 else blooms[:self.SHOWN]
        for bloom_id, name in shown:
            chip = self._chip(name)
            chip.setToolTip(f"Edit {name}")
            chip.clicked.connect(lambda _=False, b=bloom_id: self.on_pick(b))
            row.addWidget(chip, 0)
        rest = blooms[len(shown):]
        if rest:
            more = self._chip(f"+{len(rest)} more", quiet=True)
            more.clicked.connect(lambda: self._menu(more, rest))
            row.addWidget(more, 0)
        row.addStretch(1)
        self.col.addLayout(row)

    def _chip(self, text: str, quiet: bool = False) -> QPushButton:
        from PySide6.QtGui import QFontMetrics

        chip = QPushButton()
        colour = theme.MUTED if quiet else theme.ACCENT
        chip.setStyleSheet(theme.button_style(13, "4px 11px") + f"QPushButton {{ color: {colour}; font-size: 12px;"
                           " font-weight: 600; }")
        chip.setCursor(Qt.CursorShape.PointingHandCursor)
        metrics = QFontMetrics(chip.font())
        chip.setText(metrics.elidedText(text, Qt.TextElideMode.ElideRight, 78) if not quiet else text)
        return chip

    def _menu(self, under: QPushButton, blooms: list[tuple[str, str]]) -> None:
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        for bloom_id, name in blooms:
            menu.addAction(name).triggered.connect(lambda _=False, b=bloom_id: self.on_pick(b))
        theme.ui_burst()
        menu.exec(under.mapToGlobal(QPoint(0, under.height() + 4)))
        menu.deleteLater()
