"""A widget that shows one canvas and lets you edit the active scene's items with the mouse.

- Click an item to select it; drag to move it (snapping options are in Settings → General).
- Drag a corner handle to scale; hold Alt while dragging a handle to crop instead.
- Arrow keys nudge the selected item by 1 px, or 10 px with Shift.
- Double-click a part to edit it in place: the show stays on screen, dimmed around the part, and the
  mouse edits the part's own items. Double-click outside it or press Esc to finish.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from sprigcast.app import safe_zones, theme
from sprigcast.app.terms import PART, a
from sprigcast.core import edit
from sprigcast.core.scene import Scene, SceneItem

HANDLE_PX = 8  # handle size on screen
ZOOM_MIN, ZOOM_MAX = 0.25, 8.0  # Build canvas zoom, 1 = fits
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
ACCENT = QColor(theme.ACCENT)


class Preview(QWidget):
    # canvas id, item index (-1 = nothing selected)
    selected = Signal(str, int)
    # canvas id, finished: False while dragging, True when the edit is done (save then)
    edited = Signal(str, bool)
    # canvas id, item index: double-click on an item (a part opens in place), or -1 outside / Esc
    opened = Signal(str, int)
    # canvas id, item index (-1 = empty canvas), where on screen: right-click asks for a menu there
    menu_requested = Signal(str, int, QPoint)
    # canvas id: the small "twin" of the other shape in Build was clicked (swap to editing it)
    swap_requested = Signal(str)
    # A sprig dragged in from the library: canvas id, part id.
    sprig_dropped = Signal(str, str)

    def __init__(self, canvas_id: str, title: str, aspect: tuple[int, int], parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas_id = canvas_id
        self.title = title
        self.subtitle = ""
        self.show_title = True  # Build's big canvas hides it: the bloom's name is already above the card
        self.aspect = aspect
        self.scene: Scene | None = None
        self.sizes: dict[str, tuple[int, int]] = {}
        self.statuses: dict[str, str] = {}  # source id -> status, from the engine (empty pages get a placeholder)
        self.selected_index: int | None = None
        self.transparent = True  # show see-through canvas areas as a checkerboard
        self.live_look = False  # Live's stage: no checkerboard frame, room for the on-air ring
        self.align = "center"  # Live's stage: "right"/"left" so two canvases sit together, evenly spaced
        self.ring: str | None = None
        self.zoom = 1.0  # 1 = the whole canvas fits; the wheel zooms at the mouse (Build only), Ctrl+0 fits again
        self.zoom_locked = False  # Build's "Fit to window, locked": the wheel and drags leave the view alone
        self.output_size = None  # Build: a callable giving the canvas's output size, for the scaling menu
        self.pan = QPointF(0, 0)  # widget pixels the zoomed canvas is shifted by (middle-drag or Space+drag)
        self._panning: QPointF | None = None
        self._space_down = False
        self.keep_aspect = True  # resizing keeps the shape; Shift while dragging does the opposite
        self.twin = False  # Build's small view of the other shape: click to swap, never edited
        self.ghost_index: int | None = None  # on a twin: the item matching the one selected, outlined  # a colour ring round the picture, such as LIVE pink while on air
        self.editable = True  # False in Live mode: the previews only show, so a stray click can't move things
        # Editing a part in place: where its layout sits on this canvas (see set_inner).
        self.inner: tuple[float, float, float, float, float] | None = None  # x, y, scale, crop left, crop top
        self.inner_size: tuple[int, int] | None = None  # the part layout's own size
        self.inner_rect: tuple[float, float, float, float] | None = None  # visible part area, canvas pixels
        self._inner_image: QImage | None = None
        self.empty_text = "Waiting for the renderer…"  # shown until the first frame arrives
        self.hint: tuple[str, str] | None = None  # an empty bloom or sprig: a title and a line, in a card
        self.safe_zones = "Off"  # see safe_zones.CHOICES; only portrait canvases draw them
        self.live_safe_zones = "Off"  # Live keeps its own choice (off unless picked on the canvas's right-click)
        self.part_names: dict[str, str] = {}  # placement source id -> part name, for the hover hint  # the part alone, drawn bright over the dimmed show
        self._image: QImage | None = None
        self._image_data: bytes | None = None  # what _image draws from, kept alive while it does
        self._drag: tuple[str, int | None, QPointF, SceneItem] | None = None  # mode, corner, start, item copy
        self.setMinimumSize(160, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)  # sprigs dragged in from the library
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)

    # Data from the main window -------------------------------------------------

    def set_frame(self, data: bytes, width: int, height: int, canvas_size: tuple[int, int] | None = None) -> None:
        """An image of the canvas; it may be smaller than the canvas (shrunk to fit on screen)."""
        fmt = QImage.Format.Format_RGBA8888_Premultiplied if self.transparent else QImage.Format.Format_RGBA8888
        if isinstance(data, bytes):
            # bytes never change, so the image can use them in place while we hold them: no copy per frame.
            self._image_data = data
            self._image = QImage(data, width, height, width * 4, fmt)
        else:  # a reused or changing buffer: copy() detaches the image from it
            self._image_data = None
            self._image = QImage(data, width, height, width * 4, fmt).copy()
        self.aspect = canvas_size or (width, height)  # editing works in canvas pixels, whatever the image size
        self.update()

    def set_inner_frame(self, data: bytes, width: int, height: int) -> None:
        """The part being edited in place, rendered alone (premultiplied RGBA)."""
        if self.inner is None:
            return
        self._inner_image = QImage(data, width, height, width * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()
        self.update()

    def set_scene(self, scene: Scene | None, selected_index: int | None) -> None:
        self.scene = scene
        self.selected_index = selected_index
        self.update()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        """A preview that isn't showing keeps no picture (up to 8 MB); a new one arrives when it is shown."""
        self._image = self._inner_image = self._image_data = None
        super().hideEvent(event)

    def set_inner(self, placement: SceneItem | None, part_size: tuple[int, int] | None) -> None:
        """Edit a part in place: `placement` is the part's item in the show, `part_size` its layout's size.

        Mouse and keyboard then work in the part's coordinates, and set_scene() takes the part layout.
        None goes back to editing the show.
        """
        if placement is None or not part_size:
            self.inner = self.inner_size = self.inner_rect = None
            self._inner_image = None
        else:
            left, top, _r, _b = placement.crop
            px, py, pw, ph = placement.picture_rect(part_size)
            k = placement.picture_scale(part_size)[0]
            self.inner = (placement.x + px, placement.y + py, k, left, top)
            self.inner_size = part_size
            self.inner_rect = (placement.x + px, placement.y + py, pw, ph)
        self._drag = None
        self.update()

    # Coordinates -------------------------------------------------------------------

    def canvas_rect(self) -> QRectF:
        """Where the canvas is drawn inside the widget, leaving room for the title line."""
        top = 24.0 if self.show_title else 4.0
        pad = 6.0 if self.live_look else 0.0  # room for the on-air ring
        avail_w, avail_h = float(self.width()) - 2 * pad, float(self.height()) - top - pad
        aw, ah = self.aspect
        scale = self._fit_scale() * self.zoom
        w, h = aw * scale, ah * scale
        spare = avail_w - w
        x = pad + (spare if self.align == "right" else 0.0 if self.align == "left" else spare / 2)
        return QRectF(x + self.pan.x(), top + (avail_h - h) / 2 + self.pan.y(), w, h)

    def _fit_scale(self) -> float:
        """Screen pixels per canvas pixel when the whole canvas fits."""
        top = 24.0 if self.show_title else 4.0
        pad = 6.0 if self.live_look else 0.0
        avail_w, avail_h = float(self.width()) - 2 * pad, float(self.height()) - top - pad
        aw, ah = self.aspect
        return max(1e-6, min(avail_w / aw, avail_h / ah))

    def pixel_scale(self) -> float:
        """Screen pixels per canvas pixel right now: 1.0 is the canvas at its real size (OBS's 100%)."""
        return self._fit_scale() * self.zoom

    def set_pixel_scale(self, k: float) -> None:
        """Show the canvas at `k` screen pixels per canvas pixel, centred (1.0 = real size)."""
        self.zoom = min(ZOOM_MAX, max(ZOOM_MIN, k / self._fit_scale()))
        self.pan = QPointF(0, 0)
        self.update()

    def zoom_at(self, factor: float, at: QPointF | None = None) -> None:
        """Zoom by `factor` keeping the canvas point under `at` (widget pixels) where it is."""
        at = at or QPointF(self.width() / 2, self.height() / 2)
        before = self.canvas_rect()
        if self.zoom_locked:
            return
        new = min(ZOOM_MAX, max(ZOOM_MIN, self.zoom * factor))
        if abs(new - 1.0) < 0.04:  # passing through "fits" snaps to it, so it's easy to land on
            new = 1.0
        if new == self.zoom:
            return
        fx = (at.x() - before.x()) / before.width()
        fy = (at.y() - before.y()) / before.height()
        self.zoom = new
        if new == 1.0:
            self.pan = QPointF(0, 0)
        else:
            after = self.canvas_rect()
            self.pan += QPointF(at.x() - (after.x() + fx * after.width()), at.y() - (after.y() + fy * after.height()))
        self.update()

    def fit_view(self) -> None:
        self.zoom, self.pan = 1.0, QPointF(0, 0)
        self.update()

    def _space(self) -> tuple[float, float, float]:
        """Widget x, y of the editing space's origin and widget pixels per space pixel.

        The space is the canvas, or the part's layout while a part is edited in place.
        """
        r = self.canvas_rect()
        k = r.width() / self.aspect[0]
        if self.inner is None:
            return r.x(), r.y(), k
        x, y, scale, left, top = self.inner
        return r.x() + (x - left * scale) * k, r.y() + (y - top * scale) * k, k * scale

    def _scale(self) -> float:
        return self._space()[2]

    def _bounds(self) -> tuple[int, int]:
        """Size of the editing space, for snapping to its edges."""
        return self.inner_size or self.aspect

    def to_canvas(self, p: QPointF) -> tuple[float, float]:
        """A widget point in editing-space pixels (canvas pixels, or the part's while editing in place)."""
        ox, oy, s = self._space()
        return (p.x() - ox) / s, (p.y() - oy) / s

    def to_widget(self, x: float, y: float) -> QPointF:
        ox, oy, s = self._space()
        return QPointF(ox + x * s, oy + y * s)

    def _other_rects(self, moving: SceneItem) -> list[tuple[float, float, float, float]]:
        """Screen-aligned boxes of the other visible items, for snapping to them."""
        rects = []
        for other in self.scene.items if self.scene else []:
            size = self.sizes.get(other.source)
            if other is moving or not other.visible or not size or other.rotation % 360 != 0:
                continue
            w, h = other.drawn_size(size)
            if w and h:
                rects.append((other.x, other.y, w, h))
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
        p.fillRect(self.rect(), QColor(theme.PANEL))
        rect = self.canvas_rect()
        p.save()
        band = 22 if self.show_title else 0  # zoomed in, the canvas stays under the title (when there is one)
        p.setClipRect(QRectF(0, band, self.width(), self.height() - band))
        # Square corners: the canvas is drawn exactly as it goes out to the stream and the recording.
        if self._image is None:
            p.fillRect(rect, QColor("#000000"))
            p.setPen(QColor(theme.MUTED))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.empty_text)
        else:
            if self.transparent:
                self._paint_checker(p, rect)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(rect, self._image)
        p.restore()
        p.setPen(QColor(theme.MUTED))
        label = f"{self.title}  ·  {self.subtitle}" if self.subtitle else self.title
        if self.zoom != 1.0 and not self._zoom_bar_shown():
            label += f"   ·   {self.pixel_scale() * 100:.0f}%  (Ctrl+0 to fit)"
        if not self.show_title:
            if self.zoom != 1.0 and not self._zoom_bar_shown():  # only the zoom, in a small chip in the corner
                text = f"{self.zoom * 100:.0f}%  ·  Ctrl+0 to fit"
                chip = QRectF(0, 0, p.fontMetrics().horizontalAdvance(text) + 20, 22)
                chip.moveTopRight(QPointF(rect.right() - 8 if rect.right() < self.width() else self.width() - 8, 8))
                p.save()
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawRoundedRect(chip, 11, 11)
                p.setPen(QColor(theme.TEXT))
                p.drawText(chip, Qt.AlignmentFlag.AlignCenter, text)
                p.restore()
        elif self.live_look:  # the title starts where the canvas does, and never runs past it
            left = max(8.0, rect.x() + 2)
            label = p.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(max(60.0, rect.width() - 4)))
            p.drawText(int(left), 17, label)
        else:
            p.drawText(8, 17, label)
        if self.live_look:
            if self.ring:
                p.setPen(QPen(QColor(self.ring), 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(rect.adjusted(-3, -3, 3, 3))  # square, like the canvas it frames
        self._paint_empty_pages(p)
        self._paint_hint(p, rect)
        self._paint_inner(p)
        self._paint_safe_zones(p)
        self._paint_selection(p)
        self._paint_zoom_bar(p)
        p.end()

    def _zoom_bar_shown(self) -> bool:
        return self.editable and not self.twin and not self.live_look and self.width() > 260

    def _zoom_bar(self) -> dict[str, QRectF]:
        """The little zoom pill in the bottom-right corner: −, the zoom (click: fit), +."""
        h, button, label = 28.0, 28.0, 60.0
        right, bottom = self.width() - 12.0, self.height() - 12.0
        left = right - (button * 2 + label)
        return {"out": QRectF(left, bottom - h, button, h), "fit": QRectF(left + button, bottom - h, label, h),
                "in": QRectF(left + button + label, bottom - h, button, h)}

    def _paint_zoom_bar(self, p: QPainter) -> None:
        if not self._zoom_bar_shown():
            return
        parts = self._zoom_bar()
        whole = parts["out"].united(parts["in"])
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(theme.EDGE_STRONG), 1))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(whole, whole.height() / 2, whole.height() / 2)
        hover = getattr(self, "_zoom_hover", None)
        for key, rect in parts.items():
            if key == hover:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.RAISED))
                p.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 12, 12)
            text = {"out": "−", "in": "+"}.get(key, ("Fit" if self.zoom_locked else f"{self.pixel_scale() * 100:.0f}%"))
            p.setPen(QColor(theme.TEXT if key != "fit" or self.zoom != 1.0 else theme.MUTED))
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    def _scaling_menu(self, at) -> None:
        """Like OBS's Preview Scaling: fit (or fit and locked), the canvas's real size, its output size, zoom."""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        aw, ah = self.aspect
        fit = menu.addAction("Fit to window")
        fit.setCheckable(True)
        fit.setChecked(self.zoom == 1.0 and self.pan.isNull() and not self.zoom_locked)
        fit.triggered.connect(lambda: (self.set_zoom_locked(False), self.fit_view()))
        lock = menu.addAction("Fit and lock")
        lock.setCheckable(True)
        lock.setChecked(self.zoom_locked)
        lock.triggered.connect(lambda on: self.set_zoom_locked(on))
        menu.addSeparator()
        real = menu.addAction(f"Canvas ({aw} x {ah})")
        real.triggered.connect(lambda: (self.set_zoom_locked(False), self.set_pixel_scale(1.0)))
        out = self.output_size() if callable(self.output_size) else None
        if out and tuple(out) != (aw, ah):
            action = menu.addAction(f"Output ({out[0]} x {out[1]})")
            action.triggered.connect(lambda: (self.set_zoom_locked(False), self.set_pixel_scale(out[0] / aw)))
        menu.addSeparator()
        menu.addAction("Zoom in\tCtrl+=").triggered.connect(lambda: (self.set_zoom_locked(False), self.zoom_at(1.25)))
        menu.addAction("Zoom out\tCtrl+-").triggered.connect(lambda: (self.set_zoom_locked(False), self.zoom_at(0.8)))
        menu.exec(at - QPoint(0, menu.sizeHint().height() + 6))

    def set_zoom_locked(self, on: bool) -> None:
        self.zoom_locked = bool(on)
        if on:
            self.fit_view()
        from PySide6.QtCore import QSettings

        QSettings().setValue("build/zoom_locked", self.zoom_locked)
        self.update()

    def _zoom_bar_at(self, pos: QPointF) -> str | None:
        if not self._zoom_bar_shown():
            return None
        return next((key for key, rect in self._zoom_bar().items() if rect.contains(pos)), None)

    _mark = None  # the SprigCast S (data/mark.svg), loaded once

    @classmethod
    def _brand_mark(cls):
        if cls._mark is None:
            from sprigcast.app.main import brand_mark

            cls._mark = brand_mark()
        return cls._mark

    def _paint_hint(self, p: QPainter, rect: QRectF) -> None:
        """A friendly card in the middle of an empty bloom or sprig, instead of a bare checkerboard."""
        if self.hint is None or self.twin or self.inner is not None or rect.width() < 220:
            return
        from sprigcast.app import icons

        title, line = self.hint
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        big = QFont(self.font())
        big.setPixelSize(16)
        big.setWeight(QFont.Weight.DemiBold)
        small = QFont(self.font())
        small.setPixelSize(12)
        width = min(rect.width() - 40, max(QFontMetrics(big).horizontalAdvance(title),
                                           QFontMetrics(small).horizontalAdvance(line)) + 56)
        card = QRectF(0, 0, width, 112)
        card.moveCenter(rect.center())
        p.setPen(QPen(QColor(theme.EDGE_STRONG), 1))
        p.setBrush(QColor(theme.PANEL))
        p.drawRoundedRect(card, 20, 20)
        icons.paint_icon(p, QRectF(card.center().x() - 11, card.y() + 16, 22, 22), "sprig", theme.ACCENT)
        p.setFont(big)
        p.setPen(QColor(theme.TEXT_BRIGHT))
        p.drawText(QRectF(card.x() + 12, card.y() + 44, card.width() - 24, 24), Qt.AlignmentFlag.AlignCenter, title)
        p.setFont(small)
        p.setPen(QColor(theme.MUTED))
        p.drawText(QRectF(card.x() + 12, card.y() + 70, card.width() - 24, 22), Qt.AlignmentFlag.AlignCenter,
                   QFontMetrics(small).elidedText(line, Qt.TextElideMode.ElideRight, int(card.width() - 24)))
        p.restore()

    def _paint_empty_pages(self, p: QPainter) -> None:
        """A browser source with no page yet: a solid card with the SprigCast S and its size, so it can be
        placed and sized before the page exists. Only here in the editor: viewers see nothing."""
        if not self.editable or self.scene is None or self._image is None:
            return
        for item in self.scene.items:
            if not item.visible or self.statuses.get(item.source) != "No page set":
                continue
            size = self.sizes.get(item.source)
            quad = item.quad(size) if size and size[0] and size[1] else None
            if not quad:
                continue
            tl, tr, bl, br = (self.to_widget(x, y) for x, y, _u, _v in quad)
            shape = QPolygonF([tl, tr, br, bl])
            box = shape.boundingRect()
            p.save()
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(QColor(theme.ACCENT), 1.5))
            p.setBrush(QColor(theme.STAGE_EMPTY))
            p.drawPolygon(shape)
            side = min(box.width(), box.height())
            if side >= 24:
                mark = min(72.0, side * 0.4)
                text_h = 34 if box.height() >= mark + 44 else 0
                top = box.center().y() - (mark + text_h) / 2
                self._brand_mark().paint(p, QRectF(box.center().x() - mark / 2, top, mark, mark).toRect())
                if text_h and box.width() >= 90:
                    p.setPen(QColor(theme.TEXT))
                    font = p.font()
                    font.setPointSizeF(max(7.0, font.pointSizeF() * 0.9))
                    p.setFont(font)
                    p.drawText(QRectF(box.left() + 4, top + mark + 4, box.width() - 8, text_h),
                               Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                               f"Browser · {size[0]}×{size[1]}\nAdd a link in Sources")
            p.restore()

    def _paint_safe_zones(self, p: QPainter) -> None:
        """Where the vertical apps put their buttons and captions: tinted boxes, and the clear middle outlined."""
        w, h = self.aspect
        found = safe_zones.zones(self.live_safe_zones if self.live_look else self.safe_zones, w, h)
        if not found or self._image is None or self.inner is not None or not (self.editable or self.live_look):
            return
        p.save()
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.8))
        p.setFont(font)
        for label, x, y, zw, zh in found:
            a, b = self.to_widget(x, y), self.to_widget(x + zw, y + zh)
            box = QRectF(a, b)
            p.fillRect(box, QColor(255, 95, 162, 38))
            p.setPen(QPen(QColor(255, 95, 162, 150), 1, Qt.PenStyle.DashLine))
            p.drawRect(box)
            if box.width() > 40 and box.height() > 12:
                p.setPen(QColor(255, 220, 235))
                p.drawText(box.adjusted(3, 2, -3, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                           | Qt.TextFlag.TextWordWrap, label)
        clear = safe_zones.clear_area(self.safe_zones, w, h)
        if clear is not None:
            x, y, cw, ch = clear
            p.setPen(QPen(QColor(61, 220, 132, 200), 1.5, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(self.to_widget(x, y), self.to_widget(x + cw, y + ch)))
        p.restore()

    _checker: QBrush | None = None

    @classmethod
    def _paint_checker(cls, p: QPainter, rect: QRectF, cell: int = 12) -> None:
        if cls._checker is None:
            tile = QPixmap(2 * cell, 2 * cell)
            tile.fill(QColor("#2b2b2b"))
            tp = QPainter(tile)
            tp.fillRect(0, 0, cell, cell, QColor("#3d3d3d"))
            tp.fillRect(cell, cell, cell, cell, QColor("#3d3d3d"))
            tp.end()
            cls._checker = QBrush(tile)
        p.fillRect(rect, cls._checker)

    def _paint_inner(self, p: QPainter) -> None:
        """While a part is edited in place: dim the rest of the show and outline the part.

        With the part's own image, the whole show dims and the part is drawn bright on top (so a part
        that fills the canvas still stands out); until that arrives, only the area around it dims.
        """
        if self.inner_rect is None:
            return
        r = self.canvas_rect()
        k = r.width() / self.aspect[0]
        x, y, w, h = self.inner_rect
        part = QRectF(r.x() + x * k, r.y() + y * k, w * k, h * k)
        p.save()
        p.setClipRect(r)
        shade = QColor(0, 0, 0, 150)
        image = self._inner_image
        if image is not None and self.inner is not None and self.inner_size == (image.width(), image.height()):
            p.fillRect(r, shade)
            _x, _y, scale, left, top = self.inner
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(part, image, QRectF(left, top, w / scale, h / scale))
        else:
            p.fillRect(QRectF(r.left(), r.top(), r.width(), max(0.0, part.top() - r.top())), shade)
            p.fillRect(QRectF(r.left(), part.bottom(), r.width(), max(0.0, r.bottom() - part.bottom())), shade)
            p.fillRect(QRectF(r.left(), part.top(), max(0.0, part.left() - r.left()), part.height()), shade)
            p.fillRect(QRectF(part.right(), part.top(), max(0.0, r.right() - part.right()), part.height()), shade)
        pen = QPen(ACCENT, 1.0, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(part)
        p.restore()

    def _paint_ghost(self, p: QPainter) -> None:
        """On a twin: a dashed outline round the same thing as is selected in the shape being edited."""
        if not self.twin or self.scene is None or self.ghost_index is None or self.ghost_index >= len(self.scene.items):
            return
        item = self.scene.items[self.ghost_index]
        size = self.sizes.get(item.source)
        box = item.box(size) if size and size[0] and size[1] else None
        if not box:
            return
        tl, tr, bl, br = (self.to_widget(x, y) for x, y in box)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(ACCENT, 1.5)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([tl, tr, br, bl]))

    def _paint_selection(self, p: QPainter) -> None:
        self._paint_ghost(p)
        selected = self._selected_item() if self.editable else None
        if selected is None:
            return
        item, size = selected
        box = item.box(size)
        if not box:
            return
        tl, tr, bl, br = (self.to_widget(x, y) for x, y in box)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(ACCENT, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPolygon(QPolygonF([tl, tr, br, bl]))
        p.setBrush(ACCENT)
        half = HANDLE_PX / 2
        for corner in (tl, tr, bl, br):
            p.drawRect(QRectF(corner.x() - half, corner.y() - half, HANDLE_PX, HANDLE_PX))
        # Side handles: drag to stretch (aspect unlocked) or, holding Alt, to crop that side.
        p.setBrush(QColor(theme.PANEL))
        p.setPen(QPen(ACCENT, 1.5))
        for a_, b_ in ((tl, bl), (tr, br), (tl, tr), (bl, br)):
            mid = (a_ + b_) / 2
            p.drawEllipse(mid, half * 0.9, half * 0.9)
        # The pin point: where it turns around, and what Place's X and Y measure.
        pin = self.to_widget(*item.pin(size))
        p.setPen(QPen(ACCENT, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(pin, half * 1.2, half * 1.2)
        p.setBrush(ACCENT)
        p.drawEllipse(pin, 1.6, 1.6)

    # Mouse and keyboard --------------------------------------------------------------------

    def _takes_sprig(self, event) -> bool:
        from sprigcast.app.library import SPRIG_MIME

        return self.editable and not self.twin and self.scene is not None and event.mimeData().hasFormat(SPRIG_MIME)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._takes_sprig(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        self.dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        from sprigcast.app.library import SPRIG_MIME

        if not self._takes_sprig(event):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.sprig_dropped.emit(self.canvas_id, bytes(event.mimeData().data(SPRIG_MIME)).decode())

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.twin:
            if event.button() == Qt.MouseButton.LeftButton:
                self.swap_requested.emit(self.canvas_id)
            return
        bar = self._zoom_bar_at(event.position()) if event.button() == Qt.MouseButton.LeftButton else None
        if bar is not None:
            if bar == "fit":
                self._scaling_menu(self.mapToGlobal(self._zoom_bar()["fit"].topLeft().toPoint()))
            else:
                if self.zoom_locked:
                    self.set_zoom_locked(False)
                self.zoom_at(1.25 if bar == "in" else 1 / 1.25)
            return
        middle = event.button() == Qt.MouseButton.MiddleButton
        can_pan = self.editable and not self.zoom_locked and (self.zoom != 1.0 or not self.pan.isNull())
        if (middle or (self._space_down and event.button() == Qt.MouseButton.LeftButton)) and can_pan:
            self._panning = event.position() - self.pan  # pan the zoomed canvas
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.RightButton and self.scene is not None and self.editable:
            index = edit.hit_test(self.scene.items, self.sizes, self.to_canvas(event.position()))
            if index != self.selected_index:
                self.selected_index = index
                self.selected.emit(self.canvas_id, -1 if index is None else index)
                self.update()
            self.menu_requested.emit(self.canvas_id, -1 if index is None else index,
                                     event.globalPosition().toPoint())
            return
        if event.button() == Qt.MouseButton.RightButton and not self.editable and not self.twin:
            self.menu_requested.emit(self.canvas_id, -1, event.globalPosition().toPoint())  # Live: projector menu
            return
        if event.button() != Qt.MouseButton.LeftButton or self.scene is None or not self.editable:
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
            self._drag_prefs = snap_settings()  # read once per drag, not on every mouse move
            self._drag_others = None
        self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._panning is not None:
            self.pan = event.position() - self._panning
            self.update()
            return
        if self._drag is None:
            bar = self._zoom_bar_at(event.position())
            if bar != getattr(self, "_zoom_hover", None):
                self._zoom_hover = bar
                self.update()
            if bar is not None:
                self.setCursor(Qt.CursorShape.PointingHandCursor)
                return
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
            prefs = getattr(self, "_drag_prefs", None) or snap_settings()
            off = not prefs["enabled"] or event.modifiers() & Qt.KeyboardModifier.ControlModifier
            snap = 0.0 if off else prefs["distance"] / scale
            if prefs["sources"] and getattr(self, "_drag_others", None) is None:
                self._drag_others = self._other_rects(item)  # the others stay put while this one moves
            others = self._drag_others if prefs["sources"] else []
            edit.move(item, start_item, size, delta, self._bounds(), snap,
                      edges=prefs["edges"], center=prefs["center"], others=others)
        elif mode == "scale":
            keep = self.keep_aspect != bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            edit.scale_from_corner(item, start_item, size, corner, point, keep_aspect=keep)
        else:
            edit.crop_from_corner(item, start_item, size, corner, point)
        self.edited.emit(self.canvas_id, False)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._panning is not None:
            self._panning = None
            self.unsetCursor()
            return
        if self._drag is not None and event.button() == Qt.MouseButton.LeftButton:
            moved = event.position() != self._drag[2]
            self._drag = None
            if moved:
                self.edited.emit(self.canvas_id, True)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton or self.scene is None or not self.editable:
            return
        index = edit.hit_test(self.scene.items, self.sizes, self.to_canvas(event.position()))
        if self.inner is not None and index is not None:
            return  # inside the part: an ordinary click
        self._drag = None
        self.opened.emit(self.canvas_id, -1 if index is None else index)

    def wheelEvent(self, event) -> None:  # noqa: N802
        """Build: the wheel zooms at the mouse (in past fit and out below it); Shift+wheel moves sideways.
        Middle-drag or Space+drag moves around. Live never zooms."""
        if self.twin or not self.editable:
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120 or event.angleDelta().x() / 120
        if self.zoom_locked:
            return
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.pan += QPointF(steps * 80, 0)
            self.update()
        else:
            self.zoom_at(1.15 ** steps, event.position())

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.NativeGesture and self.editable and not self.twin:
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:  # touchpad pinch
                self.zoom_at(1 + event.value(), event.position())
                return True
        return super().event(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Space and not event.isAutoRepeat():
            self._space_down = False
        super().keyReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        ctrl = event.modifiers() & Qt.KeyboardModifier.ControlModifier
        if event.key() == Qt.Key.Key_Space:
            self._space_down = True  # Space + drag pans, like design tools
            return
        if ctrl and event.key() == Qt.Key.Key_0:
            self.fit_view()
            return
        if ctrl and event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal) and self.editable:
            self.zoom_at(1.25)
            return
        if ctrl and event.key() == Qt.Key.Key_Minus and self.editable:
            self.zoom_at(1 / 1.25)
            return
        if event.key() == Qt.Key.Key_Escape and self.inner is not None:
            self.opened.emit(self.canvas_id, -1)
            return
        selected = self._selected_item() if self.editable else None
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
        if self.twin:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            return
        if self.scene is None or not self.editable:
            self.unsetCursor()
            return
        point = self.to_canvas(pos)
        selected = self._selected_item()
        if selected is not None:
            corner = edit.handle_at(selected[0], selected[1], point, HANDLE_PX / self._scale())
            if corner is not None:
                shapes = {0: Qt.CursorShape.SizeFDiagCursor, 3: Qt.CursorShape.SizeFDiagCursor,
                          1: Qt.CursorShape.SizeBDiagCursor, 2: Qt.CursorShape.SizeBDiagCursor,
                          4: Qt.CursorShape.SizeHorCursor, 5: Qt.CursorShape.SizeHorCursor,
                          6: Qt.CursorShape.SizeVerCursor, 7: Qt.CursorShape.SizeVerCursor}
                self.setCursor(shapes[corner])
                tip = "Drag to resize · hold Alt to crop · Shift flips aspect lock"
                if self.toolTip() != tip:
                    self.setToolTip(tip)
                return
        index = edit.hit_test(self.scene.items, self.sizes, point)
        if index is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()
        name = self.part_names.get(self.scene.items[index].source) if index is not None else None
        hint = f"{name}: {a(PART)}. Double-click to edit it here." if name and self.inner is None else ""
        if hint != self.toolTip():
            self.setToolTip(hint)
