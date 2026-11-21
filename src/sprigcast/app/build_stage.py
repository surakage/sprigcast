"""Build mode's own screen: every bloom and sprig on the left, the canvas in the middle, and what's
inside the picked one on the right with the inspector under it.

    ┌ Library ──────┐ ┌ Just Chatting ──────────────────── ⇄ Link ┐ ┌ Inside ──────┐
    │ ⌕ Find…       │ │  the shape you edit            [twin]    │ │ ❧ Webcam     │
    │ Blooms     +  │ │                                          │ │ ▣ Game       │
    │  ▾ Main Scenes│ │                                          │ ├──────────────┤
    │ ❧ Sprigs   +  │ │                                          │ │ Place  X  Y  │
    └───────────────┘ └──────────────────────────────────────────┘ └──────────────┘

Scenes (blooms and sprigs) are on the left, sources on the right, so the two never mix. The big
canvas is the shape being edited; the other shape sits beside it as a small "twin" you can't edit
by accident: click it to swap. The main window builds the widgets that go in the slots.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme
from sprigcast.app.library import SPRIG_MIME
from sprigcast.app.live_stage import heading

LEFT_WIDTH = 290
RIGHT_WIDTH = 320


class BuildBar(QWidget):
    """Above the canvas: what you're editing on the left, tools (Link, Safe zones) on the right."""

    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 0, 0, 0)
        row.setSpacing(8)
        self.where = heading("", 15)
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {theme.MUTED};")
        self.mark = QLabel()  # the sprig, shown while a sprig is open
        self.mark.setPixmap(icons.pixmap("sprig", theme.ACCENT, 18))
        self.mark.hide()
        row.addWidget(self.mark)
        row.addWidget(self.where)
        row.addWidget(self.note)
        row.addStretch(1)
        self.tools = QHBoxLayout()
        self.tools.setSpacing(6)
        row.addLayout(self.tools)
        self.setFixedHeight(40)

    def show_place(self, title: str, note: str, sprig: bool = False) -> None:
        self.mark.setVisible(bool(sprig and title))
        self.where.setText(title)
        self.note.setText(note)


def _card(name: str) -> QFrame:
    """A rounded panel card, like the canvas's."""
    card = QFrame()
    card.setObjectName(name)
    card.setStyleSheet(f"#{name} {{ background: {theme.PANEL}; border: none; border-radius: 24px; }}")
    return card


def column(width: int) -> tuple[QWidget, QVBoxLayout]:
    panel = QWidget()
    panel.setFixedWidth(width)
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    return panel, layout


