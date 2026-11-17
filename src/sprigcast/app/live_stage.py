"""Live mode's own screen: the stage with Chat beside it, and a row of cards under both.

    header: … ● 1:56:14 · 2 of 2 sending   [Garden check]  [Record]  [Go live]
    ┌ stage ─────────────────────────────────────┐ ┌ Chat ───────┐
    │ horizontal program   vertical program      │ │             │
    │ [Both|Horizontal|Vertical]  [Clip|Screenshot|Virtual Cam] │
    └────────────────────────────────────────────┘ └─────────────┘
    [Blooms] [Sound] [Transitions]                  [Destinations]

No docks: the main window moves its canvas previews and the bloom list in here when Live mode starts,
and back when Build mode starts. Panels you pick in the Panels menu (chat, activity) fill the column
beside the stage (app/panels.py). The header widgets (header_box) and the bar under the canvases
(stage_bar) belong to LiveSide; the main window and LiveStage place them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme

SIDE_WIDTH = 360  # Chat beside the canvases, Destinations under it
BOTTOM_HEIGHT = 300  # the Blooms | Sound | Transitions row under the canvas
GAP = 20


class Switch(QCheckBox):
    """A small on/off switch: a pill with a round seed for a knob that sprouts, green, when on."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(30, 16)

    def hitButton(self, pos) -> bool:  # noqa: N802 (Qt naming): the whole pill is clickable
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        on = self.isChecked()
        track = QColor(theme.ACCENT if on else theme.EDGE_STRONG)
        if not self.isEnabled():
            track.setAlpha(110)
        p.setBrush(track)
        p.drawRoundedRect(QRectF(0, 0, 30, 16), 8, 8)
        # The knob is a round seed, like the radio choices: plain when off, sprouting when on.
        knob = QRectF(16 if on else 2, 2, 12, 12)
        p.setBrush(QColor(theme.TEXT_BRIGHT if on else theme.MUTED))
        p.drawEllipse(knob)
        if on:
            theme.paint_sprout(p, knob, theme.ACCENT_DARK)
        p.end()


def heading(text: str, size: int = 14) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setPixelSize(size + 2)
    font.setWeight(font.Weight.DemiBold)
    label.setFont(font)
    label.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
    return label


