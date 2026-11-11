"""A widget that shows one canvas and lets you edit the active scene's items with the mouse.

- Click an item to select it; drag to move it (snapping options are in Settings → General).
- Drag a corner handle to scale; hold Alt while dragging a handle to crop instead.
- Arrow keys nudge the selected item by 1 px, or 10 px with Shift.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from sprigcast.core import edit
from sprigcast.core.scene import Scene, SceneItem

HANDLE_PX = 8  # handle size on screen
SNAP_PX = 10  # default snap distance on screen


def snap_settings() -> dict:
    """Snapping options from Settings → General (QSettings "editing/..."), with OBS-like defaults."""
    from PySide6.QtCore import QSettings

    q = QSettings()
    return {
        "enabled": q.value("editing/snap", True, type=bool),
        "distance": q.value("editing/snap_distance", float(SNAP_PX), type=float),
        "edges": q.value("editing/snap_edges", True, type=bool),
        "sources": q.value("editing/snap_sources", True, type=bool),
        "center": q.value("editing/snap_center", True, type=bool),
    }
ACCENT = QColor("#e8a4b8")


class Preview(QWidget):
    # canvas id, item index (-1 = nothing selected)
    selected = Signal(str, int)
    # canvas id, finished: False while dragging, True when the edit is done (save then)
    edited = Signal(str, bool)

    def __init__(self, canvas_id: str, title: str, aspect: tuple[int, int], parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas_id = canvas_id
        self.title = title
        self.subtitle = ""
        self.aspect = aspect
        self.scene: Scene | None = None
        self.sizes: dict[str, tuple[int, int]] = {}
        self.selected_index: int | None = None
        self._image: QImage | None = None
        self._drag: tuple[str, int | None, QPointF, SceneItem] | None = None  # mode, corner, start, item copy
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # Data from the main window -------------------------------------------------

    def set_frame(self, data: bytes, width: int, height: int) -> None:
        # copy() detaches the image from the bytes object, which the engine may drop at any time.
        self._image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
        self.aspect = (width, height)
        self.update()

    def set_scene(self, scene: Scene | None, selected_index: int | None) -> None:
        self.scene = scene
        self.selected_index = selected_index
        self.update()

    # Coordinates -------------------------------------------------------------------

    def canvas_rect(self) -> QRectF:
        """Where the canvas is drawn inside the widget, leaving room for the title line."""
        top = 24.0
        avail_w, avail_h = float(self.width()), float(self.height()) - top
        aw, ah = self.aspect
        scale = min(avail_w / aw, avail_h / ah)
        w, h = aw * scale, ah * scale
        return QRectF((avail_w - w) / 2, top + (avail_h - h) / 2, w, h)

    def _scale(self) -> float:
        return self.canvas_rect().width() / self.aspect[0]

    def to_canvas(self, p: QPointF) -> tuple[float, float]:
        r, s = self.canvas_rect(), self._scale()
        return (p.x() - r.x()) / s, (p.y() - r.y()) / s

    def to_widget(self, x: float, y: float) -> QPointF:
        r, s = self.canvas_rect(), self._scale()
        return QPointF(r.x() + x * s, r.y() + y * s)

    def _other_rects(self, moving: SceneItem) -> list[tuple[float, float, float, float]]:
        """Screen-aligned boxes of the other visible items, for snapping to them."""
        rects = []
        for other in self.scene.items if self.scene else []:
            size = self.sizes.get(other.source)
            if other is moving or not other.visible or not size or other.rotation % 360 != 0:
                continue
            w, h = other.cropped_size(size)
            if w and h:
                rects.append((other.x, other.y, w * other.scale, h * other.scale))
        return rects

    def _selected_item(self) -> tuple[SceneItem, tuple[int, int]] | None:
        if self.scene is None or self.selected_index is None or self.selected_index >= len(self.scene.items):
            return None
        item = self.scene.items[self.selected_index]
        size = self.sizes.get(item.source)
        return (item, size) if size and size[0] and size[1] else None

    # Painting --------------------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#141414"))
        p.setPen(QColor("#d8d8d8"))
        label = f"{self.title}  ·  {self.subtitle}" if self.subtitle else self.title
        p.drawText(8, 17, label)
        rect = self.canvas_rect()
        if self._image is None:
            p.fillRect(rect, QColor("#000000"))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Waiting for the renderer…")
        else:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(rect, self._image)
        self._paint_selection(p)
        p.end()

    def _paint_selection(self, p: QPainter) -> None:
        selected = self._selected_item()
        if selected is None:
            return
        item, size = selected
        quad = item.quad(size)
        if not quad:
            return
        tl, tr, bl, br = (self.to_widget(x, y) for x, y, _u, _v in quad)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(ACCENT, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([tl, tr, br, bl]))
        p.setBrush(ACCENT)
        half = HANDLE_PX / 2
        for corner in (tl, tr, bl, br):
            p.drawRect(QRectF(corner.x() - half, corner.y() - half, HANDLE_PX, HANDLE_PX))

    # Mouse and keyboard --------------------------------------------------------------------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self.scene is None:
            return
        point = self.to_canvas(event.position())
        radius = HANDLE_PX / self._scale()
        selected = self._selected_item()
        if selected is not None:
            item, size = selected
            corner = edit.handle_at(item, size, point, radius)
            if corner is not None:
                alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                self._drag = ("crop" if alt else "scale", corner, event.position(), item.model_copy())
                return
        index = edit.hit_test(self.scene.items, self.sizes, point)
        self.selected_index = index
        self.selected.emit(self.canvas_id, -1 if index is None else index)
        if index is not None:
            self._drag = ("move", None, event.position(), self.scene.items[index].model_copy())
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is None:
            self._update_cursor(event.position())
            return
        selected = self._selected_item()
        if selected is None:
            return
        item, size = selected
        mode, corner, start_pos, start_item = self._drag
        point = self.to_canvas(event.position())
        if mode == "move":
            scale = self._scale()
            delta = ((event.position().x() - start_pos.x()) / scale, (event.position().y() - start_pos.y()) / scale)
            prefs = snap_settings()
            off = not prefs["enabled"] or event.modifiers() & Qt.KeyboardModifier.ControlModifier
            snap = 0.0 if off else prefs["distance"] / scale
            others = self._other_rects(item) if prefs["sources"] else []
            edit.move(item, start_item, size, delta, self.aspect, snap,
                      edges=prefs["edges"], center=prefs["center"], others=others)
        elif mode == "scale":
            edit.scale_from_corner(item, start_item, size, corner, point)
        else:
            edit.crop_from_corner(item, start_item, size, corner, point)
        self.edited.emit(self.canvas_id, False)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            moved = event.position() != self._drag[2]
            self._drag = None
            if moved:
                self.edited.emit(self.canvas_id, True)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        selected = self._selected_item()
        steps = {
            Qt.Key.Key_Left: (-1, 0),
            Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, -1),
            Qt.Key.Key_Down: (0, 1),
        }
        if selected is None or event.key() not in steps:
            super().keyPressEvent(event)
            return
        dx, dy = steps[event.key()]
        factor = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        edit.nudge(selected[0], dx * factor, dy * factor)
        self.edited.emit(self.canvas_id, True)
        self.update()

    def _update_cursor(self, pos: QPointF) -> None:
        if self.scene is None:
            return
        point = self.to_canvas(pos)
        selected = self._selected_item()
        if selected is not None:
            corner = edit.handle_at(selected[0], selected[1], point, HANDLE_PX / self._scale())
            if corner is not None:
                diagonal = corner in (0, 3)
                self.setCursor(Qt.CursorShape.SizeFDiagCursor if diagonal else Qt.CursorShape.SizeBDiagCursor)
                return
        if edit.hit_test(self.scene.items, self.sizes, point) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()
