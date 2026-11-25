"""A rounded card pop-up, the shape every small SprigCast dialog shares (no square window frame).

    ╭──────────────────────────────╮
    │ Title                        │
    │ ...body...                   │
    │            Cancel  [ Do it ] │
    ╰──────────────────────────────╯

Drag it by any empty spot. Esc cancels, Enter presses the main button.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme

FIELD_STYLE = (
    f"QLineEdit {{ background: {theme.CARD}; border: 1px solid {theme.EDGE_STRONG}; border-radius: 17px;"
    f" padding: 7px 14px; color: {theme.TEXT}; }}"
    f"QLineEdit:focus {{ border-color: {theme.ACCENT}; }}"
)
PRIMARY_PILL = theme.PRIMARY_STYLE + "QPushButton { border-radius: 17px; padding: 9px 22px; }" \
    f"QPushButton:disabled {{ background: transparent; color: {theme.MUTED}; border: 1px solid {theme.BUTTON_OFF_EDGE}; }}"


SECTION_NAME = "sectionCard"
SECTION_STYLE = (f"#{SECTION_NAME} {{ background: {theme.SECTION}; border: 1px solid {theme.SECTION_EDGE};"
                 " border-radius: 18px; }")


def section(title: str = "", spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    """A titled section card, the way every area groups its parts (user 2026-11-25): its own rounded card with a
    visible edge on the panel, so sections never blend into one flat surface. Returns (the card, its layout)."""
    frame = QFrame()
    frame.setObjectName(SECTION_NAME)
    frame.setStyleSheet(SECTION_STYLE)
    box = QVBoxLayout(frame)
    box.setContentsMargins(18, 14, 18, 16)
    box.setSpacing(spacing)
    if title:
        box.addWidget(section_title(title))
    return frame, box


def section_title(text: str) -> QLabel:
    heading = QLabel(text)
    heading.setObjectName("sectionTitle")
    heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 650; font-size: {theme.SECTION_TITLE_PX}px;"
                          " background: transparent;")
    return heading


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600; background: transparent;")
    return label


def screen_room(widget: QWidget):
    """The free part (no taskbar) of the screen a window is on, or None."""
    from PySide6.QtGui import QGuiApplication

    screen = QWidget.screen(widget) if widget is not None else None  # (a pop-up may call a field "screen")
    screen = screen or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else None


def keep_on_screen(window: QWidget) -> None:
    """No bigger than the free part of its screen, and wholly on it: a pop-up centered on a window at a screen's
    edge, or taller than a small screen, ran past it (a 1600x900 all-in-one, 2026-11-23)."""
    room = screen_room(window)
    if room is None or room.isEmpty():
        return
    width, height = QWidget.width(window), QWidget.height(window)  # (fields may be called "size" or "width")
    # A window with Windows' own frame (Settings) is its title bar and edges bigger than its inside: the inside
    # is fitted to what's left, or the bottom, Save among it, ran under the taskbar on a 1280x720 screen at 150%.
    outer = QWidget.frameGeometry(window)
    extra_w, extra_h = max(0, outer.width() - width), max(0, outer.height() - height)
    room_w, room_h = room.width() - extra_w, room.height() - extra_h
    if width > room_w or height > room_h:
        small = QWidget.minimumSize(window)
        QWidget.setMinimumSize(window, min(small.width(), room_w), min(small.height(), room_h))
        QWidget.resize(window, min(width, room_w), min(height, room_h))
    frame = QWidget.frameGeometry(window)
    x = min(max(frame.x(), room.left()), room.left() + room.width() - frame.width())
    y = min(max(frame.y(), room.top()), room.top() + room.height() - frame.height())
    if (x, y) != (frame.x(), frame.y()):
        QWidget.move(window, max(x, room.left()), max(y, room.top()))


def fit_width(scroll: QScrollArea) -> None:
    """Nothing in a pop-up's body asks to be wider than the card: a drop-down sized to its longest item (a window
    title) made Window capture's body 715 px in a 434 px card, and its fields, switches and Save were cut off at
    the right (PC 2026-11-25). Such a drop-down cuts long items short with … instead, as combo() does."""
    inner = scroll.widget()
    if inner is None:
        return
    for box in inner.findChildren(QComboBox):
        if box.sizeAdjustPolicy() == QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon:
            continue
        policy = box.sizePolicy()
        if policy.horizontalPolicy() == QSizePolicy.Policy.Fixed or box.minimumWidth() > 0:
            continue  # a size set on purpose stays
        box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        box.setMinimumContentsLength(min(box.minimumContentsLength() or 12, 12))
        # Qt keeps the minimum it worked out from the longest item even after the policy changes: a set minimum
        # (what the new policy asks for) is what the layout goes by instead.
        box.setMinimumWidth(min(box.minimumSizeHint().width(), box.sizeHint().width()))
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        box.setSizePolicy(policy)
        box.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)


class FitScroll(QScrollArea):
    """A pop-up's body: as tall as what's in it, up to what its screen has room for, then it scrolls. The pop-ups
    are as tall as their content, and Text from a file needed 926 px: more than a 900 px screen has free."""

    def __init__(self, chrome: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.chrome = chrome  # the window's height around this scroll area (margins, shadow)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: transparent; }")
        self.viewport().setStyleSheet("background: transparent")

    def setWidget(self, widget: QWidget) -> None:  # noqa: N802
        super().setWidget(widget)
        widget.installEventFilter(self)  # its layout changing (rows shown or hidden) resizes the pop-up

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.LayoutRequest and watched is self.widget():
            self.updateGeometry()
        return super().eventFilter(watched, event)

    def _room(self) -> int:
        room = screen_room(self.window())
        return room.height() - self.chrome if room is not None and not room.isEmpty() else 100_000

    def sizeHint(self) -> QSize:  # noqa: N802
        inner = self.widget()
        if inner is None:
            return super().sizeHint()
        hint = inner.sizeHint()
        wide = self.width() if self.isVisible() else 0
        card = self.parentWidget()
        if wide <= 0 and card is not None and card.layout() is not None:
            margins = card.layout().contentsMargins()
            wide = card.minimumWidth() - margins.left() - margins.right() - 2  # (the card's edge)
        height = hint.height()
        if wide > 0 and inner.hasHeightForWidth():
            height = inner.heightForWidth(wide)
        return QSize(hint.width(), max(60, min(height, self._room())))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()


class CardDialog(QDialog):
    def __init__(self, title: str, parent: QWidget | None = None, width: int = 440):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 26)  # room for the shadow
        outer.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)  # always just fits what's showing
        card = QFrame()
        card.setFixedWidth(width - 40)
        card.setObjectName("popCard")
        card.setStyleSheet(f"#popCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 24px; }")
        # Its shadow is a picture made once per size and drawn under it (paintEvent), as the other pop-ups' is. A
        # drop-shadow effect blurred the whole card in software on its first paint and again on every repaint
        # inside it, the text field's blinking caret among them (Gardens > New, 61 ms on the PC, 2026-11-19).
        self._card = card
        outer.addWidget(card)
        holder = QVBoxLayout(card)
        holder.setContentsMargins(0, 14, 4, 14)  # the scroll bar, when there is one, inside the round corners
        self._scroll = FitScroll(14 + 26 + 14 + 14 + 2)
        holder.addWidget(self._scroll)
        inner = QWidget()
        inner.setObjectName("popCardBody")
        inner.setStyleSheet("#popCardBody { background: transparent; }")
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(24, 6, 20, 6)  # with the holder's: 24 / 20 / 24 / 20 as before
        self._scroll.setWidget(inner)
        self.body.setSpacing(14)
        heading = self.heading = QLabel(title)
        heading.setWordWrap(True)
        font = heading.font()
        font.setPixelSize(19)
        font.setWeight(font.Weight.DemiBold)
        heading.setFont(font)
        heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
        self.body.addWidget(heading)

    def paintEvent(self, event) -> None:  # noqa: N802
        theme.paint_card_shadow(self, self._card)

    def showEvent(self, event) -> None:  # noqa: N802
        fit_width(self._scroll)
        super().showEvent(event)
        keep_on_screen(self)

    def section(self, title: str = "", spacing: int = 12) -> QVBoxLayout:
        """A titled section card added to the body; returns its layout to fill."""
        frame, box = section(title, spacing)
        self.body.addWidget(frame)
        return box

    def line_edit(self, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setStyleSheet(FIELD_STYLE)
        edit.setMinimumHeight(34)
        return edit

    def add_buttons(self, primary: str, cancel: str = "Cancel") -> QPushButton:
        """Cancel and the main button, bottom right. Returns the main button."""
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        no = QPushButton(cancel)
        no.setStyleSheet(theme.button_style(17, "9px 18px"))
        no.clicked.connect(self.reject)
        yes = QPushButton(primary)
        yes.setStyleSheet(PRIMARY_PILL)
        yes.setDefault(True)
        yes.clicked.connect(self.accept)
        for button in (no, yes):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            row.addWidget(button)
        self.body.addSpacing(4)
        self.body.addLayout(row)
        return yes

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)


BOX_HEIGHT = 36  # a field or drop-down on a card: the pill radius (17) stays under half of it


def combo(min_chars: int = 12) -> QComboBox:
    """A rounded drop-down that fits the card: a long item is cut short with … instead of widening the card
    (Window capture's drop-down ran past the pop-up's right edge and cut off its green focus ring)."""
    from sprigcast.app.starter import combo_style

    box = QComboBox()
    box.setStyleSheet(combo_style())
    box.setFixedHeight(BOX_HEIGHT)
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(min_chars)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    box.setCursor(Qt.CursorShape.PointingHandCursor)
    box.setMaxVisibleItems(14)
    box.view().setTextElideMode(Qt.TextElideMode.ElideMiddle)  # keeps the app name and the end of the title
    return box


class ChoiceDialog(CardDialog):
    """Pick one from a list: a heading, a label, one drop-down, Cancel and OK. choice() is the picked index."""

    def __init__(self, title: str, label: str, items: list, current: int = 0, parent: QWidget | None = None,
                 primary: str = "OK", width: int = 460):
        super().__init__(title, parent, width)
        self.body.addWidget(field_label(label))
        self.box = combo()
        room = width - 40 - 48 - 76  # the card, its margins, the box's padding and chevron
        fit = self.box.fontMetrics()
        for item in items:  # a name, or (icon, name)
            icon, name = item if isinstance(item, tuple) else (None, str(item))
            short = fit.elidedText(name, Qt.TextElideMode.ElideMiddle, room - (24 if icon else 0))
            if icon is None:
                self.box.addItem(short)
            else:
                self.box.addItem(icon, short)
            if short != name:  # the whole name on hover
                self.box.setItemData(self.box.count() - 1, name, Qt.ItemDataRole.ToolTipRole)
        if current < 0:  # nothing picked to start with (removing one: never offer one for you)
            self.box.setPlaceholderText("Pick one")
            self.box.setCurrentIndex(-1)
        else:
            self.box.setCurrentIndex(max(0, min(current, self.box.count() - 1)))
        self.body.addWidget(self.box)
        self.ok = self.add_buttons(primary)
        self.ok.setEnabled(self.box.currentIndex() >= 0)
        self.box.currentIndexChanged.connect(lambda i: self.ok.setEnabled(i >= 0))
        self.box.setFocus()

    def choice(self) -> int:
        return self.box.currentIndex()


class TextDialog(CardDialog):
    """Type a name: a heading, a label, one text field, Cancel and OK. OK waits for some text."""

    def __init__(self, title: str, label: str, text: str = "", parent: QWidget | None = None, primary: str = "OK",
                 width: int = 420, placeholder: str = ""):
        super().__init__(title, parent, width)
        self.body.addWidget(field_label(label))
        self.edit = self.line_edit(placeholder)
        self.edit.setMinimumHeight(BOX_HEIGHT)
        self.edit.setText(text)
        self.edit.selectAll()
        self.body.addWidget(self.edit)
        self.ok = self.add_buttons(primary)
        self.edit.textChanged.connect(lambda t: self.ok.setEnabled(bool(t.strip())))
        self.ok.setEnabled(bool(text.strip()))
        self.edit.setFocus()

    def text(self) -> str:
        return self.edit.text()


def ask_choice(parent: QWidget | None, title: str, label: str, items: list, current: int = 0,
               primary: str = "OK") -> int | None:
    """The picked index, or None on Cancel. current -1: nothing picked until you pick."""
    from sprigcast.app.modal import modal

    dialog = ChoiceDialog(title, label, items, current, parent, primary)
    return dialog.choice() if modal(dialog) else None


def ask_text(parent: QWidget | None, title: str, label: str, text: str = "", primary: str = "OK",
             placeholder: str = "") -> tuple[str, bool]:
    """(text, ok), the same shape as Qt's getText."""
    from sprigcast.app.modal import modal

    dialog = TextDialog(title, label, text, parent, primary, placeholder=placeholder)
    return (dialog.text(), True) if modal(dialog) else ("", False)