def pill(text: str, style: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setStyleSheet(style or theme.PILL_STYLE)
    return button


# The header's Record and Go live: shorter than the old card's big button, still round (radius = half).
HEADER_PILL_HEIGHT = 40
HEADER_RECORD_STYLE = theme.button_style(20, "0 20px")
HEADER_RECORDING_STYLE = HEADER_RECORD_STYLE + (
    f"QPushButton {{ background: {theme.LIVE}; color: #ffffff; border-color: {theme.LIVE_EDGE}; }}"
    f" QPushButton:hover {{ background: {theme.LIVE_HOVER}; border-color: {theme.LIVE_HOVER}; }}"
)
HEADER_GO_STYLE = (
    f"QPushButton {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; border: none; border-radius: 20px;"
    f" padding: 0 26px; font-size: 15px; font-weight: 700; }}"
    f"QPushButton:hover {{ background: {theme.ACCENT_HOVER}; }}"
    f"QPushButton:disabled {{ background: transparent; color: {theme.MUTED}; border: 1px solid {theme.BUTTON_OFF_EDGE}; }}"
)
HEADER_END_STYLE = HEADER_GO_STYLE + (
    f"QPushButton {{ background: {theme.LIVE}; color: #ffffff; }} QPushButton:hover {{ background: {theme.LIVE_HOVER}; }}"
)

VIEWS = (  # (key, label, icon, tooltip): which canvases Live's stage shows
    ("both", "Both", "dual", "Show both canvases"),
    ("horizontal", "Horizontal", "landscape", "Show only the horizontal canvas, bigger"),
    ("vertical", "Vertical", "portrait", "Show only the vertical canvas, bigger"),
)


def _dot_icon(colour: str, size: int = 10) -> QIcon:
    pix = QPixmap(size * 2, size * 2)
    pix.setDevicePixelRatio(2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    p.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    p.end()
    return QIcon(pix)


BAR_HEIGHT = 32  # radius 16: round ends
BAR_BUTTON_STYLE = (
    # The height lives here: the app's QPushButton min-height would otherwise win over setFixedHeight,
    # squash the pills to 18 px and turn their round ends square.
    f"QPushButton {{ background: transparent; color: {theme.TEXT}; border: none; border-radius: 16px;"
    f" padding: 0 14px; font-weight: 600; min-height: {BAR_HEIGHT}px; max-height: {BAR_HEIGHT}px; }}"
    f"QPushButton:hover {{ background: {theme.RAISED_HOVER}; }}"
    f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}"
)
# Clip, Screenshot, Virtual Cam and its gear are actions, not a pick-one switch: each gets the button shape.
BAR_ACTION_STYLE = theme.button_style(
    16, "0 14px", extra=f"min-height: {BAR_HEIGHT - 2}px; max-height: {BAR_HEIGHT - 2}px;")


class BarGroup(QFrame):
    """A soft pill holding a few quiet buttons (icon and a word), like a segmented control."""

    def __init__(self):
        super().__init__()
        self.setObjectName("barGroup")
        self.setStyleSheet(f"#barGroup {{ background: {theme.RAISED}; border-radius: 20px; }}")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(4, 4, 4, 4)
        self.row.setSpacing(2)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)

    def add(self, text: str, icon_name: str, tip: str, checkable: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setIcon(icons.icon(icon_name, theme.MUTED, 16))
        button.setIconSize(QSize(16, 16))
        style = BAR_BUTTON_STYLE if checkable else BAR_ACTION_STYLE
        button.setStyleSheet(style)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFixedHeight(BAR_HEIGHT)
        if not text:
            button.setFixedWidth(BAR_HEIGHT)  # a round icon button at the end of the pill
            button.setStyleSheet(style + "QPushButton { padding: 0; }")
        button.setToolTip(tip)
        button.setCheckable(True)  # the camera and clips show on/off; the view buttons pick one
        if not checkable:
            button.setCheckable(text in ("Clip", "Virtual Cam"))
        self.row.addWidget(button)
        return button


class Elided(QLabel):
    """A one-line label that ends in "…" when it hasn't room (the whole text is in its tooltip)."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(30)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802
        self._full = text
        self.setToolTip(text)
        self._fit()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(10, self.width()))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())


class GardenCheck(QPushButton):
    """The header's health pill: a leaf, "Garden check", the CPU, memory and dropped frames, and a few
    small bars of recent CPU. Click it for every number and the last stream's health."""

    def __init__(self):
        super().__init__()
        self.setObjectName("gardenCheck")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(HEADER_PILL_HEIGHT)
        self.setStyleSheet(f"#gardenCheck {{ background: {theme.BUTTON}; border: 1px solid {theme.BUTTON_EDGE};"
                           f" border-top-color: {theme.BUTTON_TOP}; border-radius: 20px; }}"
                           f"#gardenCheck:hover {{ background: {theme.BUTTON_HOVER}; border-color: {theme.BUTTON_EDGE_HOVER}; }}")
        self.setToolTip("Garden check: how SprigCast and your streams are doing. Click for everything.")
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(8)
        leaf = QLabel()
        leaf.setPixmap(icons.pixmap("leaf", theme.ACCENT, 16))
        self.title = QLabel("Garden check")
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 600; background: transparent;")
        self.numbers = QLabel("")
        self.numbers.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.bars = _Bars()
        for w in (leaf, self.title, self.numbers, self.bars):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            row.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
        self.level = "good"

    def set_numbers(self, text: str, level: str = "good", cpu: float | None = None) -> None:
        """text: "CPU 4% · 209 MB · 0 dropped"; level: good, warning or bad (colours the bars)."""
        self.numbers.setText(text)
        self.level = level
        if cpu is not None:
            self.bars.push(cpu)
        self.bars.colour = {"warning": theme.WARN, "bad": theme.BAD}.get(level, theme.ACCENT)
        self.bars.update()
        self.setMinimumWidth(self.layout().sizeHint().width())  # the header never squeezes the numbers
        self.updateGeometry()
        tip = self.toolTip().split("\n")[0]
        self.setToolTip(tip if self.numbers.isVisible() else f"{tip}\n{text}")

    def set_compact(self, step: int) -> None:
        """-1 or 0: everything; 1: no numbers (they're in the tooltip); 2: just the leaf and the bars."""
        self.numbers.setVisible(step < 1)
        self.title.setVisible(step < 2)
        self.setMinimumWidth(0)
        self.setMinimumWidth(self.layout().sizeHint().width())
        self.updateGeometry()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self.layout().sizeHint().width(), HEADER_PILL_HEIGHT)


class _Bars(QWidget):
    """Five tiny bars: the last few CPU readings, so a climb shows at a glance."""

    def __init__(self):
        super().__init__()
        self.values: list[float] = []
        self.colour = theme.ACCENT
        self.setFixedSize(30, 14)

    def push(self, value: float) -> None:
        self.values = (self.values + [max(0.0, value)])[-5:]

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self.colour))
        if not self.values:
            values = [0.45, 0.7, 0.55, 0.9, 0.65]  # before the first reading: a calm, even look
            top = 1.0
        else:
            values = ([self.values[0]] * 5 + self.values)[-5:]
            # Against the recent highest (at least 5 %), so a small change still shows as bars, not dots.
            top = max(5.0, *values) * 1.1
        for i, v in enumerate(values):
            h = max(4.0, 14 * v / top)
            p.drawRoundedRect(QRectF(i * 6.5, 14 - h, 3.5, h), 1.5, 1.5)
        p.end()

