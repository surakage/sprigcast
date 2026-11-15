"""Starter kit: a ready set of blooms from a few choices, so a first stream needs no building.

    Camera  [Elgato Facecam ▾]     Screen  [Monitor 1 ▾]     Mic  [Default ▾]
    Desktop sound  (By type | All in one)   ● Games [Full-screen game ▾]  ● Music [Usual: Spotify… ▾] ...
    Blooms  ☑ Starting soon  ☑ Just Chatting  ☑ Gaming  ☑ Be right back  ☑ Ending
    ☑ Vertical versions too

Makes a "Main Scenes" folder (the Live folder) on each shape, a Webcam sprig used by the blooms
with a camera, and adds the mic and desktop sound once (sound plays in every bloom on both shapes).
`build_starter` does the work and is plain data in, data out, so it's tested without a screen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.edit import cover
from sprigcast.core.scene import PART_KIND, Scene, SceneCollection, SceneItem, SourceConfig

BLOOMS = ("Starting soon", "Just Chatting", "Gaming", "Be right back", "Ending")
TITLES = {"Starting soon": "Starting soon", "Be right back": "Be right back", "Ending": "Thanks for watching!"}
WITH_CAMERA = {"Just Chatting", "Gaming"}
FULL_CAMERA = "Just Chatting"  # the camera fills this one; the others have it in a corner
ART = {"Starting soon": "sloth-starting.png", "Be right back": "sloth-brb.png", "Ending": "sloth-ending.png"}
BACKGROUND = "#16241dff"
DEFAULT_IDS = {"starting", "starting-v", "brb", "brb-v"}  # the example blooms a fresh install starts with


@dataclass
class StarterChoice:
    camera: dict | None = None  # camera source settings (name, path, width, height, fps, mirror)
    monitor: tuple[int, int, int] | None = None  # index, width, height
    mic: str | None = "default"  # device name, "default", or None for no mic
    desktop: str | bool | None = "default"  # speakers to capture: name, "default", or None/False for none
    # Sound by type instead (Games, Music, ...): type key -> exe names picked, or None for the usual apps.
    # When set, `desktop` is left out so nothing plays twice.
    app_sounds: dict[str, list[str] | None] | None = None
    blooms: list[str] = field(default_factory=lambda: list(BLOOMS))
    vertical: bool = True
    replace_example: bool = True


def is_fresh(collection: SceneCollection) -> bool:
    """Only the example blooms that come with a new install (nothing of the person's own yet)."""
    shows = {s.id for s in collection.scenes if s.kind == "show"}
    return shows <= DEFAULT_IDS and not collection.parts()


def build_starter(collection: SceneCollection, choice: StarterChoice) -> list[str]:
    """Add the starter blooms to `collection` and return their ids (horizontal ones first)."""
    if choice.replace_example:
        _drop_example(collection)
    if choice.vertical:  # vertical versions: switch the vertical canvas on if it was off
        for canvas in collection.canvases:
            canvas.enabled = True
    canvases = [c for c in collection.canvases if c.enabled or c is collection.canvases[0]]
    main = canvases[0]
    if not choice.vertical:
        canvases = [main]
    camera_src = None
    if choice.camera is not None:
        camera_src = SourceConfig(id=collection.new_id("camera"), kind="camera", name="Camera",
                                  settings=dict(choice.camera))
        collection.sources.append(camera_src)
    webcam = None
    if camera_src is not None:  # one Webcam sprig: move or restyle it once, every bloom follows
        cam_w, cam_h = int(choice.camera.get("width", 1280)), int(choice.camera.get("height", 720))
        scale = main.width * 0.3 / cam_w
        webcam = Scene(id=collection.new_id("part"), name="Webcam", canvas=main.id, kind="part", items=[
            SceneItem(source=camera_src.id, scale=scale, x=main.width - cam_w * scale - 40,
                      y=main.height - cam_h * scale - 40)])
        collection.scenes.append(webcam)
    screen_src = None
    if choice.monitor is not None:
        index, _w, _h = choice.monitor
        screen_src = SourceConfig(id=collection.new_id("display"), kind="display_capture", name=f"Screen {index}",
                                  settings={"monitor": index, "cursor": True})
        collection.sources.append(screen_src)
    made: list[str] = []
    first_ids: dict[str, str] = {}
    pictures: dict[str, SourceConfig] = {}  # one sloth source per bloom name, shared by both shapes
    for canvas in canvases:
        folder = next((f for f in collection.folders_for(canvas.id) if f.name == "Main Scenes"), None) \
            or collection.add_folder(canvas.id, "Main Scenes")
        folder.live = True
        background = SourceConfig(id=collection.new_id("bg"), kind="color", name="Background",
                                  settings={"color": BACKGROUND, "width": canvas.width, "height": canvas.height})
        collection.sources.append(background)
        placement = None
        if webcam is not None:
            placement = SourceConfig(id=collection.new_id("part-src"), kind=PART_KIND, name="Webcam",
                                     settings={"scene": webcam.id})
            collection.sources.append(placement)
        for name in choice.blooms:
            items = [SceneItem(source=background.id)]
            if name == "Gaming" and screen_src is not None:
                _w, _h = choice.monitor[1], choice.monitor[2]
                fit = min(canvas.width / max(1, _w), canvas.height / max(1, _h))
                items.append(SceneItem(source=screen_src.id, scale=fit, x=(canvas.width - _w * fit) / 2,
                                       y=(canvas.height - _h * fit) / 2))
            if name == FULL_CAMERA and camera_src is not None:
                # Just Chatting: the camera fills the whole canvas (cropped to its shape, no empty strips).
                crop, scale = cover((cam_w, cam_h), (canvas.width, canvas.height))
                items.append(SceneItem(source=camera_src.id, crop=crop, scale=scale))
            elif name in WITH_CAMERA and placement is not None:
                fit = min(canvas.width / main.width, canvas.height / main.height)
                items.append(SceneItem(source=placement.id, scale=fit))
            if name in TITLES:
                title = SourceConfig(id=collection.new_id("text"), kind="text", name=TITLES[name],
                                     settings={"text": TITLES[name], "size": max(48, canvas.width // 18)})
                collection.sources.append(title)
                items.append(SceneItem(source=title.id, x=canvas.width * 0.08, y=canvas.height * 0.42))
            if name in ART:
                items.append(_sloth_item(collection, pictures, name, canvas))
            label = name if canvas is main else f"{name} (vertical)"
            scene = Scene(id=collection.new_id("scene"), name=label, canvas=canvas.id, folder=folder.id,
                          items=items, linked_to=None if canvas is main else first_ids.get(name))
            collection.scenes.append(scene)
            made.append(scene.id)
            if canvas is main:
                first_ids[name] = scene.id
        if made:
            first = next(s for s in collection.scenes if s.id in made and s.canvas == canvas.id)
            collection.active_scenes[canvas.id] = first.id
    _add_sound(collection, "audio_input", choice.mic, "Mic")
    if choice.app_sounds is not None:
        _add_app_sounds(collection, choice.app_sounds)
    else:
        desktop = "default" if choice.desktop is True else (choice.desktop or None)
        _add_sound(collection, "audio_output", desktop, "Desktop audio")
    collection.wrap_blooms()  # each bloom shows one sprig named after it, where its parts are edited
    return made


def _sloth_item(collection: SceneCollection, pictures: dict[str, SourceConfig], name: str, canvas) -> SceneItem:
    """The bloom's sloth picture: bottom right of a wide canvas, in the lower middle of a tall one.

    Never drawn bigger than the picture itself, so it stays sharp."""
    from sprigcast.sources.image import ART_PREFIX, image_size, resolve

    config = pictures.get(name)
    if config is None:
        config = SourceConfig(id=collection.new_id("art"), kind="image", name=f"Sloth ({name})",
                              settings={"path": ART_PREFIX + ART[name]})
        collection.sources.append(config)
        pictures[name] = config
    w, h = image_size(resolve(config.settings["path"])) or (480, 480)
    if canvas.width >= canvas.height:
        scale = min(1.0, canvas.height * 0.5 / h)
        x, y = canvas.width * 0.94 - w * scale, canvas.height * 0.9 - h * scale  # bottom right, clear of the title
    else:
        scale = min(1.0, canvas.width * 0.6 / w, canvas.height * 0.3 / h)  # fits between the title and the bottom
        x, y = (canvas.width - w * scale) / 2, canvas.height * 0.62
    if scale < 1.0:  # 1/2, 1/3...: each screen pixel then blends whole picture pixels, so the fur stays clean
        whole = 1.0 / math.ceil(1.0 / scale)
        if canvas.width >= canvas.height:
            x, y = x + w * (scale - whole), y + h * (scale - whole)  # keep the bottom-right corner
        else:
            x = (canvas.width - w * whole) / 2
        scale = whole
    return SceneItem(source=config.id, scale=scale, x=x, y=y)


def _add_app_sounds(collection: SceneCollection, picks: dict[str, list[str] | dict | None]) -> None:
    """One row per type of sound (Games, Music, ...), each catching only its apps (a list; None = the usual
    ones) or taking an output ({"device": name}, an Elgato Wave Link channel); skips types already there."""
    from sprigcast.sources.app_audio import TYPES

    have = {str(c.settings.get("preset")) for c in collection.sources if c.kind == "app_audio"}
    for key, apps in picks.items():
        kind = TYPES.get(key)
        if kind is None or key in have:
            continue
        if isinstance(apps, dict):  # an output, like Elgato Wave Link's channel for this sound
            settings = {"preset": key, "apps": [], "device": str(apps["device"]), "auto_game": False,
                        "volume_db": 0.0, "muted": False}
        else:
            settings = {"preset": key, "apps": list(apps or []), "volume_db": 0.0, "muted": False}
            if apps:
                settings["auto_game"] = False  # a game picked by name: just that one
        collection.sources.append(SourceConfig(id=collection.new_id(f"sound-{key}"), kind="app_audio",
                                               name=kind.name, settings=settings))


def _add_sound(collection: SceneCollection, kind: str, device: str | None, name: str) -> None:
    """Once only: an existing source for the same device already plays everywhere."""
    if device is None or any(c.kind == kind and str(c.settings.get("device", "default")) == device
                             for c in collection.sources):
        return
    collection.sources.append(SourceConfig(id=collection.new_id("mic" if kind == "audio_input" else "desktop"),
                                           kind=kind, name=name,
                                           settings={"device": device, "volume_db": 0.0, "muted": False}))


def _drop_example(collection: SceneCollection) -> None:
    gone = {s.id for s in collection.scenes if s.id in DEFAULT_IDS}
    if not gone:
        return
    names = {s.name for s in collection.scenes if s.id in gone}
    collection.scenes = [s for s in collection.scenes if s.id not in gone]
    # the example blooms' own sprigs (named after them, made when blooms began holding sprigs only) go too
    own = {p.id for p in collection.parts() if p.name in names and not collection.part_uses(p.id)}
    collection.scenes = [s for s in collection.scenes if s.id not in own and s.shape_of not in own]
    collection.sources = [c for c in collection.sources if str(c.settings.get("scene")) not in own
                          or c.kind != PART_KIND]
    collection.active_scenes = {c: s for c, s in collection.active_scenes.items() if s not in gone}
    used = {i.source for s in collection.scenes for i in s.items}
    collection.sources = [c for c in collection.sources
                          if c.id in used or c.kind in ("audio_input", "audio_output", "app_audio")]
    collection.transition_rules = [r for r in collection.transition_rules
                                   if r.from_scene not in gone and r.to_scene not in gone]


def combo_style() -> str:
    """Rounded pill boxes with a soft chevron (the plain drop-down button squares off the round end)."""
    try:
        arrow = f"QComboBox::down-arrow {{ image: url({theme.nature_pictures()['chevron']}); width: 12px; height: 12px; }}"
    except OSError:
        arrow = ""
    return (f"QComboBox {{ background: {theme.CHROME}; color: {theme.TEXT}; border: 1px solid {theme.EDGE_STRONG};"
            " border-radius: 16px; padding: 6px 14px; min-height: 20px; }"
            f"QComboBox:focus, QComboBox:hover {{ border-color: {theme.ACCENT}; }}"
            f"QComboBox:disabled {{ color: {theme.MUTED}; background: transparent; border-color: {theme.EDGE}; }}"
            "QComboBox::drop-down { border: none; background: transparent; width: 30px; }" + arrow)


CHIP_STYLE = (f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: 1px solid {theme.EDGE_STRONG};"
              " border-radius: 15px; padding: 7px 12px; font-weight: 600; }"
              f"QPushButton:hover {{ border-color: {theme.ACCENT}; color: {theme.TEXT}; }}"
              f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT};"
              f" border-color: {theme.ACCENT}; }}")


SEGMENT_STYLE = (f"QPushButton {{ background: transparent; color: {theme.MUTED}; border: none; border-radius: 12px;"
                 " padding: 4px 12px; font-weight: 600; }"
                 f"QPushButton:hover {{ color: {theme.TEXT}; }}"
                 f"QPushButton:checked {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; }}")


def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A soft rounded card with a heading row (named 'head', for extras on the right) and a body."""
    card = QFrame()
    card.setObjectName("starterSection")
    card.setStyleSheet(f"#starterSection {{ background: {theme.CARD}; border-radius: 18px; }}")
    body = QVBoxLayout(card)
    body.setContentsMargins(16, 12, 16, 14)
    body.setSpacing(10)
    head = QHBoxLayout()
    head.setObjectName("head")
    name = QLabel(title)
    name.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-weight: 700;")
    head.addWidget(name)
    head.addStretch(1)
    body.addLayout(head)
    return card, body


BOX_HEIGHT = 34  # every row of the form; pills keep their radius (16) under half this


def _row_label(text: str, box: QWidget) -> QLabel:
    """A form label as tall as its box, so the words sit level with the box's text."""
    label = QLabel(text)
    label.setMinimumHeight(max(BOX_HEIGHT, box.sizeHint().height()))
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


class StarterDialog(QDialog):
    """The few choices; the main window lists the devices and calls build_starter with the answer.

    A rounded, raised card like the app's other pop-ups (no square window frame); drag it by any empty spot.
    """

    def __init__(self, cameras: list, monitors: list, mics: list[str], fresh: bool, both_shapes: bool,
                 parent: QWidget | None = None, speakers: list[str] | None = None,
                 running_apps: list[str] | None = None):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle("Starter kit")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMinimumWidth(700)
        self.cameras, self.monitors = cameras, monitors
        self._drag = None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 14, 20, 26)  # room for the shadow
        card = QFrame()
        card.setObjectName("starterCard")
        card.setStyleSheet(f"#starterCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE_STRONG};"
                           " border-radius: 24px; }")
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(36)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 190))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)
        root = QVBoxLayout(card)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(12)
        title = QLabel("Set up your blooms in one go")
        font = title.font()
        font.setPixelSize(20)
        font.setWeight(font.Weight.DemiBold)
        title.setFont(font)
        root.addWidget(title)
        intro = QLabel("Pick your camera, screen and mic. SprigCast makes the usual blooms with them, ready to "
                       "go live. You can change anything afterwards.")
        intro.setWordWrap(True)
        intro.setStyleSheet(theme.MUTED_STYLE)
        root.addWidget(intro)
        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        style = combo_style()
        self.camera = QComboBox()
        self.camera.setStyleSheet(style)
        self.camera.setCursor(Qt.CursorShape.PointingHandCursor)
        self.camera.addItem("No camera", None)
        for cam in cameras:
            self.camera.addItem(cam.name, cam)
        if cameras:
            self.camera.setCurrentIndex(1)
        form.addRow(_row_label("Camera", self.camera), self.camera)
        self.screen = QComboBox()
        self.screen.setStyleSheet(style)
        self.screen.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screen.addItem("No screen", None)
        for monitor in monitors:
            self.screen.addItem(monitor.label, monitor)
        if monitors:
            self.screen.setCurrentIndex(1 + next((i for i, m in enumerate(monitors) if m.primary), 0))
        form.addRow(_row_label("Screen (for Gaming)", self.screen), self.screen)
        self.mic = QComboBox()
        self.mic.setStyleSheet(style)
        self.mic.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mic.addItem("Default mic (follows Windows)", "default")
        for name in mics:
            self.mic.addItem(name, name)
        self.mic.addItem("No mic", None)
        form.addRow(_row_label("Mic", self.mic), self.mic)
        for box in (self.camera, self.screen, self.mic):
            box.setFixedHeight(BOX_HEIGHT)
        root.addLayout(form)
        root.addWidget(self._desktop_section(style, speakers or [], running_apps or []))
        # Which blooms: on/off chips in their own card, with a running count.
        chips_card, chips = _section("Blooms to make")
        self.bloom_count = QLabel()
        self.bloom_count.setStyleSheet(theme.MUTED_STYLE)
        chips_card.findChild(QHBoxLayout, "head").addWidget(self.bloom_count)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.bloom_boxes: dict[str, QPushButton] = {}
        for i, name in enumerate(BLOOMS):
            chip = QPushButton()
            chip.setCheckable(True)
            chip.setStyleSheet(CHIP_STYLE)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.toggled.connect(lambda on, c=chip, n=name: (c.setText(f"✓  {n}" if on else n), self._count()))
            chip.setChecked(True)
            self.bloom_boxes[name] = chip
            grid.addWidget(chip, 0, i)  # one row: the dialog is wide enough
        for column in range(len(BLOOMS)):
            grid.setColumnStretch(column, 1)
        chips.addLayout(grid)
        root.addWidget(chips_card)
        # Extras, only when they apply.
        extras_card, extras = _section("Extras")
        self.vertical = QCheckBox("Vertical versions (TikTok, Shorts, Reels)")
        self.vertical.setChecked(both_shapes)
        if not both_shapes:
            self.vertical.setToolTip("Also switches on the vertical canvas, so you stream both shapes at once")
        self.replace = QCheckBox("Replace the example blooms")
        self.replace.setChecked(fresh)
        self.replace.setVisible(fresh)
        extras_row = QHBoxLayout()
        extras_row.setSpacing(24)
        for box in (self.vertical, self.replace):
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            extras_row.addWidget(box)
        extras_row.addStretch(1)
        extras.addLayout(extras_row)
        root.addWidget(extras_card)

        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background: {theme.EDGE_STRONG};")
        root.addWidget(line)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = QPushButton("Not now")
        skip.setStyleSheet(theme.SMALL_PILL_STYLE)
        skip.clicked.connect(self.reject)
        go = self.go = QPushButton("Make my blooms")
        go.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 17px; padding: 9px 20px; }")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        for b in (skip, go):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(b)
        root.addLayout(buttons)
        self._count()

    def _desktop_section(self, style: str, speakers: list[str], running: list[str]) -> QFrame:
        """Desktop sound: one row per type (Games, Music, ...) with its own on/off and apps, or all in one."""
        from sprigcast.app.live_stage import Switch
        from sprigcast.sources.app_audio import SOUND_TYPES
        from sprigcast.sources.audio import is_mixer_app_device

        card, body = _section("Desktop sound")
        head = card.findChild(QHBoxLayout, "head")
        self.split = QPushButton("By type")
        self.whole = QPushButton("All in one")
        for b, tip in ((self.split, "Games, music, voice chat... each with its own volume and mute"),
                       (self.whole, "Everything your PC plays, as one sound")):
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setStyleSheet(SEGMENT_STYLE)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            head.addWidget(b)
        self.split.setChecked(True)
        types = QWidget()
        grid = QGridLayout(types)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(2, 1)
        self.type_rows: dict[str, tuple] = {}
        for i, kind in enumerate(SOUND_TYPES):
            switch = Switch()
            switch.setChecked(True)
            switch.setCursor(Qt.CursorShape.PointingHandCursor)
            name = QLabel(kind.name)
            name.setMinimumWidth(104)
            pick = QComboBox()
            pick.setStyleSheet(style)
            pick.setFixedHeight(30)
            pick.setCursor(Qt.CursorShape.PointingHandCursor)
            pick.setToolTip("Where this sound comes from: an output (like an Elgato Wave Link channel), or with "
                            "Show desktop audio on, the usual apps or one app that's open now.")

            def show(on: bool, n=name, p=pick) -> None:
                n.setStyleSheet(f"color: {theme.TEXT if on else theme.MUTED}; background: transparent;")
                p.setEnabled(on)

            switch.toggled.connect(show)
            show(True)
            grid.addWidget(switch, i, 0)
            grid.addWidget(name, i, 1)
            grid.addWidget(pick, i, 2)
            self.type_rows[kind.key] = (switch, pick)
        body.addWidget(types)
        # Outputs are always offered (names differ from PC to PC, so every one is). Desktop audio, the
        # usual apps or one open app caught out of what the PC plays, joins the list with this switch:
        # off by default when a mixer app like Elgato Wave Link already sorts sound into outputs.
        self._speakers, self._running = list(speakers), list(running)
        show_row = QHBoxLayout()
        show_label = QLabel("Show desktop audio")
        show_label.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        show_label.setToolTip("Also list the usual apps and apps open now, caught out of desktop audio")
        self.show_desktop = Switch()
        self.show_desktop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_desktop.setChecked(not any(is_mixer_app_device(n) for n in speakers) or not speakers)
        show_row.addStretch(1)
        show_row.addWidget(show_label)
        show_row.addWidget(self.show_desktop)
        self._show_row = QWidget()
        self._show_row.setLayout(show_row)
        show_row.setContentsMargins(0, 0, 0, 0)
        body.addWidget(self._show_row)
        self.show_desktop.toggled.connect(self._fill_type_picks)
        self._fill_type_picks()
        self.desktop = QComboBox()
        self.desktop.setStyleSheet(style)
        self.desktop.setFixedHeight(BOX_HEIGHT)
        self.desktop.setCursor(Qt.CursorShape.PointingHandCursor)
        self.desktop.addItem("Default speakers (follows Windows)", "default")
        for name in speakers:
            self.desktop.addItem(name, name)
        self.desktop.addItem("No desktop sound", None)
        body.addWidget(self.desktop)
        self.desktop.hide()
        self.split.toggled.connect(lambda on: (types.setVisible(on), self._show_row.setVisible(on),
                                               self.desktop.setVisible(not on)))
        return card

    def _fill_type_picks(self) -> None:
        """Each type's choices: the outputs, plus (Show desktop audio) its usual apps and the apps open now."""
        from sprigcast.sources.app_audio import TYPES
        from sprigcast.sources.audio import device_label

        desktop = self.show_desktop.isChecked()
        for key, (_switch, pick) in self.type_rows.items():
            kind, before = TYPES[key], pick.currentData()
            pick.blockSignals(True)
            pick.clear()
            if not desktop:
                pick.addItem("Pick an output…" if self._speakers else "No outputs found", "")
            for output in self._speakers:
                pick.addItem(device_label(output), {"device": output})
            if desktop:
                if self._speakers:
                    pick.insertSeparator(pick.count())
                pick.addItem(f"Usual: {kind.hint}" if kind.apps else kind.hint, None)
                for app in self._running:
                    pick.addItem(app[:-4] if app.lower().endswith(".exe") else app, [app])
            found = next((i for i in range(pick.count()) if pick.itemData(i) == before), -1)
            if found >= 0 and before != "":
                pick.setCurrentIndex(found)
            elif desktop:
                pick.setCurrentIndex(pick.findData(None))  # the usual apps, as before
            pick.blockSignals(False)

    def app_sounds(self) -> dict[str, list[str] | None] | None:
        """Type -> picked apps (None = usual) for the types switched on; None when it's all in one."""
        if not self.split.isChecked():
            return None
        return {key: pick.currentData() for key, (switch, pick) in self.type_rows.items()
                if switch.isChecked() and pick.currentData() != ""}  # "" = no output picked yet: none made

    def _count(self) -> None:
        """'4 of 5' beside the heading; nothing picked means nothing to make."""
        if not hasattr(self, "bloom_count") or len(getattr(self, "bloom_boxes", {})) < len(BLOOMS):
            return
        on = sum(b.isChecked() for b in self.bloom_boxes.values())
        self.bloom_count.setText(f"{on} of {len(BLOOMS)}")
        if hasattr(self, "go"):
            self.go.setEnabled(on > 0)

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

    def choice(self, camera_settings) -> StarterChoice:
        """The answer. `camera_settings(device)` turns the picked camera into source settings."""
        cam = self.camera.currentData()
        monitor = self.screen.currentData()
        return StarterChoice(
            camera=camera_settings(cam) if cam is not None else None,
            monitor=(monitor.index, monitor.width, monitor.height) if monitor is not None else None,
            mic=self.mic.currentData(),
            desktop=self.desktop.currentData(),
            app_sounds=self.app_sounds(),
            blooms=[n for n, b in self.bloom_boxes.items() if b.isChecked()],
            vertical=self.vertical.isChecked(),
            replace_example=self.replace.isChecked() and not self.replace.isHidden(),
        )
