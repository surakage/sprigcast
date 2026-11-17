"""Get started: a few taps on first start set up a garden, a path and the blooms to go with them.

    What do you make?        Gaming · Just chatting · Music · Podcasts · Tutorials · Esports · Sports · IRL · Other
    Go live, record or both  Go live · Record
    Shape                    Landscape · Portrait · Both
    Where to                 Twitch · YouTube · Kick · TikTok · X            (only with Go live)

The answers name a garden after what you make and a path after where it goes ("Twitch + TikTok",
"Recording"), put a place on that path for each pick and switch on the canvases for the shape. Step 2
("Your gear") takes the camera, capture card and mic and makes the blooms that fit with the
Starter kit's builder. Devices are listed only when step 2 opens.

`plan_*` functions are plain data in, data out, so they're tested without a screen. Offered once on a
fresh install, and any time from Tools → Get started.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme
from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output.stream import SERVICES, preset_size

DONE = "setup/getting_started_done"  # QSettings: offered once

# key, label, icon
CONTENT = (
    ("gaming", "Gaming", "gamepad"),
    ("chatting", "Just chatting", "chat"),
    ("music", "Music", "note"),
    ("podcasts", "Podcasts", "mic"),
    ("tutorials", "Tutorials", "monitor"),
    ("esports", "Esports", "trophy"),
    ("sports", "Sports", "ball"),
    ("irl", "IRL", "camera"),
    ("other", "Other", "plus"),
)
SHAPES = (("horizontal", "Landscape", "landscape"), ("vertical", "Portrait", "portrait"), ("both", "Both", "dual"))
PLACES = ("twitch", "youtube", "kick", "tiktok", "x")
PLACE_COLORS = {"twitch": "#9b6cff", "youtube": "#ff4e45", "kick": "#53fc18", "tiktok": "#25f4ee", "x": "#e6ede8"}

# Which starter blooms each kind of show gets, and the name a bloom takes for it.
NO_SCREEN = ("Starting soon", "Just Chatting", "Be right back", "Ending")
ALL_BLOOMS = ("Starting soon", "Just Chatting", "Gaming", "Be right back", "Ending")
BLOOMS_FOR = {
    "gaming": (ALL_BLOOMS, {}),
    "esports": (ALL_BLOOMS, {}),
    "chatting": (NO_SCREEN, {}),
    "music": (NO_SCREEN, {"Just Chatting": "Music"}),
    "podcasts": (NO_SCREEN, {"Just Chatting": "Podcast"}),
    "irl": (NO_SCREEN, {"Just Chatting": "IRL"}),
    "tutorials": (ALL_BLOOMS, {"Gaming": "Screen"}),
    "sports": (ALL_BLOOMS, {"Gaming": "Screen"}),
    "other": (ALL_BLOOMS, {"Gaming": "Screen"}),
}


@dataclass
class Answers:
    content: str = "gaming"
    live: bool = True
    record: bool = False
    shape: str = "horizontal"  # horizontal, vertical or both
    places: list[str] = field(default_factory=list)  # service keys, in PLACES order


def content_label(key: str) -> str:
    return next((label for k, label, _ in CONTENT if k == key), "Other")


def plan_blooms(answers: Answers) -> tuple[list[str], dict[str, str]]:
    """The starter blooms to pick and their names for this kind of show."""
    blooms, names = BLOOMS_FOR.get(answers.content, BLOOMS_FOR["other"])
    return list(blooms), dict(names)


def plan_path_name(answers: Answers) -> str:
    """Where the show goes: "Twitch + TikTok", "Multistream", "Live", "Recording"."""
    if not answers.live:
        return "Recording"
    labels = [SERVICES[p].label for p in answers.places if p in SERVICES]
    if not labels:
        return "Live"
    return " + ".join(labels) if len(labels) <= 3 else "Multistream"


def free_name(name: str, taken: list[str]) -> str:
    """`name`, or "name 2", "name 3"... when it's taken (any case)."""
    used = {t.casefold() for t in taken}
    if name.casefold() not in used:
        return name
    n = 2
    while f"{name} {n}".casefold() in used:
        n += 1
    return f"{name} {n}"