class ThinMeter(QWidget):
    """A 6 px rounded level bar: green, yellow when loud, pink when it's about to clip."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.level = 0.0  # 0..1 of -60..0 dBFS
        self.muted = False
        self.colour: str | None = None  # one calm colour (a sound group's) instead of green/yellow
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_db(self, rms: float) -> None:
        target = (min(0.0, max(-60.0, rms)) + 60.0) / 60.0
        level = target if target > self.level else max(target, self.level - 0.02)  # falls back slowly
        if abs(level - self.level) > 0.002:
            self.level = level
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        p.setBrush(QColor(theme.TRACK))
        p.drawRoundedRect(r, 3, 3)
        if self.level > 0 and not self.muted:
            if self.colour:
                colour = self.colour if self.level < 0.85 else theme.LIVE  # pink only near clipping
            else:
                colour = theme.ACCENT if self.level < 2 / 3 else theme.WARN if self.level < 0.85 else theme.LIVE
            p.setBrush(QColor(colour))
            p.drawRoundedRect(QRectF(0, 0, max(6.0, r.width() * self.level), r.height()), 3, 3)
        p.end()


@dataclass(frozen=True)
class Destination:
    id: str
    name: str
    on: bool
    note: str  # "Ready", "Needs a stream key", or the live bitrate
    canvas: str = "horizontal"
    test: bool | None = None  # Twitch bandwidth test on/off; None where the service has none
    service: str = ""  # twitch, youtube, kick, tiktok, x or custom: picks the little tile


# Each place's tile: two letters on the platform's own colour.
PLATFORM_TILES = {
    "twitch": ("Tw", "#9146ff", "#ffffff"), "youtube": ("Yt", "#ff2d48", "#ffffff"),
    "kick": ("K", "#53fc18", "#0b0f0d"), "tiktok": ("Tt", "#0b0f0d", "#ffffff"),
    "x": ("X", "#0b0f0d", "#ffffff"),
}


def _sentence(note: str) -> str:
    """Each part of a tile's line starts with a capital: "Horizontal · Ready" (the user's call, 2026-11-17)."""
    return note[:1].upper() + note[1:]


SERVICE_CHOICES = (("twitch", "Twitch"), ("youtube", "YouTube"), ("kick", "Kick"), ("tiktok", "TikTok"),
                   ("x", "X"), ("custom", "Custom RTMP"))
CHIP_STYLE = theme.button_style(10, "2px 10px", extra="min-height: 16px; font-size: 11px;")
REMOVE_CHIP_STYLE = CHIP_STYLE + f"QPushButton {{ color: {theme.BAD}; }}"


def side_card() -> QFrame:
    """A soft rounded card for a side-column section, padded so nothing touches its edges."""
    card = QFrame()
    card.setObjectName("sideCard")
    card.setStyleSheet(f"#sideCard {{ background: {theme.PANEL}; border: none; border-radius: 22px; }}")
    layout = QVBoxLayout(card)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(14)
    return card


PLACES_HEIGHT = 4 * 44 + 3 * 8  # four destinations; more scroll inside the card


class FitScroll(QScrollArea):
    """Scrolls its widget only when the column is too short: as tall as the content when there is room,
    down to SHORTEST when there isn't. Never scrolls sideways."""

    SHORTEST = 150

    def __init__(self, widget: QWidget, cap: int | None = None):
        super().__init__()
        self.cap = cap  # never taller than this either: past it, the content scrolls
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.setWidget(widget)
        widget.installEventFilter(self)
        self._fit()

    def _fit(self) -> None:
        height = self.widget().sizeHint().height()
        if self.cap is not None:
            height = min(height, self.cap)
        self.setMaximumHeight(max(1, height))  # never taller than the content
        self.updateGeometry()
        self._bar()

    def _bar(self) -> None:
        # The bar only when the rows really don't fit, so it never sits over them for a moment.
        fits = self.height() >= self.widget().sizeHint().height()
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff if fits
                                        else Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._bar()
        super().resizeEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched is self.widget() and event.type() == QEvent.Type.LayoutRequest:
            self._fit()  # rows came or went: ask the column for a new height
        return super().eventFilter(watched, event)

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = self.widget().sizeHint()
        return QSize(hint.width(), hint.height() if self.cap is None else min(hint.height(), self.cap))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        shortest = self.SHORTEST if self.cap is None else 40  # a capped list may shrink and scroll
        return QSize(0, min(shortest, self.widget().sizeHint().height()))