class CardWindow(QDialog):
    """A big window (Hotkeys, Plugin store, Filters…) as a rounded card: no square Windows frame. Lay its content
    out in self.card. Drag it by any empty spot, resize it by the bottom right corner; Esc closes it.

    name: the card's object name, for a style sheet that styles the window by name (it gets BG and the rounded
    edge whatever that sheet says). size: the card's starting size.
    """

    MARGINS = (20, 14, 20, 26)  # room for the shadow

    def __init__(self, title: str, parent: QWidget | None = None, size: tuple[int, int] | None = None,
                 name: str = "windowCard"):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(*self.MARGINS)
        self.card = QFrame()
        self.card.setObjectName(name)
        self.card.setStyleSheet(f"#{name} {{ background: {theme.BG}; border: 1px solid {theme.EDGE_STRONG};"
                                " border-radius: 24px; }")
        outer.addWidget(self.card)
        self._grip = QSizeGrip(self)
        self._grip.setFixedSize(18, 18)
        self._grip.setStyleSheet("background: transparent;")
        if size is not None:
            left, top, right, bottom = self.MARGINS
            self.resize(size[0] + left + right, size[1] + top + bottom)

    def paintEvent(self, event) -> None:  # noqa: N802
        theme.paint_card_shadow(self, self.card)  # drawn once per size, not blurred on every paint

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        keep_on_screen(self)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        _, _, right, bottom = self.MARGINS
        self._grip.move(self.width() - right - 24, self.height() - bottom - 24)
        self._grip.raise_()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)