def shown_canvases(answers: Answers) -> list[str]:
    return ["horizontal", "vertical"] if answers.shape == "both" else [answers.shape]


def plan_outputs(answers: Answers, collection: SceneCollection, taken: set[str]) -> list[Output]:
    """One place per pick, set up the way its service likes, on a canvas that's on (its own if it can)."""
    if not answers.live:
        return []
    shapes = shown_canvases(answers)
    canvases = {c.id: c for c in collection.canvases}
    taken = set(taken) | {o.id for o in collection.outputs}
    outputs: list[Output] = []
    for key in answers.places:
        service = SERVICES.get(key)
        if service is None:
            continue
        canvas_id = service.canvas if service.canvas in shapes else shapes[0]
        canvas = canvases.get(canvas_id) or collection.canvases[0]
        n = 1
        while f"out-{n}" in taken:
            n += 1
        output_id = f"out-{n}"
        taken.add(output_id)
        kbps = min(service.kbps, service.max_kbps)
        outputs.append(Output(id=output_id, type="stream", service=key, canvas=canvas.id, bitrate_kbps=kbps,
                              key_ref=f"sprigcast/{output_id}", enabled=True,
                              size=preset_size(service, canvas), fps=min(service.fps, canvas.fps)))
    return outputs


def apply_path(answers: Answers, collection: SceneCollection, taken: set[str]) -> list[Output]:
    """Put the answers into the path on screen: its places, and which canvases record. Returns the places."""
    outputs = plan_outputs(answers, collection, taken)
    collection.outputs = [o for o in collection.outputs if o.type != "stream"] + outputs
    collection.recording.canvases = shown_canvases(answers)
    return outputs



# Gear ------------------------------------------------------------------------------------------------------

@dataclass
class Gear:
    """What's plugged in, listed once when the gear step opens (listing devices costs a moment)."""

    cameras: list = field(default_factory=list)  # CameraDevice: webcams and capture cards together
    mics: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)


@dataclass
class GearPicks:
    camera: object | None = None  # a CameraDevice
    capture: object | None = None  # a CameraDevice that's a capture card
    mic: str | None = "default"


def capture_audio(card: str, mics: list[str]) -> str | None:
    """The capture card's own sound input: one named after the card, else one that says it's a capture."""
    from sprigcast.sources.camera import is_capture_card

    plain = card.lower()
    return next((m for m in mics if plain and plain in m.lower()), None) or \
        next((m for m in mics if is_capture_card(m)), None)


def plan_sound() -> dict:
    """PC sound for the Starter kit: one row per type (games, music, voice chat...), each catching its usual
    apps. Works the same with or without a mixer app like Wave Link, so there's nothing to ask."""
    from sprigcast.sources.app_audio import SOUND_TYPES

    return {t.key: None for t in SOUND_TYPES}


# Window ----------------------------------------------------------------------------------------------------

class Choice(QPushButton):
    """A rounded chip with a line icon and a word; picked: green edge and fill."""

    def __init__(self, text: str, icon_name: str, key: str):
        super().__init__(text)
        self.key = key
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIcon(icons.icon(icon_name, theme.TEXT, 18))
        self.setIconSize(QSize(18, 18))
        self.setStyleSheet(
            theme.button_style(18, "8px 16px", extra="text-align: left; min-height: 20px;"))


