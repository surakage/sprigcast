"""The bloom tiles on Live mode's screen: one card per bloom (with its last on-air picture), per canvas.

In Live mode the editing docks step aside; switching scenes is one click on a big button
(linked scenes on the other canvas follow, as everywhere else).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.terms import PART_MARK, PARTS, cap
from sprigcast.core.scene import SceneCollection

BUTTON_HEIGHT = 30
BUTTON_WIDTH = (96, 190)  # each button fits its name between these, then wraps onto the next line
TILE_SIZE = (184, 150)  # a bloom tile: a 16:9 thumbnail with the name under it


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps to a new line when the row is full, like words."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6):
        super().__init__(parent)
        self._items: list = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect, move: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._spacing, 0
            if move:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line = max(line, hint.height())
        return y + line - rect.y()


def _compact(button: QPushButton, text: str) -> QPushButton:
    """A small button sized to its name (long names are shortened, the full name is in the tooltip)."""
    metrics = QFontMetrics(button.font())
    shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, BUTTON_WIDTH[1] - 24)
    button.setText(shown)
    if shown != text:
        button.setToolTip(text + ("\n" + button.toolTip() if button.toolTip() else ""))
    width = min(BUTTON_WIDTH[1], max(BUTTON_WIDTH[0], metrics.horizontalAdvance(shown) + 28))
    button.setFixedSize(width, BUTTON_HEIGHT)
    return button


class BloomTile(QPushButton):
    """A bloom as a card: its last on-air picture (or its folder colour) with the name under it.

    A pink ring means on air, a dashed green one means lined up next (Studio mode)."""

    def __init__(self, name: str, colour: str | None = None):
        super().__init__(name)
        self.colour = colour
        self.thumb: QImage | None = None
        self.setCheckable(True)
        self.setFixedSize(*TILE_SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(*TILE_SIZE)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(*TILE_SIZE)

    def set_thumb(self, image: QImage | None) -> None:
        self.thumb = image
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        hover = self.underMouse() and self.isEnabled()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.RAISED if hover else theme.PANEL))
        p.drawRoundedRect(outer, 18, 18)
        pic = QRectF(8, 8, self.width() - 16, (self.width() - 16) * 9 / 16)
        clip = QPainterPath()
        clip.addRoundedRect(pic, 12, 12)
        p.save()
        p.setClipPath(clip)
        p.fillRect(pic, QColor(self.colour or theme.STAGE_EMPTY))
        if self.thumb is not None and not self.thumb.isNull():
            size = QSize(self.thumb.width(), self.thumb.height()).scaled(pic.size().toSize(),
                                                                          Qt.AspectRatioMode.KeepAspectRatio)
            target = QRectF(pic.x() + (pic.width() - size.width()) / 2, pic.y() + (pic.height() - size.height()) / 2,
                            size.width(), size.height())
            p.drawImage(target, self.thumb)
        p.restore()
        p.setPen(QColor(theme.TEXT_BRIGHT if self.isEnabled() else theme.MUTED))
        font = p.font()
        font.setPixelSize(14)
        font.setWeight(font.Weight.DemiBold)
        p.setFont(font)
        text_rect = QRectF(14, pic.bottom() + 8, self.width() - 28, self.height() - pic.bottom() - 14)
        name = QFontMetrics(font).elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width()))
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)
        ring = theme.LIVE if self.isChecked() else theme.ACCENT if self.property("staged") else None
        if ring:
            pen = QPen(QColor(ring), 2)
            if not self.isChecked():
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(outer.adjusted(1, 1, -1, -1), 17, 17)
        p.end()


class LivePanel(QWidget):
    def __init__(self, on_scene: Callable[[str], None], on_state: Callable[[str, str | None], None] | None = None,
                 on_studio: Callable[[bool], None] | None = None, on_go: Callable[[], None] | None = None,
                 on_edit: Callable[[str], None] | None = None):
        super().__init__()
        self.on_scene = on_scene
        self.on_edit = on_edit  # open a scene in Build mode (right-click menu)
        self.on_state = on_state or (lambda part_id, name: None)
        self.staged: set[str] = set()
        self.buttons: dict[str, QPushButton] = {}
        self.state_buttons: dict[tuple[str, str | None], QPushButton] = {}  # (sprig id, state name or None)
        self.thumbs: dict[str, QImage] = {}  # scene id -> its last on-air picture, small
        self._signature: tuple = ()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        # The on-air clock and REC line live in the window's header and Live's side column now;
        # these stay (hidden) so older callers and plugins can still set them.
        self.clock = QLabel("Offline")
        self.rec = QLabel("")
        self.clock.setVisible(False)
        self.rec.setVisible(False)
        top = QHBoxLayout()
        top.setContentsMargins(4, 0, 0, 0)
        title = QLabel("Blooms")
        font = title.font()
        font.setPixelSize(16)
        font.setWeight(font.Weight.DemiBold)
        title.setFont(font)
        top.addWidget(title)
        top.addStretch(1)
        self.studio = QCheckBox("Line up first")
        self.studio.setToolTip("Studio mode: click a bloom to line it up as Next, then press Bloom it to send it on air")
        self.studio.toggled.connect(lambda on: (on_studio or (lambda _on: None))(on))
        top.addWidget(self.studio)
        root.addLayout(top)
        self.go = QPushButton("Bloom it")
        self.go.setToolTip("Send the lined-up blooms on air (with the transition)")
        self.go.setMinimumHeight(38)
        self.go.setMinimumWidth(140)
        self.go.setStyleSheet(theme.BIG_GO_STYLE.replace("24px", "19px").replace("font-size: 16px", "font-size: 13px"))
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.clicked.connect(lambda: (on_go or (lambda: None))())
        self.go.setVisible(False)
        root.addWidget(self.go, 0, Qt.AlignmentFlag.AlignRight)  # the window moves it under the stage
        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        root.addLayout(self.rows)
        root.addStretch(1)

    def set_thumb(self, scene_id: str, image: QImage) -> None:
        self.thumbs[scene_id] = image
        tile = self.buttons.get(scene_id)
        if isinstance(tile, BloomTile):
            tile.set_thumb(image)

    def show_scenes(self, collection: SceneCollection) -> None:
        signature = (tuple((s.id, s.name, s.canvas, s.kind, s.folder, s.linked_to) for s in collection.scenes),
                     tuple((f.id, f.name, f.colour, f.live) for f in collection.folders),
                     tuple((p.id, p.name, tuple(s.name for s in p.states)) for p in collection.parts()),
                     tuple(c.id for c in collection.canvases if c.enabled))
        if signature != self._signature:
            self._signature = signature
            self._rebuild(collection)
        current = {p.id: p.state for p in collection.parts()}
        for (part_id, name), button in self.state_buttons.items():
            button.blockSignals(True)
            button.setChecked(current.get(part_id) == name)
            button.blockSignals(False)
        for canvas in collection.canvases:
            active = collection.active_scenes.get(canvas.id)
            for scene in collection.scenes_for(canvas.id):
                button = self.buttons.get(scene.id)
                if button is None:
                    continue  # not in a Live folder
                button.blockSignals(True)
                button.setChecked(scene.id == active)
                staged = scene.id in self.staged and scene.id != active
                if button.property("staged") != staged:
                    button.setProperty("staged", staged)
                    button.update()
                button.blockSignals(False)

    def _rebuild(self, collection: SceneCollection) -> None:
        self._collection = collection
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)  # off screen now, not at the next event loop turn
                widget.deleteLater()
        self.buttons = {}
        self.state_buttons = {}
        sections, shown = [], set()
        for canvas in collection.canvases:
            if not canvas.enabled:
                continue  # switched off with Settings → Show canvases
            # A bloom linked to one already shown switches with it, so it needs no tile of its own.
            groups = [(f, [s for s in shows if s.linked_to not in shown])
                      for f, shows in collection.live_scenes(canvas.id)]
            groups = [(f, shows) for f, shows in groups if shows]
            shown |= {s.id for _f, shows in groups for s in shows}
            if groups:
                sections.append((canvas, groups))
        for canvas, groups in sections:
            if len(sections) > 1:
                title = QLabel(canvas.id.capitalize())
                title.setStyleSheet(theme.MUTED_STYLE + "; margin-left: 4px;")
                self.rows.addWidget(title)
            for folder, group in groups:
                if folder is not None and len(groups) > 1:  # one Live folder needs no heading
                    heading = QLabel(f"●  {folder.name}")
                    heading.setStyleSheet(f"color: {folder.colour}; font-weight: bold; margin: 4px 0 0 4px;")
                    self.rows.addWidget(heading)
                self._add_grid(group, folder.colour if folder is not None else None)
        self._add_state_rows(collection)

    def _add_state_rows(self, collection: SceneCollection) -> None:
        """One row per sprig with Live states: a button per state, plus "As built"."""
        parts = [p for p in collection.parts() if p.states]
        if not parts:
            return
        title = QLabel(f"{cap(PARTS)}")
        title.setStyleSheet(theme.MUTED_STYLE + "; margin-top: 6px;")
        self.rows.addWidget(title)
        for part in parts:
            row_box = QWidget()
            row = QHBoxLayout(row_box)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            name = QLabel(f"{PART_MARK} {part.name}")
            name.setFixedWidth(110)
            row.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
            flow_box = QWidget()
            flow = FlowLayout(flow_box)
            for state in [None, *(s.name for s in part.states)]:
                button = QPushButton()
                button.setCheckable(True)
                button.setToolTip("Switches with a quick fade, on every bloom that uses it")
                _compact(button, "As built" if state is None else state)
                button.clicked.connect(lambda _=False, pid=part.id, st=state: self.on_state(pid, st))
                self.state_buttons[(part.id, state)] = button
                flow.addWidget(button)
            row.addWidget(flow_box, 1)
            self.rows.addWidget(row_box)

    def _add_grid(self, scenes, colour: str | None = None) -> None:
        flow_box = QWidget()
        flow = FlowLayout(flow_box, spacing=14)
        tint = QColor(colour).darker(260).name() if colour else None
        for scene in scenes:
            button = BloomTile(scene.name + ("  ⇄" if scene.linked_to else ""), tint)
            if scene.linked_to:
                button.setToolTip("Linked: switches together with its partner on the other canvas")
            else:
                partners = [s.name for s in self._collection.scenes if s.linked_to == scene.id]
                button.setToolTip(scene.name + (f"\nAlso switches: {', '.join(partners)}" if partners else ""))
            button.set_thumb(self.thumbs.get(scene.id))
            button.clicked.connect(lambda _=False, sid=scene.id: self.on_scene(sid))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, sid=scene.id, b=button: self.scene_menu(sid).exec(b.mapToGlobal(pos)))
            self.buttons[scene.id] = button
            flow.addWidget(button)
        self.rows.addWidget(flow_box)

    def scene_menu(self, scene_id: str) -> QMenu:
        menu = QMenu(self)
        label = "Line up next" if self.studio.isChecked() else "Show now"
        menu.addAction(label).triggered.connect(lambda: self.on_scene(scene_id))
        if self.on_edit is not None:
            menu.addAction("Edit in Build mode").triggered.connect(lambda: self.on_edit(scene_id))
        return menu

    def set_clock(self, text: str, live: bool) -> None:
        self.clock.setText(text)
        self.clock.setStyleSheet(f"color: {theme.LIVE}" if live else "")

    def set_recording(self, text: str) -> None:
        self.rec.setText(text)