class ColourRow(QWidget):
    """A row of round colour swatches; the current one has a ring. Clicking one picks it."""

    SIZE, GAP, PAD = 20, 10, 12

    def __init__(self, colours: list[str], current: str | None, on_pick, parent: QWidget | None = None):
        super().__init__(parent)
        self.colours, self.current, self.on_pick = list(colours), current, on_pick
        self.hover: int | None = None
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(self.PAD * 2 + len(self.colours) * (self.SIZE + self.GAP) - self.GAP, self.SIZE + 16)

    def _rect(self, i: int):
        from PySide6.QtCore import QRectF

        return QRectF(self.PAD + i * (self.SIZE + self.GAP), 8, self.SIZE, self.SIZE)

    def _at(self, pos) -> int | None:
        return next((i for i in range(len(self.colours)) if self._rect(i).adjusted(-4, -4, 4, 4).contains(pos)), None)

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtGui import QPainter, QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for i, colour in enumerate(self.colours):
            r = self._rect(i)
            if colour == self.current or i == self.hover:
                p.setPen(QPen(QColor(theme.TEXT_BRIGHT if colour == self.current else theme.MUTED), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(r.adjusted(-3, -3, 3, 3))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(colour))
            p.drawEllipse(r)
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        at = self._at(event.position())
        if at != self.hover:
            self.hover = at
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self.hover = None
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        at = self._at(event.position())
        if at is None:
            return
        self.current = self.colours[at]
        self.update()
        self.on_pick(self.current)
        from PySide6.QtWidgets import QMenu

        if isinstance(self.parent(), QMenu):
            self.parent().close()


def colour_row(menu, current: str | None, on_pick):
    """The folder colours as one row of swatches inside a menu."""
    from PySide6.QtWidgets import QWidgetAction

    from sprigcast.core.scene import FOLDER_COLOURS

    action = QWidgetAction(menu)
    action.setDefaultWidget(ColourRow(list(FOLDER_COLOURS), current, on_pick, menu))
    return action
