"""The New sprig pop-up: a name, a shape and whether to drop it into the bloom you're on, in one card."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QHBoxLayout, QLabel, QWidget

from sprigcast.app.modal import modal
from sprigcast.app import theme
from sprigcast.app.card import CardDialog, field_label
from sprigcast.app.labels import literal
from sprigcast.app.terms import PART
from sprigcast.core.scene import Scene, SceneCollection

EXAMPLES = "Webcam frame, Alerts, Brand bar"


class ShapeTile(QAbstractButton):
    """A pickable tile: the canvas drawn to shape, its name and size."""

    def __init__(self, name: str, width: int, height: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.name, self.w, self.h = name, width, height
        self.setFixedHeight(64)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(180, 64)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        hover = self.underMouse()
        p.setPen(QColor(theme.ACCENT if on else (theme.BUTTON_EDGE_HOVER if hover else theme.BUTTON_EDGE)))
        p.setBrush(QColor(theme.SELECT_BG if on else (theme.BUTTON_HOVER if hover else theme.BUTTON)))
        p.drawRoundedRect(r, 16, 16)
        box_h = 34.0
        box_w = box_h * self.w / self.h
        if box_w > 44:
            box_w, box_h = 44.0, 44.0 * self.h / self.w
        glyph = QRectF(r.x() + 14 + (44 - box_w) / 2, r.center().y() - box_h / 2, box_w, box_h)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.ACCENT if on else theme.LEAF_OFF))
        p.drawRoundedRect(glyph, 4, 4)
        x = r.x() + 14 + 44 + 12
        font = QFont(self.font())
        font.setWeight(QFont.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(theme.SELECT_TEXT if on else theme.TEXT))
        p.drawText(QRectF(x, r.y() + 12, r.right() - x, 20), Qt.AlignmentFlag.AlignLeft, self.name)
        small = QFont(self.font())
        small.setPixelSize(11)
        p.setFont(small)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(x, r.y() + 33, r.right() - x, 18), Qt.AlignmentFlag.AlignLeft, f"{self.w}×{self.h}")
        p.end()

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.update()
        super().leaveEvent(event)


class NewPartDialog(CardDialog):
    def __init__(self, collection: SceneCollection, show: Scene | None, parent: QWidget | None = None):
        super().__init__(f"New {PART}", parent, width=460)
        self.name = self.line_edit(EXAMPLES)
        self.body.addWidget(field_label("Name"))
        self.body.addWidget(self.name)
        self.body.addWidget(field_label("Shape"))
        shapes = QHBoxLayout()
        shapes.setSpacing(10)
        self.shape_group = QButtonGroup(self)
        self.shape_group.setExclusive(True)
        default = show.canvas if show is not None else collection.canvases[0].id
        for canvas in collection.canvases:
            tile = ShapeTile(canvas.id.capitalize(), canvas.width, canvas.height)
            tile.setProperty("canvas", canvas.id)
            tile.setChecked(canvas.id == default)
            self.shape_group.addButton(tile)
            shapes.addWidget(tile, 1)
        self.body.addLayout(shapes)
        from sprigcast.app.live_stage import Switch

        self.add_to_show = Switch()
        self.add_to_show.setChecked(show is not None)
        self.add_label = QLabel(f"Add to “{literal(show.name)}”" if show is not None else "")
        self.add_label.setStyleSheet(f"color: {theme.TEXT}; background: transparent;")
        self.add_row = QWidget()
        row = QHBoxLayout(self.add_row)
        row.setContentsMargins(2, 2, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.add_to_show)
        row.addWidget(self.add_label)
        row.addStretch(1)
        self.add_row.setVisible(show is not None)
        self.body.addWidget(self.add_row)
        self.ok = self.add_buttons(f"Create {PART}")
        self.name.textChanged.connect(lambda text: self.ok.setEnabled(bool(text.strip())))
        self.ok.setEnabled(False)
        self.name.setFocus()

    def values(self) -> tuple[str, str, bool]:
        """(name, canvas id, add to the show)."""
        checked = self.shape_group.checkedButton()
        return (self.name.text().strip(), str(checked.property("canvas")),
                self.add_row.isVisibleTo(self) and self.add_to_show.isChecked())

    @staticmethod
    def ask(collection: SceneCollection, show: Scene | None, parent: QWidget | None = None) -> tuple[str, str, bool] | None:
        dialog = NewPartDialog(collection, show, parent)
        if not modal(dialog):
            return None
        name, canvas_id, add = dialog.values()
        return (name, canvas_id, add) if name else None