class LiveSide(QWidget):
    """The right-hand column: the big stream button, Record and Save clip, where it goes, and sound."""

    def __init__(self, on_live: Callable[[], None], on_record: Callable[[], None], on_clip: Callable[[], None],
                 on_destination: Callable[[str | None], None],
                 on_toggle: Callable[[str, bool], None] | None = None,
                 on_place: Callable[[str, str], None] | None = None,
                 on_settings: Callable[[], None] | None = None,  # unused: Settings is the header's gear
                 on_sound: Callable[[str, dict], None] | None = None,
                 on_sound_filters: Callable[[str], None] | None = None,
                 on_mic_first: Callable[[bool], None] | None = None,
                 on_camera: Callable[[], None] | None = None,
                 on_camera_settings: Callable[[], None] | None = None):
        """on_destination(id): open Settings on a place (None: the first, "new:<service>": add one).
        on_place(id, action): Edit mode's quick changes: "remove", "shape", "test"."""
        super().__init__()
        self.on_destination = on_destination
        self.on_toggle = on_toggle or (lambda _id, _on: None)
        self.on_place = on_place or (lambda _id, _action: None)
        self.is_live = False
        self.editing = False
        self._places: list[Destination] = []
        self._levels: dict[str, str] = {}
        self.setFixedWidth(SIDE_WIDTH)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(GAP)
        # Chat (and any other Live panels) on top, as tall as the canvas; the Go live card at the bottom,
        # level with Blooms, Sound and Transitions (LiveStage sizes it with them).
        self.panel_slot = QVBoxLayout()
        self.panel_slot.setContentsMargins(0, 0, 0, 0)
        root.addLayout(self.panel_slot, 1)

        # The Destinations card (bottom right). Go live, Record and the Garden check sit in the window's
        # header (header_box); Clip, Screenshot and Virtual Cam in a small bar under the canvases (stage_bar).
        where_card = side_card()
        where = where_card.layout()
        where.setSpacing(10)
        self.header_box = QWidget()
        head = QHBoxLayout(self.header_box)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(12)
        self.garden = GardenCheck()
        head.addWidget(self.garden)
        self.record = pill("Record", HEADER_RECORD_STYLE)
        self.record.setIcon(_dot_icon(theme.MUTED))
        self.record.setToolTip("Record the canvases ticked in Controls, to your recordings folder")
        self.record.clicked.connect(on_record)
        head.addWidget(self.record)
        # Shown only while recording: pause and carry on in the same file.
        self.on_pause: Callable[[], None] = lambda: None
        self.pause = pill("Pause", HEADER_RECORD_STYLE)
        self.pause.setToolTip("Pause the recording. Resume carries on in the same file.")
        self.pause.clicked.connect(lambda: self.on_pause())
        self.pause.setFixedHeight(HEADER_PILL_HEIGHT)
        self.pause.hide()
        head.addWidget(self.pause)
        self.live = pill("Go live", HEADER_GO_STYLE)
        self.live.setMinimumWidth(150)  # "End stream" fits, and Go live doesn't jump when it changes
        self.live.clicked.connect(on_live)
        head.addWidget(self.live)
        for w in (self.record, self.live):
            w.setFixedHeight(HEADER_PILL_HEIGHT)

        # Under the canvases: which canvases to show, then Clip, Screenshot and Virtual Cam.
        self.stage_bar = QWidget()
        bar_box = QVBoxLayout(self.stage_bar)
        bar_box.setContentsMargins(0, 0, 0, 0)
        bar_box.setSpacing(6)
        bar = QHBoxLayout()
        bar.setSpacing(12)
        bar_box.addLayout(bar)
        self.on_view: Callable[[str], None] = lambda _view: None
        self.view_group = BarGroup()
        self.view_buttons: dict[str, QPushButton] = {}
        for key, label, icon_name, tip in VIEWS:
            button = self.view_group.add(label, icon_name, tip, checkable=True)
            button.clicked.connect(lambda _=False, k=key: self._view_clicked(k))
            self.view_buttons[key] = button
        bar.addWidget(self.view_group)
        self.tools_group = BarGroup()
        self.clip = self.tools_group.add("Clip", "clip", "")
        self.clip.clicked.connect(on_clip)
        self._clip_on, self._clip_seconds = False, 30
        self.on_screenshot: Callable[[], None] = lambda: None
        self.screenshot = self.tools_group.add("Screenshot", "shutter", "Screenshot every canvas, to your Screenshots folder")
        self.screenshot.clicked.connect(lambda: self.on_screenshot())
        self.camera = self.tools_group.add("Virtual Cam", "camera", "")
        if on_camera is not None:
            self.camera.clicked.connect(on_camera)
        self.camera_settings = self.tools_group.add("", "gear", "Virtual camera settings: what it shows")
        if on_camera_settings is not None:
            self.camera_settings.clicked.connect(on_camera_settings)
        for w in (self.camera, self.camera_settings):
            w.setVisible(on_camera is not None)
        bar.addWidget(self.tools_group)
        self.camera_note = QLabel("")
        self.camera_note.setWordWrap(True)
        self.camera_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.camera_note.setStyleSheet(f"color: {theme.WARN}; background: transparent;")
        self.camera_note.setVisible(False)
        self.camera_note.setOpenExternalLinks(True)  # "Open settings" for Windows camera privacy
        bar_box.addWidget(self.camera_note)
        self.set_camera(False)
        self.set_clip(False, 30)
        top = QHBoxLayout()
        top.addWidget(heading("Destinations"), 1)
        self.add_place = QPushButton("+ Add")
        self.add_place.setStyleSheet(theme.LINK_STYLE)
        self.add_place.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_place.setToolTip("Add another place to stream")
        self.add_menu = QMenu(self)
        for key, label in SERVICE_CHOICES:
            self.add_menu.addAction(label).triggered.connect(lambda _=False, k=key: on_destination(f"new:{k}"))
        self.add_place.clicked.connect(self._show_add_menu)
        top.addWidget(self.add_place)
        self.manage = QPushButton("Edit")
        self.manage.setStyleSheet(theme.LINK_STYLE)
        self.manage.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manage.setToolTip("Switch places on or off, remove them, or change their shape, right here")
        self.manage.clicked.connect(lambda: self.set_editing(not self.editing))
        top.addWidget(self.manage)
        where.addLayout(top)
        places_box = QWidget()
        self.places = QVBoxLayout(places_box)
        self.places.setContentsMargins(0, 0, 0, 0)
        self.places.setSpacing(8)
        self.places_scroll = FitScroll(places_box, cap=PLACES_HEIGHT)
        where.addWidget(self.places_scroll)
        self.all_settings = QPushButton("All stream settings…")
        self.all_settings.setStyleSheet(theme.LINK_STYLE)
        self.all_settings.setCursor(Qt.CursorShape.PointingHandCursor)
        self.all_settings.clicked.connect(lambda: on_destination(None))
        self.all_settings.setVisible(False)
        where.addWidget(self.all_settings, 0, Qt.AlignmentFlag.AlignLeft)
        self.on_health: Callable[[], None] = lambda: None  # the Garden check opens the last stream's health
        where.addStretch(1)
        # How the last stream went, under the places: a click opens Stream health.
        self.last_stream = QPushButton("")
        self.last_stream.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {theme.MUTED}; text-align: left;"
            f" padding: 2px 0; }} QPushButton:hover {{ color: {theme.TEXT}; }}")
        self.last_stream.setCursor(Qt.CursorShape.PointingHandCursor)
        self.last_stream.setToolTip("Stream health: how your last streams went")
        self.last_stream.clicked.connect(lambda: self.on_health())
        self.last_stream.setVisible(False)
        where.addWidget(self.last_stream, 0, Qt.AlignmentFlag.AlignLeft)  # a short list sits at the top, its title level with Blooms, Sound and Transitions
        self.where_card = where_card  # LiveStage puts it at the end of the row under the canvas
        self.place_rows: dict[str, tuple[QLabel, QLabel, QLabel, QCheckBox]] = {}
        self.place_tools: dict[str, dict[str, QPushButton]] = {}
        self._shape_key: tuple | None = None

        from sprigcast.app.sound_card import SoundCard

        sound_card = side_card()
        self.sound = SoundCard(on_sound or (lambda _id, _changes: None), on_sound_filters or (lambda _id: None),
                               on_mic_first)
        # A long sound list (Edit shows all of them) scrolls inside its card instead of pushing the
        # panels below (Chat, Transitions) off the bottom of the window.
        # A short list sits at the top, its title level with Blooms and Transitions; a long one fills the card.
        sound_card.layout().addWidget(FitScroll(self.sound), 1, Qt.AlignmentFlag.AlignTop)
        self.sound_card = sound_card  # LiveStage shows it under the canvas, beside Blooms and Transitions

        # Panels you choose for Live (chat, activity feed...) go in panel_slot, above (app/panels.py).
        self.stats = QLabel("")
        self.stats.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self.stats.setWordWrap(True)
        self.stats.hide()  # the stats strip along the bottom of the window shows these now
        root.addWidget(self.stats)

    # The buttons ------------------------------------------------------------------------

    def set_health_link(self, shown: bool) -> None:
        """Kept for older callers: the last stream's health is in the Garden check now."""
        self._has_health = shown

    def set_last_stream(self, record) -> None:
        """ "Last stream: smooth", or "bumpy · 12 drops" (app/health.HealthRecord; None hides it)."""
        if record is None:
            self.last_stream.setVisible(False)
            return
        level, _line = record.verdict()
        text = "Last stream: smooth" if level == "good" else "Last stream: bumpy"
        if record.dropped:
            text += f"  ·  {record.dropped} drop" + ("" if record.dropped == 1 else "s")
        dot = {"good": theme.ACCENT, "warning": theme.WARN}.get(level, theme.BAD)
        self.last_stream.setIcon(_dot_icon(dot, 8))
        self.last_stream.setText(" " + text)
        self.last_stream.setVisible(True)

    def set_live(self, live: bool) -> None:
        self.is_live = live
        self.add_place.setEnabled(not live)
        if live and self.editing:
            self.set_editing(False)
        self.manage.setEnabled(not live)
        for *_, switch in self.place_rows.values():
            switch.setEnabled(not live)  # a place joins or leaves only between streams
        self.live.setText("End stream" if live else "Go live")
        self.live.setStyleSheet(HEADER_END_STYLE if live else HEADER_GO_STYLE)

    def set_view(self, view: str, choices: bool) -> None:
        """Which canvases the stage shows ("both", "horizontal", "vertical"); no choice with one canvas."""
        for key, button in self.view_buttons.items():
            button.setChecked(key == view)
        self.view_group.setVisible(choices)

    def _view_clicked(self, view: str) -> None:
        for key, button in self.view_buttons.items():
            button.setChecked(key == view)
        self.on_view(view)

    def set_camera(self, on: bool, error: str = "") -> None:
        """Camera on (highlighted, not pink: pink is only for Live and Recording), or off with a short reason."""
        self.camera.setToolTip("Virtual camera on. Click to turn it off." if on else
                               "Virtual camera: show SprigCast as a camera in Zoom, Discord, TikTok LIVE Studio…")
        self.camera.setChecked(on)
        from sprigcast.output.virtualcam import PRIVACY_HINT, PRIVACY_PAGE

        if error == PRIVACY_HINT:
            error = (f'{error} <a href="{PRIVACY_PAGE}" style="color: {theme.ACCENT};">Open settings</a>')
        self.camera_note.setText(error)
        self.camera_note.setVisible(bool(error) and not on)

    def set_recording(self, on: bool, paused: bool = False) -> None:
        """The button always says Record: pink means on (and plain again while paused). Pause shows beside it
        while recording."""
        lit = on and not paused
        self.record.setStyleSheet(HEADER_RECORDING_STYLE if lit else HEADER_RECORD_STYLE)
        self.record.setIcon(_dot_icon("#ffffff" if lit else theme.LIVE if on else theme.MUTED))
        self.record.setToolTip("Paused. Click to stop and save." if paused else "Recording. Click to stop." if on else
                               "Record the canvases ticked in Controls, to your recordings folder")
        self.pause.setVisible(on)
        self.pause.setText("Resume" if paused else "Pause")

    def set_clip(self, on: bool, seconds: int) -> None:
        """The button always says Clip: highlighted when ready (a click saves the last `seconds`)."""
        self._clip_on, self._clip_seconds = on, seconds
        self.clip.setToolTip(f"Clips on. Click to save the last {seconds} s (length and shapes: Settings → Recording)"
                             if on else f"Get clips ready: SprigCast keeps the last {seconds} s, so Clip can save it")
        self.clip.setChecked(on)

    def clip_saved(self, files: int) -> None:
        """A short flash of the button (the status bar names the files); the label stays Clip."""
        from PySide6.QtCore import QTimer

        if files:
            self.clip.setChecked(False)
            QTimer.singleShot(250, self, lambda: self.set_clip(self._clip_on, self._clip_seconds))

    # Destinations ---------------------------------------------------------------------------

    def _show_add_menu(self) -> None:
        self.add_menu.exec(self.add_place.mapToGlobal(self.add_place.rect().bottomLeft()))

    def set_editing(self, on: bool) -> None:
        """Edit mode: each place gets Remove, its shape and (Twitch) test mode, without leaving Live."""
        self.editing = on and not self.is_live
        self.manage.setText("Done" if self.editing else "Edit")
        self.all_settings.setVisible(self.editing)
        self.show_destinations(self._places, self._levels)

    def show_destinations(self, places: list[Destination], levels: dict[str, str]) -> None:
        """levels: destination id -> health level ("good", "warning", "bad", "idle", "off")."""
        self._places, self._levels = list(places), dict(levels)
        shape = ([p.id for p in places], self.editing)
        if shape != self._shape_key:
            self._shape_key = shape
            while self.places.count():
                item = self.places.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()
            self.place_rows, self.place_tools = {}, {}
            if not places:
                empty = QPushButton("+ Add a place to stream")
                empty.setStyleSheet(theme.LINK_STYLE)
                empty.clicked.connect(self._show_add_menu)
                self.places.addWidget(empty)
            for place in places:
                row = self._place_row(place)
                self.places.addWidget(row)
                row.show()  # now, so the list measures its rows straight away
            self.places_scroll._fit()
        from sprigcast.app.destinations import HEALTH_COLORS

        for place in places:
            tile, name, note, switch = self.place_rows[place.id]
            switch.blockSignals(True)
            switch.setChecked(place.on)
            switch.blockSignals(False)
            switch.setEnabled(not self.is_live)
            level = levels.get(place.id, "idle" if place.on else "off")
            colour = HEALTH_COLORS.get(level, HEALTH_COLORS['idle'])
            name.setText(place.name)
            name.setStyleSheet(f"color: {theme.TEXT_BRIGHT if place.on else theme.TEXT}; font-weight: 600;"
                               " background: transparent;")
            # The second line: the shape, then how it's doing (the live bitrate in its health colour).
            state = colour if place.on and level != "idle" else theme.MUTED
            note.setText(f'{place.canvas.capitalize()}  ·  <span style="color:{state}">{_sentence(place.note)}</span>')
            tools = self.place_tools.get(place.id)
            if tools:
                tall = place.canvas == "vertical"
                tools["shape"].setText("Vertical" if tall else "Horizontal")
                if "test" in tools:
                    tools["test"].blockSignals(True)
                    tools["test"].setChecked(bool(place.test))
                    tools["test"].blockSignals(False)

    def _place_row(self, place: Destination) -> QWidget:
        if not self.editing:
            box = QPushButton()
            # The row's height in the style sheet too: the app's QPushButton min-height would win at polish.
            box.setStyleSheet("QPushButton { background: transparent; border: none; padding: 0; text-align: left;"
                              " min-height: 44px; max-height: 44px; }")
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            box.setToolTip("Open this place's settings")
            box.clicked.connect(lambda _=False, pid=place.id: self.on_destination(pid))
        else:
            box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(12)
        letters, fill, ink = PLATFORM_TILES.get(place.service, (place.name[:2], theme.RAISED_HOVER, theme.TEXT))
        tile = QLabel(letters)
        tile.setFixedSize(32, 32)
        tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tile.setStyleSheet(f"background: {fill}; color: {ink}; border-radius: 10px; font-weight: 800;"
                           f" border: 1px solid {theme.EDGE_STRONG if fill == theme.CHROME else fill};")
        name, note = Elided(), QLabel()
        for label in (name, note):  # a long name or note is cut at the card's edge, never the switch
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setMinimumWidth(40)
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setStyleSheet(f"color: {theme.MUTED}; background: transparent; font-size: 12px;")
        for label in (tile, name, note):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        words = QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(name)
        words.addWidget(note)
        switch = Switch()
        switch.setCursor(Qt.CursorShape.PointingHandCursor)
        switch.setToolTip("On: Go live sends here. Off: skipped.")
        switch.toggled.connect(lambda on, pid=place.id: self.on_toggle(pid, on))
        line.addWidget(tile)
        line.addLayout(words, 1)
        line.addWidget(switch)
        outer.addLayout(line)
        if not self.editing:
            box.setFixedHeight(44)  # a QPushButton sizes to its text, not its layout: give it the row's height
        self.place_rows[place.id] = (tile, name, note, switch)
        if self.editing:
            tools_row = QHBoxLayout()
            tools_row.setContentsMargins(0, 2, 0, 4)
            tools_row.setSpacing(6)
            tools: dict[str, QPushButton] = {}

            def chip(key: str, text: str, tip: str, style: str = CHIP_STYLE) -> QPushButton:
                b = QPushButton(text)
                b.setStyleSheet(style)
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setToolTip(tip)
                tools[key] = b
                tools_row.addWidget(b)
                return b

            chip("shape", "", "Which canvas this place gets: click to swap").clicked.connect(
                lambda _=False, pid=place.id: self.on_place(pid, "shape"))
            if place.test is not None:
                test = chip("test", "Test", "Bandwidth test: send to Twitch without going live")
                test.setCheckable(True)
                test.toggled.connect(lambda _on, pid=place.id: self.on_place(pid, "test"))
            gear = chip("settings", "", "Key, bitrate and everything else")
            gear.setIcon(icons.icon("gear", size=14))
            gear.clicked.connect(
                lambda _=False, pid=place.id: self.on_destination(pid))
            tools_row.addStretch(1)
            remove = chip("remove", "Remove", "Remove this place (click twice)", REMOVE_CHIP_STYLE)
            remove.clicked.connect(lambda _=False, pid=place.id, b=remove: self._remove_clicked(pid, b))
            outer.addLayout(tools_row)
            self.place_tools[place.id] = tools
        return box

    def _remove_clicked(self, place_id: str, button: QPushButton) -> None:
        """First click asks ("Sure?"), the second removes."""
        if button.text() != "Sure?":
            button.setText("Sure?")
            return
        self.on_place(place_id, "remove")

    # Sound --------------------------------------------------------------------------------

    def show_sound(self, sources: list) -> None:
        """Sound items for every audio source shown in Live (see app/sound_card.py)."""
        self.sound.show_sound(sources)

    def set_levels(self, levels: dict[str, tuple[float, float]]) -> None:
        self.sound.set_levels(levels)


