"""Starter kit: a ready set of blooms from a few choices, so a first stream needs no building.

    Camera  [Elgato Facecam ▾]     Screen  [Monitor 1 ▾]     Mic  [Default ▾]   ☑ Desktop sound
    Blooms  ☑ Starting soon  ☑ Just Chatting  ☑ Gaming  ☑ Be right back  ☑ Ending
    ☑ Vertical versions too

Makes a "Main Scenes" folder (the Live folder) on each shape, a Webcam sprig used by the blooms
with a camera, and adds the mic and desktop sound once (sound plays in every bloom on both shapes).
`build_starter` does the work and is plain data in, data out, so it's tested without a screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.core.scene import PART_KIND, Scene, SceneCollection, SceneItem, SourceConfig

BLOOMS = ("Starting soon", "Just Chatting", "Gaming", "Be right back", "Ending")
TITLES = {"Starting soon": "Starting soon", "Be right back": "Be right back", "Ending": "Thanks for watching!"}
WITH_CAMERA = {"Just Chatting", "Gaming"}
BACKGROUND = "#16241dff"
DEFAULT_IDS = {"starting", "starting-v", "brb", "brb-v"}  # the example blooms a fresh install starts with


@dataclass
class StarterChoice:
    camera: dict | None = None  # camera source settings (name, path, width, height, fps, mirror)
    monitor: tuple[int, int, int] | None = None  # index, width, height
    mic: str | None = "default"  # device name, "default", or None for no mic
    desktop: bool = True
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
            if name in WITH_CAMERA and placement is not None:
                fit = min(canvas.width / main.width, canvas.height / main.height)
                items.append(SceneItem(source=placement.id, scale=fit))
            if name in TITLES:
                title = SourceConfig(id=collection.new_id("text"), kind="text", name=TITLES[name],
                                     settings={"text": TITLES[name], "size": max(48, canvas.width // 18)})
                collection.sources.append(title)
                items.append(SceneItem(source=title.id, x=canvas.width * 0.08, y=canvas.height * 0.42))
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
    if choice.desktop:
        _add_sound(collection, "audio_output", "default", "Desktop audio")
    return made


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
    collection.scenes = [s for s in collection.scenes if s.id not in gone]
    collection.active_scenes = {c: s for c, s in collection.active_scenes.items() if s not in gone}
    used = {i.source for s in collection.scenes for i in s.items}
    collection.sources = [c for c in collection.sources
                          if c.id in used or c.kind in ("audio_input", "audio_output")]
    collection.transition_rules = [r for r in collection.transition_rules
                                   if r.from_scene not in gone and r.to_scene not in gone]


class StarterDialog(QDialog):
    """The few choices; the main window lists the devices and calls build_starter with the answer."""

    def __init__(self, cameras: list, monitors: list, mics: list[str], fresh: bool, both_shapes: bool,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Starter kit")
        self.setMinimumWidth(520)
        self.cameras, self.monitors = cameras, monitors
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 22)
        root.setSpacing(14)
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
        form.setVerticalSpacing(10)
        self.camera = QComboBox()
        self.camera.addItem("No camera", None)
        for cam in cameras:
            self.camera.addItem(cam.name, cam)
        if cameras:
            self.camera.setCurrentIndex(1)
        form.addRow("Camera", self.camera)
        self.screen = QComboBox()
        self.screen.addItem("No screen", None)
        for monitor in monitors:
            self.screen.addItem(monitor.label, monitor)
        if monitors:
            self.screen.setCurrentIndex(1 + next((i for i, m in enumerate(monitors) if m.primary), 0))
        form.addRow("Screen (for Gaming)", self.screen)
        self.mic = QComboBox()
        self.mic.addItem("Default mic (follows Windows)", "default")
        for name in mics:
            self.mic.addItem(name, name)
        self.mic.addItem("No mic", None)
        form.addRow("Mic", self.mic)
        root.addLayout(form)
        self.desktop = QCheckBox("Desktop sound (games, music, alerts)")
        self.desktop.setChecked(True)
        root.addWidget(self.desktop)
        blooms = QLabel("Blooms to make")
        blooms.setStyleSheet(f"color: {theme.MUTED}; font-weight: 600;")
        root.addWidget(blooms)
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)
        self.bloom_boxes: dict[str, QCheckBox] = {}
        for i, name in enumerate(BLOOMS):
            box = QCheckBox(name)
            box.setChecked(True)
            self.bloom_boxes[name] = box
            grid.addWidget(box, i // 3, i % 3)
        root.addLayout(grid)
        self.vertical = QCheckBox("Vertical versions too (TikTok, Shorts, Reels)")
        self.vertical.setChecked(both_shapes)
        self.vertical.setVisible(both_shapes)
        root.addWidget(self.vertical)
        self.replace = QCheckBox("Replace the example blooms")
        self.replace.setChecked(fresh)
        self.replace.setVisible(fresh)
        root.addWidget(self.replace)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        skip = QPushButton("Not now")
        skip.setStyleSheet(theme.SMALL_PILL_STYLE)
        skip.clicked.connect(self.reject)
        go = QPushButton("Make my blooms")
        go.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 17px; padding: 9px 20px; }")
        go.setDefault(True)
        go.clicked.connect(self.accept)
        for b in (skip, go):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(b)
        root.addLayout(buttons)

    def choice(self, camera_settings) -> StarterChoice:
        """The answer. `camera_settings(device)` turns the picked camera into source settings."""
        cam = self.camera.currentData()
        monitor = self.screen.currentData()
        return StarterChoice(
            camera=camera_settings(cam) if cam is not None else None,
            monitor=(monitor.index, monitor.width, monitor.height) if monitor is not None else None,
            mic=self.mic.currentData(),
            desktop=self.desktop.isChecked(),
            blooms=[n for n, b in self.bloom_boxes.items() if b.isChecked()],
            vertical=not self.vertical.isHidden() and self.vertical.isChecked(),
            replace_example=self.replace.isChecked() and not self.replace.isHidden(),
        )
