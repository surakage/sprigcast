"""The bloom tiles on Live mode's screen: one card per bloom (with its last on-air picture), per canvas.

In Live mode the editing docks step aside; switching scenes is one click on a big button
(linked scenes on the other canvas follow, as everywhere else).
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QSizePolicy,
    QLabel,
    QLayout,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme
from sprigcast.app.terms import PARTS, cap
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
    """A bloom as a pill button: a dot in its folder colour, then its name.

    A pink ring and dot mean on air. The
    last on-air picture is kept (Build's bloom pills show it) but not drawn here: pills keep many
    blooms on screen at once."""

    HEIGHT = 38
    TALL = 54  # two lines of name, for a list too narrow for it on one

    def __init__(self, name: str, colour: str | None = None):
        super().__init__(name)
        self.colour = colour
        self.thumb: QImage | None = None
        self.key = ""  # its hotkey (or 1-9 while SprigCast has focus), shown in a little chip on the right
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        font = self.font()
        font.setPixelSize(13)
        font.setWeight(font.Weight.DemiBold)
        self.setFont(font)
        self.setFixedHeight(self.HEIGHT)
        self.wrap = False

    def _lines(self, width: float) -> list[str]:
        """The name in at most two lines for `width` (the second ends in "…" if it still doesn't fit)."""
        metrics = QFontMetrics(self.font())
        text = self.text()
        if metrics.horizontalAdvance(text) <= width or not self.wrap:
            return [text]
        words = text.split()
        first = ""
        for i, word in enumerate(words):
            trial = f"{first} {word}".strip()
            if metrics.horizontalAdvance(trial) > width:
                rest = " ".join(words[i:])
                break
            first = trial
        else:
            return [text]
        if not first:  # one long word: break it where it runs out of room
            cut = len(text)
            while cut > 1 and metrics.horizontalAdvance(text[:cut]) > width:
                cut -= 1
            first, rest = text[:cut], text[cut:]
        return [first, metrics.elidedText(rest, Qt.TextElideMode.ElideRight, int(width))]

    def _text_width(self) -> float:
        return self.width() - 46 - self._key_width()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.wrap:
            height = self.TALL if len(self._lines(self._text_width())) > 1 else self.HEIGHT
            if height != self.height():
                self.setFixedHeight(height)

    def sizeHint(self) -> QSize:  # noqa: N802
        text = QFontMetrics(self.font()).horizontalAdvance(self.text())
        return QSize(min(240, text + 50 + self._key_width()), self.HEIGHT)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(60, self.HEIGHT)

    def set_thumb(self, image: QImage | None) -> None:
        self.thumb = image

    def set_key(self, key: str) -> None:
        self.key = key
        self.updateGeometry()
        self.update()

    def _key_width(self) -> int:
        if not self.key or 0 < self.width() < 150:
            return 0  # a narrow tile gives its name the room: the key still works, and its tooltip says it
        if getattr(self, "_key_for", None) != self.key:  # measured once per key (it was on every paint)
            small = QFont(self.font())
            small.setPixelSize(11)
            self._key_for, self._key_w = self.key, QFontMetrics(small).horizontalAdvance(self.key) + 14 + 6
        return self._key_w

    def _small_font(self) -> QFont:
        small = getattr(self, "_small", None)
        if small is None:
            small = self._small = QFont(self.font())
            small.setPixelSize(11)
        return small

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == event.Type.FontChange:
            self._key_for = self._shown_for = self._small = None  # measure again in the new font
        super().changeEvent(event)

    def _shown_text(self, width: float) -> str:
        """The name as drawn in `width`: on one or two lines, ending in "…" if it doesn't fit. Worked out once per
        name, width and key: every tile measured its text on every paint, and a pop-up opening over the window
        repaints them all (309 ms with the Settings gear while live on the PC, 2026-11-18)."""
        key = (self.text(), int(width), self.wrap)
        if getattr(self, "_shown_for", None) != key:
            lines = self._lines(width)
            if len(lines) == 1:
                lines = [QFontMetrics(self.font()).elidedText(lines[0], Qt.TextElideMode.ElideRight, int(width))]
            self._shown_for, self._shown = key, "\n".join(lines)
        return self._shown

    def enterEvent(self, event) -> None:  # noqa: N802
        super().enterEvent(event)
        if self.thumb is not None and not self.thumb.isNull():
            ThumbPop.show_for(self, self.thumb)

    def leaveEvent(self, event) -> None:  # noqa: N802
        ThumbPop.hide_now()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        ThumbPop.hide_now()
        super().hideEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = min(outer.height(), self.HEIGHT - 2) / 2  # a two-line tile keeps the pill's round ends
        hover = self.underMouse() and self.isEnabled()
        theme.paint_button(p, outer, radius, hover)
        p.setPen(Qt.PenStyle.NoPen)
        on_air = self.isChecked()
        dot = QColor(theme.LIVE if on_air else (self.colour or theme.LEAF_OFF))
        if not on_air and self.colour:
            dot = dot.lighter(260)  # folder colours arrive darkened for the old picture tiles
        p.setBrush(dot)
        p.drawEllipse(QRectF(16, outer.center().y() - 4, 8, 8))
        p.setPen(QColor(theme.TEXT_BRIGHT if self.isEnabled() else theme.MUTED))
        p.setFont(self.font())
        key_w = self._key_width()
        if key_w:
            small = self._small_font()
            chip = QRectF(outer.right() - 10 - (key_w - 6), outer.center().y() - 10, key_w - 6, 20)
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.CHROME))
            p.drawRoundedRect(chip, 10, 10)
            p.setPen(QColor(theme.MUTED))
            p.setFont(small)
            p.drawText(chip, Qt.AlignmentFlag.AlignCenter, self.key)
            p.restore()
        text_rect = QRectF(32, 0, self._text_width(), self.height())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self._shown_text(text_rect.width()))
        if on_air:
            p.setPen(QPen(QColor(theme.LIVE), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(outer.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        p.end()


class ThumbPop(QLabel):
    """The bloom's last on-air picture, floating just above its pill while you point at it."""

    _one: "ThumbPop | None" = None
    WIDTH = 200

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    @classmethod
    def show_for(cls, tile: QWidget, image: QImage) -> None:
        if cls._one is None:
            cls._one = ThumbPop()
        pop = cls._one
        scaled = image.scaledToWidth(cls.WIDTH, Qt.TransformationMode.SmoothTransformation)
        if scaled.height() > 160:  # a vertical bloom: keep it about as tall as a horizontal one
            scaled = image.scaledToHeight(160, Qt.TransformationMode.SmoothTransformation)
        rounded = QPixmap(scaled.size())
        rounded.fill(Qt.GlobalColor.transparent)
        p = QPainter(rounded)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, scaled.width(), scaled.height()), 12, 12)
        p.setClipPath(path)
        p.drawImage(0, 0, scaled)
        p.end()
        pop.setPixmap(rounded)
        pop.resize(rounded.size())
        corner = tile.mapToGlobal(QPoint(0, 0))
        pop.move(corner.x(), corner.y() - rounded.height() - 8)
        pop.show()

    @classmethod
    def hide_now(cls) -> None:
        if cls._one is not None:
            cls._one.hide()


class LivePanel(QWidget):
    def __init__(self, on_scene: Callable[[str], None], on_state: Callable[[str, str | None], None] | None = None,
                 on_edit: Callable[[str], None] | None = None):
        super().__init__()
        self.on_scene = on_scene
        self.on_edit = on_edit  # open a scene in Build mode (right-click menu)
        self.on_project: Callable | None = None  # (menu, scene id): adds the Live options for a bloom
        self.on_state = on_state or (lambda part_id, name: None)
        self.buttons: dict[str, QPushButton] = {}
        self.state_buttons: dict[tuple[str, str | None], QPushButton] = {}  # (sprig id, state name or None)
        self.thumbs: dict[str, QImage] = {}  # scene id -> its last on-air picture, small
        self.number_order: list[str] = []  # the blooms that 1-9 switch to, in the order shown
        for n in range(1, 10):
            # Window shortcuts only (never global): a game's number keys are left alone. Typing in a
            # text box (chat) still types the number, and they do nothing while Live is hidden.
            shortcut = QShortcut(QKeySequence(str(n)), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(lambda n=n: self._number(n))
        self._signature: tuple = ()
        self.view = "both"  # which canvases Live's stage shows; the list follows it
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
        # How blooms change: a pill with the transition's name; the window fills its menu.
        self.transition_chip = QPushButton("Fade")
        self.transition_chip.setToolTip("How blooms change on air")
        self.transition_chip.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.transition_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        top.addWidget(self.transition_chip)
        top.addSpacing(12)
        root.addLayout(top)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(10)
        root.addLayout(self.rows)
        root.addStretch(1)

    def set_view(self, view: str) -> None:
        """"both": a column per canvas; a canvas id: just that canvas's blooms."""
        self.view = view
        collection = getattr(self, "_collection", None)
        if collection is not None:
            self.show_scenes(collection)

    def _number(self, n: int) -> None:
        if 0 < n <= len(self.number_order) and self.isVisible():
            self.on_scene(self.number_order[n - 1])

    def set_thumb(self, scene_id: str, image: QImage) -> None:
        self.thumbs[scene_id] = image
        tile = self.buttons.get(scene_id)
        if isinstance(tile, BloomTile):
            tile.set_thumb(image)

    def show_scenes(self, collection: SceneCollection) -> None:
        signature = (tuple((s.id, s.name, s.canvas, s.kind, s.folder, s.linked_to) for s in collection.scenes),
                     tuple((f.id, f.name, f.colour, f.live) for f in collection.folders),
                     tuple((p.id, p.name, tuple(s.name for s in p.states)) for p in collection.parts()),
                     tuple(c.id for c in collection.canvases if c.enabled), self.view,
                     tuple(sorted((collection.hotkeys or {}).items())))
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
        self.number_order = []
        sections = []
        for canvas in collection.canvases:
            if not canvas.enabled or self.view not in ("both", canvas.id):
                continue  # switched off with Settings → Show canvases, or not shown on the stage
            # Every bloom in its canvas's list, linked ones too (⇄: clicking either switches both).
            groups = [(f, shows) for f, shows in collection.live_scenes(canvas.id) if shows]
            if groups:
                sections.append((canvas, groups))
        # A list per canvas: side by side when both show, each with its shape over it.
        columns_box = QWidget()
        columns = QHBoxLayout(columns_box)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(16)
        for canvas, groups in sections:
            column = QVBoxLayout()
            column.setSpacing(6)
            if len(sections) > 1 or self.view != "both":
                title = QLabel(canvas.id.capitalize())
                title.setStyleSheet(theme.MUTED_STYLE + "; background: transparent;")
                head = QHBoxLayout()
                head.setSpacing(6)
                mark = QLabel()
                mark.setPixmap(icons.pixmap("portrait" if canvas.height > canvas.width else "landscape", theme.MUTED, 14))
                head.addWidget(mark)
                head.addWidget(title, 1)
                column.addLayout(head)
            for folder, group in groups:
                if folder is not None and len(groups) > 1:  # one Live folder needs no heading
                    heading = QLabel(f"●  {folder.name}")
                    heading.setStyleSheet(f"color: {folder.colour}; font-weight: bold; margin: 4px 0 0 4px;")
                    column.addWidget(heading)
                self._add_list(column, group, folder.colour if folder is not None else None)
            column.addStretch(1)
            columns.addLayout(column, 1)
        self.rows.addWidget(columns_box)
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
            mark = QLabel()
            mark.setPixmap(icons.pixmap("sprig", theme.ACCENT, 14))
            row.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            name = QLabel(part.name)
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

    def _add_list(self, column: QVBoxLayout, scenes, colour: str | None = None) -> None:
        """The blooms as a list, one full-width row each. The column's heading names the canvas, so a
        "(vertical)" on the end of a name and the ⇄ link mark are left off (the tooltip says it's linked)."""
        for button in self._tiles(scenes, colour, plain=True):
            # Shrink with the card (the name ends in "…"; the whole name is in the tooltip).
            button.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            button.setMinimumWidth(90)
            button.wrap = True  # a narrow card shows a long name on two lines rather than "St…"
            column.addWidget(button)

    def _add_grid(self, scenes, colour: str | None = None) -> None:
        flow_box = QWidget()
        flow = FlowLayout(flow_box, spacing=8)
        for button in self._tiles(scenes, colour):
            flow.addWidget(button)
        self.rows.addWidget(flow_box)

    def _tiles(self, scenes, colour: str | None = None, plain: bool = False) -> list:
        tiles = []
        tint = QColor(colour).darker(260).name() if colour else None
        for scene in scenes:
            if plain:
                name = scene.name
                for shape in ("vertical", "horizontal"):
                    if name.lower().endswith(f" ({shape})"):
                        name = name[: -len(shape) - 3].rstrip()
                button = BloomTile(name or scene.name, tint)
            else:
                button = BloomTile(scene.name + ("  ⇄" if scene.linked_to else ""), tint)
            if scene.linked_to:
                button.setToolTip(f"{scene.name}\nLinked: switches together with its partner on the other canvas")
            else:
                partners = [s.name for s in self._collection.scenes if s.linked_to == scene.id]
                button.setToolTip(scene.name + (f"\nAlso switches: {', '.join(partners)}" if partners else ""))
            button.set_thumb(self.thumbs.get(scene.id))
            from sprigcast.app.hotkeys import SCENE_PREFIX

            combo = (self._collection.hotkeys or {}).get(SCENE_PREFIX + scene.id, "")
            if len(self.number_order) < 9 and not scene.linked_to:  # a linked twin switches with its partner's key
                self.number_order.append(scene.id)
                combo = combo or str(len(self.number_order))
            button.set_key(combo)
            if combo:
                button.setToolTip(button.toolTip() + f"\nKey: {combo}")
            button.clicked.connect(lambda _=False, sid=scene.id: self.on_scene(sid))
            button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            button.customContextMenuRequested.connect(
                lambda pos, sid=scene.id, b=button: self._run_menu(self.scene_menu(sid), b.mapToGlobal(pos)))
            self.buttons[scene.id] = button
            tiles.append(button)
        return tiles

    @staticmethod
    def _run_menu(menu: QMenu, where) -> None:
        menu.exec(where)
        menu.deleteLater()  # a new menu each right-click: kept, each one held a window handle for good

    def scene_menu(self, scene_id: str) -> QMenu:
        """A bloom tile's right-click menu: things you do with a bloom while live (editing is in Build)."""
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        if self.on_project is not None:
            self.on_project(menu, scene_id)
        return menu

    def set_clock(self, text: str, live: bool) -> None:
        self.clock.setText(text)
        self.clock.setStyleSheet(f"color: {theme.LIVE}" if live else "")

    def set_recording(self, text: str) -> None:
        self.rec.setText(text)