class StageCard(QFrame):
    """The rounded card that holds the program previews."""

    def __init__(self):
        super().__init__()
        self.setObjectName("stageCard")
        self.setStyleSheet(f"#stageCard {{ background: {theme.PANEL}; border: none; border-radius: 28px; }}"
                           "#stageCard QSplitter::handle { background: transparent; }")
        self.root = QVBoxLayout(self)
        self.root.setContentsMargins(24, 24, 24, 18)
        self.root.setSpacing(16)
        self.holder = QVBoxLayout()  # the main window puts its canvas splitter in here
        self.holder.setContentsMargins(0, 0, 0, 0)
        self.root.addLayout(self.holder, 1)
        # While live: a small banner over the top of the stage when a destination reconnects or
        # auto-pilot has lowered its bitrate (it floats, so the canvases never jump).
        self.notice = QLabel("", self)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.notice.hide()



    def set_notice(self, text: str, level: str = "warning") -> None:
        """Show (or with "" hide) the banner: amber for a lowered bitrate, red while reconnecting."""
        if not text:
            self.notice.hide()
            return
        colour = theme.BAD if level == "bad" else theme.WARN
        self.notice.setText(("⟳  " if level == "bad" else "↓  ") + text)
        self.notice.setStyleSheet(f"QLabel {{ background: {theme.CHROME}; color: {colour}; border: 1px solid {colour};"
                                  " border-radius: 16px; padding: 0 18px; font-weight: 600; }")
        self.notice.setWordWrap(True)
        self.notice.ensurePolished()  # measured with its padding and bold font, not the plain ones
        self._place_notice()
        self.notice.show()
        self.notice.raise_()

    def _place_notice(self) -> None:
        """One line when it fits; a long one wraps onto more lines instead of being cut off."""
        label = self.notice
        room = max(160, self.width() - 80)
        natural = label.fontMetrics().horizontalAdvance(label.text()) + 36 + 4  # padding 18 each side, border
        width = min(natural, room)
        height = max(32, label.heightForWidth(width) + 12) if natural > room else 32
        label.setFixedSize(width, height)  # 32 high with radius 16; taller wraps keep the same round ends
        label.move((self.width() - width) // 2, 56)  # just inside the top of the canvases

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self.notice.isVisible():
            self._place_notice()

class LiveStage(QWidget):
    """The whole Live screen. The main window fills `card.holder` and `tiles_slot` when Live starts.

        ┌ stage ──────────────────────────────┐ ┌ Chat ──────┐
        │ the canvases                         │ │            │
        │ [Both|Horizontal|Vertical] [Clip|…]  │ │            │
        └──────────────────────────────────────┘ └────────────┘
        [Blooms] [Sound] [Transitions]            [Destinations]
    """

    def __init__(self, side: LiveSide):
        super().__init__()
        self.setObjectName("liveStage")
        self.setStyleSheet(f"#liveStage {{ background: {theme.CHROME}; }}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(GAP, 8, GAP, GAP)  # one gap size around and between every card
        root.setSpacing(GAP)
        top = QHBoxLayout()
        top.setSpacing(GAP)
        self.card = StageCard()
        self.card.root.addWidget(side.stage_bar, 0, Qt.AlignmentFlag.AlignHCenter)
        top.addWidget(self.card, 1)
        top.addWidget(side)  # Chat (and any other Live panels), as tall as the stage
        root.addLayout(top, 1)
        # Under it all: Blooms | Sound | Transitions | Destinations (as wide as Chat above it).
        self.bottom = QWidget()
        row = QHBoxLayout(self.bottom)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(GAP)
        self.blooms_card, self.transitions_card = side_card(), side_card()
        self.shares = {self.blooms_card: 5, side.sound_card: 7, self.transitions_card: 3}
        # Each card keeps enough room for its own controls; past that they share by these weights.
        self.least = {self.blooms_card: 240, side.sound_card: 225, self.transitions_card: 235}
        for card, share in self.shares.items():
            card.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            card.setMinimumWidth(self.least[card])
            row.addWidget(card, share)
        side.where_card.setFixedWidth(SIDE_WIDTH)
        row.addWidget(side.where_card)
        self.bottom_row = row
        self.bottom.setFixedHeight(BOTTOM_HEIGHT)
        root.addWidget(self.bottom)
        self.tiles_slot = self.blooms_card.layout()
        self.side = side

    def set_blooms_wide(self, wide: bool) -> None:
        """Two bloom columns (both canvases) need more room than one; Sound takes what's left."""
        self.bottom_row.setStretch(0, 7 if wide else 3)
        self.blooms_card.setMinimumWidth(360 if wide else 200)  # two lists side by side need a little more

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # About a third of the screen, so a small laptop screen still has a canvas worth looking at.
        self.bottom.setFixedHeight(max(220, min(BOTTOM_HEIGHT, int(self.height() * 0.32))))