class BuildStage(QWidget):
    """The whole Build screen. `rail`, `card_slot`, `left` (Inside) and `right` (inspector) are filled
    by the main window."""

    def __init__(self, library: QWidget, bar: BuildBar):
        super().__init__()
        self.setObjectName("buildStage")
        self.setStyleSheet(f"#buildStage {{ background: {theme.CHROME}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        body = QHBoxLayout(self)
        body.setContentsMargins(20, 6, 20, 16)
        body.setSpacing(22)
        self.bar = bar
        # Left: every bloom and sprig.
        self.rail_panel, self.rail = column(LEFT_WIDTH)
        self.rail.addWidget(library, 3)
        body.addWidget(self.rail_panel)
        # Middle: what you're editing, then the canvas.
        middle = QVBoxLayout()
        middle.setSpacing(16)
        middle.addWidget(bar)
        bar.hide()  # Build's top bar says where you are now (the shape tabs); the list shows the bloom
        self.card = QFrame()
        self.card.setObjectName("buildCard")
        self.card.setStyleSheet(f"#buildCard {{ background: {theme.PANEL}; border: none; border-radius: 28px; }}"
                                "#buildCard QSplitter::handle { background: transparent; }")
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(14, 14, 14, 14)
        self.card_slot = card_layout
        middle.addWidget(self.card, 1)
        self.below_card = middle  # Sound's strips go under the canvas
        body.addLayout(middle, 1)
        # Right: what's inside, then the inspector for the picked thing. Drag the gap to share the room.
        self.right_panel = QSplitter(Qt.Orientation.Vertical)
        self.right_panel.setFixedWidth(RIGHT_WIDTH)
        self.right_panel.setChildrenCollapsible(False)
        self.right_panel.setHandleWidth(16)
        self.right_panel.setStyleSheet(
            "QSplitter::handle { background: transparent; }")
        inside = _card("leavesCard")
        self.left = QVBoxLayout(inside)  # "left" for history: Inside used to be the left column
        self.left.setContentsMargins(16, 14, 14, 14)
        self.left.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        title = heading("Leaves", 15)
        title.setToolTip("The sources in this bloom: camera, screen, text, sprigs...")
        top.addWidget(title)
        self.inside_note = QLabel("")
        self.inside_note.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px;")
        top.addWidget(self.inside_note, 1)
        self.add_slot = QHBoxLayout()
        top.addLayout(self.add_slot)
        self.left.addLayout(top)
        inspector = _card("inspectorCard")
        self.right = QVBoxLayout(inspector)
        self.right.setContentsMargins(16, 14, 8, 8)
        self.right.setSpacing(10)
        self.right_panel.addWidget(inside)  # the leaves on top, the picked leaf's settings below
        self.right_panel.addWidget(inspector)
        self.right_panel.setStretchFactor(0, 2)
        self.right_panel.setStretchFactor(1, 3)
        # A sound picked in the library swaps the right column for its controls (sound has no place on a canvas).
        self.right_stack = QStackedWidget()
        self.right_stack.setFixedWidth(RIGHT_WIDTH)
        self.sound_page = QWidget()
        self.sound_slot = QVBoxLayout(self.sound_page)
        self.sound_slot.setContentsMargins(0, 0, 0, 0)
        self.sound_slot.setSpacing(12)
        saved = QSettings().value("build/leaves_split2")
        if saved is not None:
            self.right_panel.restoreState(saved)  # where you last left the gap
        self.right_panel.splitterMoved.connect(
            lambda *_: QSettings().setValue("build/leaves_split2", self.right_panel.saveState()))
        self.right_stack.addWidget(self.right_panel)
        self.right_stack.addWidget(self.sound_page)
        body.addWidget(self.right_stack)


# Inside: the bloom's contents, sprigs shown open ---------------------------------------------

ROLE_NAME = Qt.ItemDataRole.UserRole + 20  # the name alone, without the [kind] tag
ROLE_TAG = Qt.ItemDataRole.UserRole + 21  # quiet text on the right: "2 hidden here", "filters", "no signal"
ROLE_SPRIG = Qt.ItemDataRole.UserRole + 22  # True for a sprig placement
ROLE_CHILDREN = Qt.ItemDataRole.UserRole + 23  # [(depth, name, is_sprig)]: what's inside a sprig
ROLE_KIND = Qt.ItemDataRole.UserRole + 24  # the source's kind, for its little icon
ROLE_SWATCH = Qt.ItemDataRole.UserRole + 25  # a colour source's colour
ROLE_LOCKED = Qt.ItemDataRole.UserRole + 26  # True when it can't be moved or picked on the canvas
ROW_H, CHILD_H, EYE_W = 34, 24, 30
ACTION_W = 26  # each of the row's icons at the end: lock, settings, remove
ACTIONS = ("lock", "settings", "remove")
ICON = 22

KIND_NAMES = {"display_capture": "screen", "window_capture": "window", "game_capture": "game",
              "slideshow": "slide show",  # the Slide show plugin
              "camera": "camera", "color": "color",
              "image": "picture", "text": "text", "browser": "web page", "chat": "chat", "audio_input": "mic",
              "audio_output": "desktop sound", "app_audio": "app sound", "scene": "sprig", "media": "video",
              "capture_card": "capture card"}


KIND_TINTS = {  # each kind its own colour, so a list reads at a glance (pink stays for Live and Recording)
    "game_capture": "#b388ff", "display_capture": "#7fb8ff", "window_capture": "#9aa7ff", "camera": "#4cc9f0",
    "chat": "#ffd23f", "browser": "#5fe0d0", "image": "#a6e36b", "slideshow": "#c8e86b", "media": "#ff9f5a",
    "text": "#dfe7e2", "audio_input": "#ffb35c", "audio_output": "#f2c46b", "app_audio": "#ffc98a",
    "capture_card": "#e08cff", "sound_file": "#ff9f5a", "scene": "#e6e0ff", "nested": "#9aa7ff", "empty": "#9fb3a9",
}


def _mix(top: str | QColor, under: str, amount: float) -> QColor:
    a, b = QColor(top), QColor(under)
    return QColor(round(a.red() * amount + b.red() * (1 - amount)), round(a.green() * amount + b.green() * (1 - amount)),
                  round(a.blue() * amount + b.blue() * (1 - amount)))


def paint_kind_icon(p: QPainter, r: QRectF, kind: str, colour: str | None = None, selected: bool = False,
                    sprig: bool = False) -> None:
    """A small drawn icon on a soft colour chip saying what a source is: screen, window, game, camera,
    picture... Each kind has its own icon and colour, so you can tell them apart whatever they're named.
    sprig: a sprig showing that kind (same look, so a webcam sprig reads like a webcam)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    tint = QColor(str(colour or "#888888")[:7]) if kind == "color" else QColor(KIND_TINTS.get(kind, "#9fb3a9"))
    under = theme.SELECT_BG if selected else theme.PANEL
    chip = _mix(tint, under, 0.17)
    glow = QLinearGradient(r.topLeft(), r.bottomLeft())
    glow.setColorAt(0.0, _mix(tint, under, 0.24))
    glow.setColorAt(1.0, chip)
    p.setPen(QPen(_mix(tint, under, 0.42), 1))
    p.setBrush(glow)
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 7, 7)
    ink = tint
    pen = QPen(ink, 1.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    c = r.center()
    cx, cy = c.x(), c.y()
    if kind == "display_capture":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5, 13, 8.5), 1.5, 1.5)
        p.drawLine(QPointF(cx - 3, cy + 5.5), QPointF(cx + 3, cy + 5.5))
    elif kind == "window_capture":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5.5, 13, 11), 1.5, 1.5)
        p.drawLine(QPointF(cx - 6.5, cy - 2), QPointF(cx + 6.5, cy - 2))
    elif kind == "game_capture":
        icons.paint_icon(p, QRectF(cx - 7.5, cy - 7.5, 15, 15), "gamepad", ink, 1.4)
    elif kind == "slideshow":  # pictures in a stack
        p.drawRoundedRect(QRectF(cx - 4.5, cy - 6.5, 11, 8.5), 1.5, 1.5)
        p.setBrush(chip)
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 3.5, 11, 9), 1.5, 1.5)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx - 4.5, cy + 3.5), QPointF(cx - 2, cy + 0.5))
        p.drawLine(QPointF(cx - 2, cy + 0.5), QPointF(cx + 2.5, cy + 3.5))
    elif kind == "camera":
        p.drawRoundedRect(QRectF(cx - 7, cy - 4.5, 10, 9), 2, 2)
        path = QPainterPath()
        path.moveTo(cx + 3, cy - 1.5)
        path.lineTo(cx + 7, cy - 4)
        path.lineTo(cx + 7, cy + 4)
        path.lineTo(cx + 3, cy + 1.5)
        p.drawPath(path)
    elif kind == "capture_card":  # a capture box: its body, a play mark, and the HDMI port on its side
        p.drawRoundedRect(QRectF(cx - 5, cy - 4.5, 12, 9), 2.5, 2.5)
        tri = QPainterPath()
        tri.moveTo(cx - 0.5, cy - 2.2)
        tri.lineTo(cx + 3, cy)
        tri.lineTo(cx - 0.5, cy + 2.2)
        tri.closeSubpath()
        p.drawPath(tri)
        p.drawLine(QPointF(cx - 8, cy - 1.5), QPointF(cx - 5, cy - 1.5))
        p.drawLine(QPointF(cx - 8, cy + 1.5), QPointF(cx - 5, cy + 1.5))
    elif kind == "color":
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(str(colour or "#888888")[:7]))
        p.drawRoundedRect(QRectF(cx - 5.5, cy - 5.5, 11, 11), 3, 3)
    elif kind == "image":
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 5.5, 13, 11), 2, 2)
        path = QPainterPath()
        path.moveTo(cx - 5, cy + 4)
        path.lineTo(cx - 1.5, cy)
        path.lineTo(cx + 1, cy + 2.5)
        path.lineTo(cx + 3, cy + 0.5)
        path.lineTo(cx + 5, cy + 4)
        p.drawPath(path)
        p.drawPoint(QPointF(cx + 2.5, cy - 2.5))
    elif kind == "text":
        p.drawLine(QPointF(cx - 5, cy - 5), QPointF(cx + 5, cy - 5))
        p.drawLine(QPointF(cx, cy - 5), QPointF(cx, cy + 5.5))
    elif kind == "chat":  # a speech bubble with a tail
        path = QPainterPath()
        path.addRoundedRect(QRectF(cx - 6.5, cy - 5.5, 13, 9), 3, 3)
        p.drawPath(path)
        p.drawLine(QPointF(cx - 3, cy + 3.5), QPointF(cx - 4.5, cy + 6.5))
        p.drawLine(QPointF(cx - 4.5, cy + 6.5), QPointF(cx, cy + 3.5))
    elif kind == "browser":
        p.drawEllipse(QRectF(cx - 6, cy - 6, 12, 12))
        p.drawEllipse(QRectF(cx - 2.5, cy - 6, 5, 12))
        p.drawLine(QPointF(cx - 6, cy), QPointF(cx + 6, cy))
    elif kind in ("audio_input", "audio_output", "app_audio"):
        if kind == "audio_input":  # a mic
            p.drawRoundedRect(QRectF(cx - 2.5, cy - 6.5, 5, 8.5), 2.5, 2.5)
            p.drawArc(QRectF(cx - 5, cy - 3, 10, 7), 180 * 16, 180 * 16)
            p.drawLine(QPointF(cx, cy + 4), QPointF(cx, cy + 6.5))
        elif kind == "audio_output":  # a speaker: everything the PC plays
            icons.paint_icon(p, QRectF(cx - 7.5, cy - 7.5, 15, 15), "speaker", ink, 1.4)
        else:  # one app's sound: level bars
            for i, h in enumerate((4, 9, 6, 11, 5)):
                x = cx - 6 + i * 3
                p.drawLine(QPointF(x, cy + h / 2), QPointF(x, cy - h / 2))
    elif kind == "sound_file":  # a sound file: a note
        icons.paint_icon(p, QRectF(cx - 7.5, cy - 7.5, 15, 15), "note", ink, 1.4)
    elif kind == "media":  # a play triangle in a frame
        p.drawRoundedRect(QRectF(cx - 7, cy - 5.5, 14, 11), 2.5, 2.5)
        tri = QPainterPath()
        tri.moveTo(cx - 2, cy - 3)
        tri.lineTo(cx + 3.5, cy)
        tri.lineTo(cx - 2, cy + 3)
        tri.closeSubpath()
        p.drawPath(tri)
    elif kind == "scene":  # a sprig mixing things: four little tiles, each a kind's own colour
        p.setPen(Qt.PenStyle.NoPen)
        for (dx, dy), tile in zip(((-6.5, -6.5), (0.5, -6.5), (-6.5, 0.5), (0.5, 0.5)),
                                  ("game_capture", "camera", "chat", "media")):
            p.setBrush(QColor(KIND_TINTS[tile]))
            p.drawRoundedRect(QRectF(cx + dx, cy + dy, 6, 6), 1.8, 1.8)
    elif kind == "empty":  # nothing in it yet: a dashed box with a bright +
        dash = QPen(ink, 1.3)
        dash.setDashPattern([2.2, 1.8])
        p.setPen(dash)
        p.drawRoundedRect(QRectF(cx - 6.5, cy - 6.5, 13, 13), 3.5, 3.5)
        plus = QPen(QColor(theme.ACCENT_HOVER), 1.8)
        plus.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(plus)
        p.drawLine(QPointF(cx - 3, cy), QPointF(cx + 3, cy))
        p.drawLine(QPointF(cx, cy - 3), QPointF(cx, cy + 3))
    elif kind == "nested":  # sprigs inside a sprig: three stacked layers, each its own colour
        def layer(y: float, fill: str) -> None:
            path = QPainterPath()
            path.moveTo(cx, y - 3.6)
            path.lineTo(cx + 7, y)
            path.lineTo(cx, y + 3.6)
            path.lineTo(cx - 7, y)
            path.closeSubpath()
            p.setPen(QPen(_mix(fill, under, 0.55), 1))
            p.setBrush(QColor(fill))
            p.drawPath(path)

        layer(cy + 3.6, KIND_TINTS["camera"])
        layer(cy, KIND_TINTS["game_capture"])
        layer(cy - 3.6, theme.ACCENT)
    else:  # plugins and anything new: a little puzzle-ish dot
        p.drawEllipse(QRectF(cx - 4, cy - 4, 8, 8))
        p.drawPoint(QPointF(cx + 5.5, cy))
    p.restore()


BADGE = 20  # one status icon's room on a row

# A Leaves row's quiet tags as icons: what needs a look in warm colours, the rest quiet.
_BADGES = {"problem": ("warning", theme.BAD), "not found": ("warning", theme.WARN), "lost": ("warning", theme.WARN),
           "closed": ("warning", theme.WARN), "busy": ("warning", theme.WARN), "unplugged": ("plug", theme.WARN),
           "waiting": ("clock", theme.MUTED), "starting": ("clock", theme.MUTED), "asleep": ("moon", theme.MUTED),
           "filters": ("sliders", theme.MUTED)}


def tag_badges(tag: str) -> list[tuple[str, str]]:
    """'2 hidden here · filters · problem' → the icons to draw for it, in that order."""
    out = []
    for word in (w.strip() for w in tag.split("·")):
        if word.endswith("hidden here"):
            out.append(("eye-off", theme.MUTED))
        elif word in _BADGES:
            out.append(_BADGES[word])
    return out


def action_rects(top: QRectF) -> list[QRectF]:
    """Where a row's icons go: evenly spaced at the right end, lock first."""
    right = top.right() - 6
    return [QRectF(right - ACTION_W * (len(ACTIONS) - i), top.y(), ACTION_W, top.height()) for i in range(len(ACTIONS))]


class InsideDelegate(QStyledItemDelegate):
    """Draws an Inside row: a visibility dot, the name (a sprig icon for sprigs), a quiet tag, then the sprig's
    own contents underneath on branch lines, like the mockup."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        children = index.data(ROLE_CHILDREN) or []
        view = self.parent()
        # As wide as the list shows, never wider: a row wider than the list scrolled sideways and cut the
        # lock, settings and remove icons off (user, 2026-11-20).
        width = view.viewport().width() if isinstance(view, QListWidget) else option.rect.width()
        return QSize(width, ROW_H + CHILD_H * len(children) + (4 if children else 0))

    def paint(self, p: QPainter, option, index) -> None:
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(0, 1, 0, -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(theme.SELECT_BG if selected else theme.PANEL))
            p.drawRoundedRect(r, 12, 12)
        top = QRectF(r.x(), r.y(), r.width(), ROW_H - 2)
        shown = index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value or \
            index.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        eye = QRectF(top.x() + 10, top.center().y() - 5, 10, 10)
        p.setPen(QPen(QColor(theme.ACCENT if shown else theme.MUTED), 1.5))
        p.setBrush(QColor(theme.ACCENT) if shown else Qt.BrushStyle.NoBrush)
        p.drawEllipse(eye)
        name = index.data(ROLE_NAME) or index.data(Qt.ItemDataRole.DisplayRole) or ""
        sprig = bool(index.data(ROLE_SPRIG))
        x = top.x() + EYE_W
        font = QFont(option.font)
        font.setWeight(QFont.Weight.DemiBold if sprig or selected else QFont.Weight.Normal)
        p.setFont(font)
        metrics = QFontMetrics(font)
        kind = index.data(ROLE_KIND) or ("scene" if sprig else "")
        if kind:
            paint_kind_icon(p, QRectF(x, top.center().y() - ICON / 2, ICON, ICON), kind, index.data(ROLE_SWATCH),
                            selected, sprig=sprig)
            p.setFont(font)
            x += ICON + 10
        locked = bool(index.data(ROLE_LOCKED))
        lock_w = ACTION_W * len(ACTIONS) + 4
        tools = hover or selected  # settings and remove only show when you point at (or pick) the row
        for action, slot in zip(ACTIONS, action_rects(top)):
            box = QRectF(slot.center().x() - 7, slot.center().y() - 7, 14, 14)
            if action == "lock" and (locked or tools):
                icons.paint_icon(p, box, "lock" if locked else "unlock", theme.ACCENT if locked else theme.MUTED)
            elif action == "settings" and tools:
                icons.paint_icon(p, box, "gear", theme.MUTED)
            elif action == "remove" and tools:
                icons.paint_icon(p, box, "trash", theme.MUTED)
        p.setFont(font)
        badges = tag_badges(index.data(ROLE_TAG) or "")  # small icons, not words (the tooltip says it)
        tag_w = BADGE * len(badges) + 6 if badges else 0
        colour = theme.SELECT_TEXT if selected else theme.TEXT if shown else theme.MUTED
        p.setPen(QColor(colour))
        width = int(top.right() - lock_w - x - tag_w - 8)
        p.drawText(QRectF(x, top.y(), width, top.height()), Qt.AlignmentFlag.AlignVCenter,
                   metrics.elidedText(name, Qt.TextElideMode.ElideRight, width))
        bx = top.right() - lock_w - tag_w + 2
        for icon, tint in badges:
            icons.paint_icon(p, QRectF(bx + 1, top.center().y() - 7, 14, 14), icon, tint, 1.5)
            bx += BADGE
        children = index.data(ROLE_CHILDREN) or []
        y = top.bottom()
        p.setFont(QFont(option.font))
        for depth, child, child_sprig, *look in children:
            indent = top.x() + 24 + depth * 16
            p.setPen(QPen(QColor(theme.EDGE_STRONG), 2))
            p.drawLine(int(indent), int(y), int(indent), int(y + CHILD_H))
            tx = indent + 12
            child_kind = look[0] if look else ("scene" if child_sprig else "")
            if child_kind:  # what each thing inside is, at a glance
                paint_kind_icon(p, QRectF(tx, y + CHILD_H / 2 - 9, 18, 18), child_kind, look[1] if len(look) > 1 else None,
                                selected, sprig=child_sprig)
                p.setFont(QFont(option.font))
                tx += 24
            p.setPen(QColor(theme.MUTED if not selected else theme.SELECT_TEXT))
            p.drawText(QRectF(tx, y, top.right() - tx - 6, CHILD_H), Qt.AlignmentFlag.AlignVCenter, child)
            y += CHILD_H
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        """A click on the dot shows or hides the thing (like the old checkbox)."""
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            if pos.y() - option.rect.y() < ROW_H:
                top = QRectF(option.rect).adjusted(0, 1, 0, -1)
                for action, slot in zip(ACTIONS, action_rects(QRectF(top.x(), top.y(), top.width(), ROW_H - 2))):
                    if slot.contains(pos):
                        self.parent().action_clicked.emit(index.row(), action)
                        return True
            if pos.x() - option.rect.x() < EYE_W and pos.y() - option.rect.y() < ROW_H:
                state = index.data(Qt.ItemDataRole.CheckStateRole)
                on = state in (Qt.CheckState.Checked, Qt.CheckState.Checked.value)
                model.setData(index, Qt.CheckState.Unchecked if on else Qt.CheckState.Checked,
                              Qt.ItemDataRole.CheckStateRole)
                return True
        return False


class InsideList(QListWidget):
    """The Inside list: drag a row to put it in front of or behind the others."""

    moved = Signal(int, int)  # from row, to row (rows are top-most first)
    sprig_dropped = Signal(str)  # a sprig dragged in from the library: its part id
    action_clicked = Signal(int, str)  # an icon at the end of a row: row (top-most first), "lock"/"settings"/"remove"

    def __init__(self):
        super().__init__()
        self.setItemDelegate(InsideDelegate(self))
        self.setMouseTracking(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # everything fits: no sideways
        self.setResizeMode(QListView.ResizeMode.Adjust)  # rows follow the list's width
        self.setSpacing(1)

    def event(self, event) -> bool:  # noqa: D102
        handled = theme.view_activation(self, event)  # no repaint when a pop-up takes the focus
        return super().event(event) if handled is None else handled

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)
        if event.source() is self:
            event.accept()

    def _slot_at(self, y: float) -> int:
        """Where a row dragged to this height goes: before the row under it (its top half) or after it.
        Worked out here, not by Qt: a sprig's row is tall (its contents listed under it), and Qt only lets a
        drop land in a few pixels at its very top or bottom, so most drops did nothing (PC, 2026-11-20)."""
        for row in range(self.count()):
            rect = self.visualItemRect(self.item(row))
            if y < rect.center().y():
                return row
        return self.count()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.acceptProposedAction()
            return
        if event.source() is not self:
            super().dragMoveEvent(event)
            return
        slot = self._slot_at(event.position().y())
        if slot != getattr(self, "_drop_slot", None):
            self._drop_slot = slot
            self.viewport().update()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_slot = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        slot = getattr(self, "_drop_slot", None)
        if slot is None or not self.count():
            return
        if slot < self.count():
            y = self.visualItemRect(self.item(slot)).top()
        else:
            y = self.visualItemRect(self.item(self.count() - 1)).bottom()
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(theme.ACCENT), 2))
        p.drawLine(QPointF(8, y), QPointF(self.viewport().width() - 8, y))
        p.end()

    def dropEvent(self, event) -> None:  # noqa: N802
        """Work out where the row was dropped and let the window reorder the bloom (it rebuilds the list)."""
        self._drop_slot = None
        self.viewport().update()
        if event.mimeData().hasFormat(SPRIG_MIME):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.sprig_dropped.emit(bytes(event.mimeData().data(SPRIG_MIME)).decode())
            return
        source = self.currentRow()
        slot = self._slot_at(event.position().y())
        target = slot - 1 if slot > source else slot  # the slot counts the row being moved
        event.setDropAction(Qt.DropAction.IgnoreAction)
        event.accept()
        if source >= 0 and target != source:
            self.moved.emit(source, max(0, min(self.count() - 1, target)))


def kind_icon(kind: str, size: int = 20) -> QIcon:
    """The Leaves list's chip for a kind as an icon, for menus (+ Add), sharp on high-DPI screens."""
    from PySide6.QtGui import QPixmap

    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    paint_kind_icon(p, QRectF(0, 0, size, size), kind)
    p.end()
    return QIcon(pix)