class PlaceCard(QPushButton):
    """A destination: a coloured dot, the service name and one short state line. Tap to pick it."""

    HEIGHT = 58

    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.note = ""
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(SERVICES[key].label)
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; padding: 0;"
                           f" min-height: {self.HEIGHT}px; max-height: {self.HEIGHT}px; }}")

    def sizeHint(self) -> QSize:
        return QSize(220, self.HEIGHT)

    def set_note(self, text: str) -> None:
        self.note = text
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        on = self.isChecked()
        p.setPen(QPen(QColor(theme.ACCENT if on else theme.ACCENT if self.underMouse() else theme.EDGE_STRONG), 1.2))
        p.setBrush(QColor(theme.SELECT_BG if on else theme.RAISED_HOVER if self.underMouse() else theme.CARD))
        p.drawRoundedRect(r, 18, 18)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(PLACE_COLORS.get(self.key, theme.MUTED)))
        p.drawEllipse(QRectF(r.left() + 18, r.center().y() - 6, 12, 12))
        font = p.font()
        font.setWeight(font.Weight.DemiBold)
        p.setFont(font)
        p.setPen(QColor(theme.SELECT_TEXT if on else theme.TEXT))
        text_left = r.left() + 42
        if self.note:
            p.drawText(QRectF(text_left, r.top() + 9, r.width() - 80, 20), Qt.AlignmentFlag.AlignVCenter,
                       SERVICES[self.key].label)
            font.setWeight(font.Weight.Normal)
            font.setPixelSize(max(10, font.pixelSize() - 1) if font.pixelSize() > 0 else 11)
            p.setFont(font)
            p.setPen(QColor(theme.MUTED))
            p.drawText(QRectF(text_left, r.top() + 28, r.width() - 80, 18), Qt.AlignmentFlag.AlignVCenter, self.note)
        else:
            p.drawText(QRectF(text_left, r.top(), r.width() - 80, r.height()), Qt.AlignmentFlag.AlignVCenter,
                       SERVICES[self.key].label)
        if on:  # a tick on the right
            pen = QPen(QColor(theme.ACCENT), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            x, y = r.right() - 30, r.center().y()
            p.drawLine(QRectF(x, y, 0, 0).topLeft(), QRectF(x + 4, y + 4, 0, 0).topLeft())
            p.drawLine(QRectF(x + 4, y + 4, 0, 0).topLeft(), QRectF(x + 12, y - 5, 0, 0).topLeft())
        p.end()


def _heading(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600; background: transparent;")
    return label


class GettingStartedDialog(QDialog):
    """Two steps: the questions, then your gear. `answers()` and `gear()` after exec();
    `twitch` holds (account, stream key) if Twitch was signed in."""

    def __init__(self, parent: QWidget | None = None,
                 twitch_account: Callable[[], str | None] | None = None,
                 connect_twitch: Callable[[QWidget], tuple[str, str | None] | None] | None = None,
                 list_gear: Callable[[], Gear] | None = None,
                 can_sign_in: Callable[[str], bool] | None = None,
                 sign_in: Callable[[str, QWidget], object] | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Get started")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(720)
        self._twitch_account = twitch_account or (lambda: None)
        self._connect_twitch = connect_twitch
        self._list_gear = list_gear or Gear
        self.found: Gear | None = None
        self.twitch: tuple[str, str | None] | None = None
        self._can_sign_in = can_sign_in or (lambda _service: False)
        self._sign_in = sign_in
        self.fetched: dict[str, object] = {}  # YouTube / Kick: service -> Fetched (account, key, server)
        self._drag = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 26)  # room for the shadow
        card = QFrame()
        card.setObjectName("guideCard")
        card.setStyleSheet(f"#guideCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 24px; }")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(30, 26, 30, 24)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(18)
        words = QVBoxLayout()
        words.setSpacing(6)
        self.title = QLabel("Let's get you growing")
        font = self.title.font()
        font.setPixelSize(22)
        font.setWeight(font.Weight.DemiBold)
        self.title.setFont(font)
        self.title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; background: transparent;")
        words.addWidget(self.title)
        self.intro = QLabel("A few taps and SprigCast sets itself up for you. Change anything later.")
        self.intro.setWordWrap(True)
        self.intro.setStyleSheet(theme.MUTED_STYLE + "; background: transparent;")
        words.addWidget(self.intro)
        self.step = QLabel("1 of 2")
        self.step.setStyleSheet(f"color: {theme.MUTED}; font-size: 9pt; letter-spacing: 1px; background: transparent;")
        words.addWidget(self.step)
        words.addStretch(1)
        top.addLayout(words, 1)
        try:
            from sprigcast.app.empty import ArtPicture

            self.art = ArtPicture("welcome", 120)
            top.addWidget(self.art, 0, Qt.AlignmentFlag.AlignTop)
        except Exception:  # noqa: BLE001 (art is a nicety)
            self.art = None
        root.addLayout(top)
        root.addSpacing(6)

        self.pages = QStackedWidget()
        self.pages.setStyleSheet("QStackedWidget { background: transparent; }")
        self.pages.addWidget(self._questions_page())
        self.gear_page = QWidget()
        self.gear_page.setStyleSheet("background: transparent;")
        self.pages.addWidget(self.gear_page)
        root.addWidget(self.pages)

        root.addSpacing(10)
        buttons = QHBoxLayout()
        self.skip = QPushButton("Not now")
        self.skip.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip.clicked.connect(self._back_or_close)
        buttons.addWidget(self.skip)
        buttons.addStretch(1)
        self.go = QPushButton("Next  →")
        self.go.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 18px; padding: 10px 22px; }")
        self.go.setCursor(Qt.CursorShape.PointingHandCursor)
        self.go.setDefault(True)
        self.go.clicked.connect(self._next)
        buttons.addWidget(self.go)
        root.addLayout(buttons)
        self._changed()

    # Step 1: the questions ------------------------------------------------------------------------------

    def _questions_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.addWidget(_heading("What do you make?"))
        self.content: dict[str, Choice] = {}
        self._content_group = QButtonGroup(self)  # one pick (auto-exclusive would span every chip on the card)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for i, (key, label, icon_name) in enumerate(CONTENT):
            chip = self.content[key] = Choice(label, icon_name, key)
            self._content_group.addButton(chip)
            chip.toggled.connect(self._changed)
            grid.addWidget(chip, i // 5, i % 5)
        for column in range(5):
            grid.setColumnStretch(column, 1)
        root.addLayout(grid)
        root.addSpacing(8)

        root.addWidget(_heading("Go live, record, or both?"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.live = Choice("Go live", "broadcast", "live")
        self.record = Choice("Record", "record", "record")
        for chip in (self.live, self.record):
            chip.toggled.connect(self._changed)
            row.addWidget(chip)
        row.addStretch(1)
        root.addLayout(row)
        root.addSpacing(8)

        root.addWidget(_heading("Shape"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.shapes: dict[str, Choice] = {}
        self._shape_group = QButtonGroup(self)
        for key, label, icon_name in SHAPES:
            chip = self.shapes[key] = Choice(label, icon_name, key)
            self._shape_group.addButton(chip)
            chip.toggled.connect(self._changed)
            row.addWidget(chip)
        row.addStretch(1)
        root.addLayout(row)
        root.addSpacing(8)

        self.places_box = QWidget()
        self.places_box.setStyleSheet("background: transparent;")
        places = QVBoxLayout(self.places_box)
        places.setContentsMargins(0, 0, 0, 0)
        places.setSpacing(10)
        places.addWidget(_heading("Where do you go live?"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        self.places: dict[str, PlaceCard] = {}
        for i, key in enumerate(PLACES):
            place = self.places[key] = PlaceCard(key)
            place.toggled.connect(lambda on, k=key: self._place_toggled(k, on))
            grid.addWidget(place, i // 2, i % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        places.addLayout(grid)
        root.addWidget(self.places_box)
        self.places_box.hide()
        root.addStretch(1)
        return page

    # Step 2: your gear ----------------------------------------------------------------------------------

    def _fill_gear_page(self) -> None:
        """Lists the devices once, the first time this step opens."""
        from sprigcast.app.starter import BOX_HEIGHT, combo_style
        from sprigcast.sources.audio import device_label
        from sprigcast.sources.camera import is_capture_card

        gear = self.found = self._list_gear()
        style = combo_style()
        box = QFrame()
        box.setObjectName("gearCard")
        box.setStyleSheet(f"#gearCard {{ background: {theme.CARD}; border-radius: 18px; }}")
        form = QGridLayout(box)
        form.setContentsMargins(18, 16, 18, 16)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        form.setColumnStretch(2, 1)
        page = QVBoxLayout(self.gear_page)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(box)
        page.addStretch(1)

        def row(icon_name: str, text: str) -> QComboBox:
            mark = QLabel()
            mark.setPixmap(icons.pixmap(icon_name, theme.ACCENT, 20))
            mark.setStyleSheet("background: transparent;")
            label = QLabel(text)
            label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600; background: transparent;")
            label.setMinimumWidth(110)
            pick = QComboBox()
            pick.setStyleSheet(style)
            pick.setFixedHeight(BOX_HEIGHT)
            pick.setCursor(Qt.CursorShape.PointingHandCursor)
            r = form.rowCount()
            form.addWidget(mark, r, 0)
            form.addWidget(label, r, 1)
            form.addWidget(pick, r, 2)
            return pick

        cards = [c for c in gear.cameras if is_capture_card(c.name)]
        webcams = [c for c in gear.cameras if c not in cards]
        self.camera_pick = row("camera", "Camera")
        self.camera_pick.addItem("No camera", None)
        for cam in webcams:
            self.camera_pick.addItem(cam.name, cam)
        if webcams:
            self.camera_pick.setCurrentIndex(1)
        self.capture_pick = None
        if cards:  # only when one is plugged in
            self.capture_pick = row("gamepad", "Capture card")
            self.capture_pick.addItem("Not now", None)
            for cam in cards:
                self.capture_pick.addItem(cam.name, cam)
            self.capture_pick.setCurrentIndex(1)
        self.mic_pick = row("mic", "Mic")
        self.mic_pick.addItem("Default mic", "default")
        for name in gear.mics:
            if not is_capture_card(name):
                self.mic_pick.addItem(device_label(name), name)
        self.mic_pick.addItem("No mic", None)

    def gear(self) -> GearPicks:
        if self.found is None:
            return GearPicks()
        return GearPicks(camera=self.camera_pick.currentData(),
                         capture=self.capture_pick.currentData() if self.capture_pick is not None else None,
                         mic=self.mic_pick.currentData())

    # Moving between steps -------------------------------------------------------------------------------

    def _next(self) -> None:
        if self.pages.currentIndex() == 1:
            self.accept()
            return
        if self.found is None:
            self._fill_gear_page()
        self.pages.setCurrentIndex(1)
        self.title.setText("Your gear")
        self.intro.setText("Pick what to use. SprigCast makes your blooms with it.")
        self._changed()

    def _back_or_close(self) -> None:
        if self.pages.currentIndex() == 0:
            self.reject()
            return
        self.pages.setCurrentIndex(0)
        self.title.setText("Let's get you growing")
        self.intro.setText("A few taps and SprigCast sets itself up for you. Change anything later.")
        self._changed()

    # Picks -----------------------------------------------------------------------------------------------

    def _picked(self, chips: dict[str, Choice]) -> str | None:
        return next((k for k, c in chips.items() if c.isChecked()), None)

    def _changed(self) -> None:
        if not hasattr(self, "go"):
            return
        gear = self.pages.currentIndex() == 1
        self.places_box.setVisible(self.live.isChecked())
        ready = (self._picked(self.content) is not None and self._picked(self.shapes) is not None
                 and (self.live.isChecked() or self.record.isChecked()))
        self.go.setEnabled(ready or gear)
        self.go.setText("Get started  →" if gear else "Next  →")
        self.skip.setText("Back" if gear else "Not now")
        self.step.setText("2 of 2" if gear else "1 of 2")
        self.pages.currentWidget().adjustSize()
        self.pages.setFixedHeight(self.pages.currentWidget().sizeHint().height())
        self.adjustSize()

    def _place_toggled(self, key: str, on: bool) -> None:
        place = self.places[key]
        if key != "twitch":
            if on and key not in self.fetched and self._sign_in is not None and self._can_sign_in(key):
                fetched = self._sign_in(key, self)
                if fetched is not None:
                    self.fetched[key] = fetched
            got = self.fetched.get(key) if on else None
            place.set_note(getattr(got, "account", "") if got is not None else "Key next" if on else "")
            return
        if not on:
            place.set_note("")
            return
        account = self.twitch[0] if self.twitch else self._twitch_account()
        if not account and self._connect_twitch is not None:
            result = self._connect_twitch(self)
            if result:
                self.twitch = result
                account = result[0]
        place.set_note(account or "Key next")

    def answers(self) -> Answers:
        return Answers(content=self._picked(self.content) or "other", live=self.live.isChecked(),
                       record=self.record.isChecked(), shape=self._picked(self.shapes) or "horizontal",
                       places=[k for k in PLACES if self.places[k].isChecked()] if self.live.isChecked() else [])

    # Drag by any empty spot ------------------------------------------------------------------------------

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
