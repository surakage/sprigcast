"""A rounded card pop-up, the shape every small SprigCast dialog shares (no square window frame).

    ╭──────────────────────────────╮
    │ Title                        │
    │ ...body...                   │
    │            Cancel  [ Do it ] │
    ╰──────────────────────────────╯

Drag it by any empty spot. Esc cancels, Enter presses the main button.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
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


def field_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600; background: transparent;")
    return label


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
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        self.body = QVBoxLayout(card)
        self.body.setContentsMargins(24, 20, 24, 20)
        self.body.setSpacing(14)
        heading = QLabel(title)
        font = heading.font()
        font.setPixelSize(19)
        font.setWeight(font.Weight.DemiBold)
        heading.setFont(font)
        heading.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
        self.body.addWidget(heading)

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
