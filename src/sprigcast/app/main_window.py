"""Main window: two canvas previews plus Scenes, Sources, Properties, Audio and Recording docks."""

from __future__ import annotations

import queue
import time
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.audio_dock import AudioPanel
from sprigcast.app.camera_dialog import CameraDialog
from sprigcast.app.preview import Preview
from sprigcast.app.recording_dialog import RecordingDialog
from sprigcast.app.stream_dialog import StreamDialog, output_label
from sprigcast.core.events import AudioLevels, EngineError, ReplaceCollection, SourceInfo, Stats
from sprigcast.core.scene import Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.output.keys import get_key
from sprigcast.output.recorder import RecordingSession, codec_label, pick_codec
from sprigcast.output.stream import SERVICES, StreamSession
from sprigcast.render.engine import Engine
from sprigcast.sources.audio import list_audio_devices
from sprigcast.sources.camera import list_cameras
from sprigcast.sources.win32_enum import list_monitors, list_windows

ROLE_ID = Qt.ItemDataRole.UserRole


def _button(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    return b


class MainWindow(QMainWindow):
    def __init__(self, collection: SceneCollection, scenes_path: Path):
        super().__init__()
        self.collection = collection
        self.scenes_path = scenes_path
        self.selected_scene_id: str | None = collection.active_scenes.get(collection.canvases[0].id)
        self._updating = False  # True while we fill widgets, so their signals do not echo back
        self.source_sizes: dict[str, tuple[int, int]] = {}
        self.source_statuses: dict[str, str] = {}
        self.recording: RecordingSession | None = None
        self.streaming: StreamSession | None = None
        self._stream_status_at = 0.0

        self.setWindowTitle("SprigCast")
        self.resize(1440, 860)

        # Previews ------------------------------------------------------------
        self.previews: dict[str, Preview] = {}
        splitter = QSplitter(Qt.Orientation.Horizontal)
        for canvas in collection.canvases:
            preview = Preview(canvas.id, canvas.id.capitalize(), (canvas.width, canvas.height))
            preview.selected.connect(self._on_preview_selected)
            preview.edited.connect(self._on_preview_edited)
            self.previews[canvas.id] = preview
            splitter.addWidget(preview)
            splitter.setStretchFactor(splitter.count() - 1, 3 if canvas.width >= canvas.height else 1)
        self.setCentralWidget(splitter)

        self._build_scenes_dock()
        self._build_sources_dock()
        self._build_properties_dock()
        self._build_audio_dock()
        self._build_output_dock()
        self._build_menu()
        self.status = QLabel("Starting renderer…")
        self.statusBar().addPermanentWidget(self.status)

        # Engine --------------------------------------------------------------
        self.engine = Engine(collection)
        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_engine)
        self.timer.start(33)  # about 30 preview updates per second

        self._refresh_all()

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree = QTreeWidget()
        self.scene_tree.setHeaderHidden(True)
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self.scene_tree.itemDoubleClicked.connect(lambda *_: self._make_active())
        buttons = QHBoxLayout()
        buttons.addWidget(_button("Show", self._make_active))
        buttons.addWidget(_button("Add", self._add_scene))
        buttons.addWidget(_button("Rename", self._rename_scene))
        buttons.addWidget(_button("Remove", self._remove_scene))
        self._dock("Scenes", self.scene_tree, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_sources_dock(self) -> None:
        self.source_list = QListWidget()
        self.source_list.itemSelectionChanged.connect(self._refresh_properties)
        self.source_list.itemChanged.connect(self._on_source_toggled)
        buttons = QHBoxLayout()
        buttons.addWidget(_button("+ Display", self._add_display))
        buttons.addWidget(_button("+ Window", self._add_window))
        buttons.addWidget(_button("+ Camera", self._add_camera))
        buttons.addWidget(_button("+ Color", self._add_color))
        buttons.addWidget(_button("+ Image", self._add_image))
        buttons.addWidget(_button("Remove", self._remove_item))
        buttons.addWidget(_button("Up", lambda: self._move_item(+1)))
        buttons.addWidget(_button("Down", lambda: self._move_item(-1)))
        self._dock("Sources", self.source_list, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_properties_dock(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        self.prop_name = QLineEdit()
        self.prop_name.editingFinished.connect(self._on_name_edited)
        form.addRow("Name", self.prop_name)
        self.spin: dict[str, QDoubleSpinBox] = {}
        for key, lo, hi, step, decimals in (
            ("x", -10000, 10000, 1, 0),
            ("y", -10000, 10000, 1, 0),
            ("scale", 0.01, 20, 0.05, 2),
            ("rotation", -360, 360, 1, 1),
        ):
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setDecimals(decimals)
            box.valueChanged.connect(lambda value, k=key: self._on_transform_edited(k, value))
            self.spin[key] = box
            form.addRow(key.capitalize(), box)
        self.prop_extra = _button("", self._edit_source_settings)
        form.addRow("Source", self.prop_extra)
        self.prop_fit = _button("Fit to canvas", self._fit_item)
        form.addRow("", self.prop_fit)
        self.prop_status = QLabel("")
        self.prop_status.setWordWrap(True)
        form.addRow("Status", self.prop_status)
        self.prop_locked = QCheckBox("Locked (clicks in the preview pass through it)")
        self.prop_locked.toggled.connect(self._on_locked_toggled)
        form.addRow("", self.prop_locked)
        self._dock("Properties", panel, None, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_audio_dock(self) -> None:
        self.audio_panel = AudioPanel(self._on_audio_changed)
        buttons = QHBoxLayout()
        buttons.addWidget(_button("+ Mic", lambda: self._add_audio("audio_input")))
        buttons.addWidget(_button("+ Desktop", lambda: self._add_audio("audio_output")))
        buttons.addWidget(_button("Device…", self._change_audio_device))
        buttons.addWidget(_button("Remove", self._remove_audio))
        self._dock("Audio", self.audio_panel, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _build_output_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.live_button = QPushButton("Go live")
        self.live_button.setMinimumHeight(40)
        self.live_button.clicked.connect(self._toggle_streaming)
        layout.addWidget(self.live_button)
        self.stream_status = QLabel("Offline")
        self.stream_status.setWordWrap(True)
        layout.addWidget(self.stream_status)
        self.stream_boxes_layout = QVBoxLayout()
        layout.addLayout(self.stream_boxes_layout)
        self.stream_boxes: dict[str, QCheckBox] = {}
        self._refresh_stream_boxes()
        layout.addSpacing(10)

        self.record_button = QPushButton("Start recording")
        self.record_button.setMinimumHeight(40)
        self.record_button.clicked.connect(self._toggle_recording)
        layout.addWidget(self.record_button)
        self.record_status = QLabel("Not recording")
        self.record_status.setWordWrap(True)
        layout.addWidget(self.record_status)
        self.record_canvas_boxes: dict[str, QCheckBox] = {}
        for canvas in self.collection.canvases:
            box = QCheckBox(f"Record {canvas.id} ({canvas.width}×{canvas.height})")
            box.setChecked(canvas.id in self.collection.recording.canvases)
            box.toggled.connect(self._on_record_canvases_changed)
            self.record_canvas_boxes[canvas.id] = box
            layout.addWidget(box)
        layout.addStretch(1)
        buttons = QHBoxLayout()
        self.stream_settings_button = _button("Stream…", self._stream_settings)
        buttons.addWidget(self.stream_settings_button)
        self.record_settings_button = _button("Recording…", self._recording_settings)
        buttons.addWidget(self.record_settings_button)
        buttons.addWidget(_button("Folder", self._open_recordings_folder))
        self._dock("Output", panel, buttons, Qt.DockWidgetArea.BottomDockWidgetArea)

    def _dock(self, title: str, body: QWidget, buttons: QHBoxLayout | None, area) -> None:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(body)
        if buttons is not None:
            layout.addLayout(buttons)
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(wrapper)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable | QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area, dock)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_folder = QAction("Show scenes file location", self)
        open_folder.triggered.connect(
            lambda: QMessageBox.information(self, "SprigCast", f"Scenes are saved to:\n{self.scenes_path}")
        )
        file_menu.addAction(open_folder)
        recordings = QAction("Open recordings folder", self)
        recordings.triggered.connect(self._open_recordings_folder)
        file_menu.addAction(recordings)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    # Engine link ------------------------------------------------------------

    def _poll_engine(self) -> None:
        for canvas_id, mailbox in self.engine.frames.items():
            frame = mailbox.take()
            if frame is not None and canvas_id in self.previews:
                self.previews[canvas_id].set_frame(frame.data, frame.width, frame.height)
        while True:
            try:
                event = self.engine.events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, Stats):
                self.status.setText(
                    f"{event.fps:.0f} fps  ·  render {event.render_ms:.1f} ms  ·  late ticks {event.dropped_ticks}"
                )
            elif isinstance(event, AudioLevels):
                self.audio_panel.set_levels(event.levels)
            elif isinstance(event, SourceInfo):
                self.source_sizes, self.source_statuses = event.sizes, event.statuses
                for preview in self.previews.values():
                    preview.sizes = self.source_sizes
                self._refresh_source_labels()
            elif isinstance(event, EngineError):
                self.statusBar().showMessage(event.message, 10000)
                if "compositor" in event.message:
                    QMessageBox.critical(self, "SprigCast", event.message)
        if self.recording is not None:
            self._refresh_recording_status()
        if self.streaming is not None:
            self._refresh_stream_status()

    def _changed(self, save: bool = True) -> None:
        """Hand a copy of the collection to the engine after every edit, and save it unless mid-drag."""
        if save:
            self.collection.save(self.scenes_path)
        self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True)))

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self.streaming is not None:
            self._stop_streaming()
        if self.recording is not None:
            self._stop_recording()
        self.timer.stop()
        self.engine.stop()
        super().closeEvent(event)

    # Selection helpers -----------------------------------------------------

    def _scene(self) -> Scene | None:
        if self.selected_scene_id is None:
            return None
        try:
            return self.collection.scene(self.selected_scene_id)
        except StopIteration:
            return None

    def _item_index(self) -> int | None:
        """Index into scene.items of the selected row. The list shows the top-most item first."""
        scene, row = self._scene(), self.source_list.currentRow()
        if scene is None or row < 0:
            return None
        return len(scene.items) - 1 - row

    def _item(self) -> SceneItem | None:
        scene, index = self._scene(), self._item_index()
        return None if scene is None or index is None else scene.items[index]

    # Refreshing widgets ------------------------------------------------------

    def _refresh_all(self) -> None:
        self._refresh_audio()
        self._refresh_scenes()
        self._refresh_sources()
        self._refresh_previews()

    def _refresh_previews(self) -> None:
        for canvas_id, preview in self.previews.items():
            scene = self.collection.active_scene(canvas_id)
            preview.subtitle = scene.name if scene else "no scene"
            index = self._item_index() if scene is not None and scene.id == self.selected_scene_id else None
            preview.set_scene(scene, index)

    def _refresh_scenes(self) -> None:
        self._updating = True
        self.scene_tree.clear()
        bold = QFont()
        bold.setBold(True)
        for canvas in self.collection.canvases:
            parent = QTreeWidgetItem([f"{canvas.id.capitalize()}  ({canvas.width}x{canvas.height})"])
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.scene_tree.addTopLevelItem(parent)
            active = self.collection.active_scenes.get(canvas.id)
            for scene in self.collection.scenes_for(canvas.id):
                label = scene.name + ("   (linked)" if scene.linked_to else "")
                child = QTreeWidgetItem([label])
                child.setData(0, ROLE_ID, scene.id)
                if scene.id == active:
                    child.setFont(0, bold)
                    child.setText(0, "▶ " + label)
                parent.addChild(child)
                if scene.id == self.selected_scene_id:
                    child.setSelected(True)
            parent.setExpanded(True)
        self._updating = False

    def _refresh_sources(self, keep_row: int | None = None) -> None:
        self._updating = True
        self.source_list.clear()
        scene = self._scene()
        if scene is not None:
            for item in reversed(scene.items):  # top-most first, like OBS
                row = QListWidgetItem(self._source_label(item.source))
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(Qt.CheckState.Checked if item.visible else Qt.CheckState.Unchecked)
                self.source_list.addItem(row)
        if keep_row is not None and 0 <= keep_row < self.source_list.count():
            self.source_list.setCurrentRow(keep_row)
        self._updating = False
        self._refresh_properties()

    def _source_label(self, source_id: str) -> str:
        config = self.collection.source(source_id)
        label = f"{config.name}   [{config.kind}]"
        status = self.source_statuses.get(source_id, "ok")
        return label if status in ("ok", "capturing") else f"{label}   ({status})"

    def _refresh_source_labels(self) -> None:
        scene = self._scene()
        if scene is None or self.source_list.count() != len(scene.items):
            return
        self._updating = True
        for row, item in enumerate(reversed(scene.items)):
            text = self._source_label(item.source)
            if self.source_list.item(row).text() != text:
                self.source_list.item(row).setText(text)
        self._updating = False
        item = self._item()
        if item is not None:
            self.prop_status.setText(self._status_text(item.source))

    def _status_text(self, source_id: str) -> str:
        size = self.source_sizes.get(source_id)
        status = self.source_statuses.get(source_id, "")
        return f"{status}" + (f"  ·  {size[0]}x{size[1]}" if size and size[0] else "")

    def _refresh_properties(self) -> None:
        item = self._item()
        enabled = item is not None
        self._updating = True
        for box in self.spin.values():
            box.setEnabled(enabled)
        self.prop_name.setEnabled(enabled)
        self.prop_extra.setEnabled(enabled)
        self.prop_fit.setEnabled(enabled)
        self.prop_locked.setEnabled(enabled)
        if item is not None:
            self.prop_locked.setChecked(item.locked)
            config = self.collection.source(item.source)
            self.prop_name.setText(config.name)
            for key, box in self.spin.items():
                box.setValue(float(getattr(item, key)))
            self.prop_extra.setText(
                {
                    "color": "Pick color…",
                    "image": "Choose image…",
                    "display_capture": "Choose monitor…",
                    "window_capture": "Choose window…",
                    "camera": "Camera settings…",
                }.get(config.kind, "Settings…")
            )
            self.prop_status.setText(self._status_text(item.source))
        else:
            self.prop_name.clear()
            self.prop_extra.setText("")
            self.prop_status.setText("")
            self.prop_locked.setChecked(False)
        self._updating = False
        self._refresh_previews()

    # Audio -------------------------------------------------------------------------

    AUDIO_KINDS = ("audio_input", "audio_output")

    def _refresh_audio(self) -> None:
        self.audio_panel.set_sources(
            [
                (s.id, s.name, float(s.settings.get("volume_db", 0.0)), bool(s.settings.get("muted", False)))
                for s in self.collection.sources
                if s.kind in self.AUDIO_KINDS
            ]
        )

    def _on_audio_changed(self, source_id: str, volume_db: float, muted: bool) -> None:
        config = self.collection.source(source_id)
        config.settings = {**config.settings, "volume_db": volume_db, "muted": muted}
        self._changed()

    def _pick_audio_device(self, kind: str, current: str = "default") -> str | None:
        devices = list_audio_devices(loopback=(kind == "audio_output"))
        if not devices:
            QMessageBox.information(self, "SprigCast", "Audio capture is only available on Windows.")
            return None
        labels = ["Default (follows Windows)"] + [d.name for d in devices]
        start = labels.index(current) if current in labels else 0
        title = "Desktop audio" if kind == "audio_output" else "Microphone"
        label, ok = QInputDialog.getItem(self, title, "Device:", labels, start, False)
        if not ok:
            return None
        return "default" if label == labels[0] else label

    def _add_audio(self, kind: str) -> None:
        device = self._pick_audio_device(kind)
        if device is None:
            return
        base = "Desktop audio" if kind == "audio_output" else "Mic"
        name = base if device == "default" else f"{base} ({device})"
        self.collection.sources.append(
            SourceConfig(
                id=self.collection.new_id("desktop" if kind == "audio_output" else "mic"),
                kind=kind,
                name=name,
                settings={"device": device, "volume_db": 0.0, "muted": False},
            )
        )
        self._changed()
        self._refresh_audio()

    def _change_audio_device(self) -> None:
        source_id = self.audio_panel.selected_id()
        if source_id is None:
            QMessageBox.information(self, "SprigCast", "Select an audio source first (click its name).")
            return
        config = self.collection.source(source_id)
        device = self._pick_audio_device(config.kind, str(config.settings.get("device", "default")))
        if device is None:
            return
        config.settings = {**config.settings, "device": device}
        base = "Desktop audio" if config.kind == "audio_output" else "Mic"
        config.name = base if device == "default" else f"{base} ({device})"
        self._changed()
        self._refresh_audio()

    def _remove_audio(self) -> None:
        source_id = self.audio_panel.selected_id()
        if source_id is None:
            QMessageBox.information(self, "SprigCast", "Select an audio source first (click its name).")
            return
        self.collection.sources = [s for s in self.collection.sources if s.id != source_id]
        self._changed()
        self._refresh_audio()

    # Preview editing -------------------------------------------------------------

    def _on_preview_selected(self, canvas_id: str, index: int) -> None:
        """A click in a preview selects that canvas's active scene and the item under the mouse."""
        scene = self.collection.active_scene(canvas_id)
        if scene is None:
            return
        if scene.id != self.selected_scene_id:
            self.selected_scene_id = scene.id
            self._refresh_scenes()
        row = len(scene.items) - 1 - index if index >= 0 else None
        self._refresh_sources(keep_row=row)

    def _on_preview_edited(self, canvas_id: str, finished: bool) -> None:
        self._changed(save=finished)
        item = self._item()
        if item is None:
            return
        self._updating = True
        for key, box in self.spin.items():
            box.setValue(float(getattr(item, key)))
        self._updating = False

    def _on_locked_toggled(self, locked: bool) -> None:
        item = self._item()
        if self._updating or item is None:
            return
        item.locked = locked
        self._changed()

    # Scene actions ---------------------------------------------------------------

    def _on_scene_selected(self) -> None:
        if self._updating:
            return
        selected = self.scene_tree.selectedItems()
        scene_id = selected[0].data(0, ROLE_ID) if selected else None
        if scene_id:
            self.selected_scene_id = scene_id
            self._refresh_sources()

    def _make_active(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        self.collection.set_active(scene.id)
        self._changed()
        self._refresh_scenes()
        self._refresh_previews()

    def _add_scene(self) -> None:
        canvases = [c.id for c in self.collection.canvases]
        canvas_id, ok = QInputDialog.getItem(self, "Add scene", "Canvas:", canvases, 0, False)
        if not ok:
            return
        name, ok = QInputDialog.getText(self, "Add scene", "Scene name:")
        if not ok or not name.strip():
            return
        link_options = ["(not linked)"] + [s.name for s in self.collection.scenes if s.canvas != canvas_id]
        link, ok = QInputDialog.getItem(self, "Add scene", "Switch together with:", link_options, 0, False)
        if not ok:
            return
        linked = next((s.id for s in self.collection.scenes if s.name == link and s.canvas != canvas_id), None)
        scene = Scene(id=self.collection.new_id("scene"), name=name.strip(), canvas=canvas_id, linked_to=linked)
        self.collection.scenes.append(scene)
        self.collection.active_scenes.setdefault(canvas_id, scene.id)
        self.selected_scene_id = scene.id
        self._changed()
        self._refresh_all()

    def _rename_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        name, ok = QInputDialog.getText(self, "Rename scene", "Scene name:", text=scene.name)
        if ok and name.strip():
            scene.name = name.strip()
            self._changed()
            self._refresh_all()

    def _remove_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        if len(self.collection.scenes_for(scene.canvas)) == 1:
            QMessageBox.information(self, "SprigCast", "Each canvas needs at least one scene.")
            return
        if QMessageBox.question(self, "Remove scene", f"Remove {scene.name!r}?") != QMessageBox.StandardButton.Yes:
            return
        self.collection.scenes = [s for s in self.collection.scenes if s.id != scene.id]
        for other in self.collection.scenes:
            if other.linked_to == scene.id:
                other.linked_to = None
        if self.collection.active_scenes.get(scene.canvas) == scene.id:
            self.collection.active_scenes[scene.canvas] = self.collection.scenes_for(scene.canvas)[0].id
        for item in scene.items:
            self.collection.remove_source_if_unused(item.source)
        self.selected_scene_id = self.collection.active_scenes.get(scene.canvas)
        self._changed()
        self._refresh_all()

    # Source actions --------------------------------------------------------------

    def _add_item(self, config: SourceConfig) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        self.collection.sources.append(config)
        scene.items.append(SceneItem(source=config.id))
        self._changed()
        self._refresh_sources(keep_row=0)

    def _add_color(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        color = QColorDialog.getColor(QColor("#9cc89a"), self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return
        canvas = self.collection.canvas(scene.canvas)
        hex_color = "#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha())
        self._add_item(
            SourceConfig(
                id=self.collection.new_id("color"),
                kind="color",
                name=f"Color {hex_color}",
                settings={"color": hex_color, "width": canvas.width, "height": canvas.height},
            )
        )

    def _add_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not path:
            return
        self._add_item(
            SourceConfig(id=self.collection.new_id("image"), kind="image", name=Path(path).stem, settings={"path": path})
        )

    def _pick_monitor(self, current: int | None = None):
        monitors = list_monitors()
        if not monitors:
            QMessageBox.information(self, "SprigCast", "Display capture is only available on Windows.")
            return None
        labels = [m.label for m in monitors]
        start = next((i for i, m in enumerate(monitors) if m.index == current), 0)
        label, ok = QInputDialog.getItem(self, "Display capture", "Monitor:", labels, start, False)
        return monitors[labels.index(label)] if ok else None

    def _pick_window(self):
        windows = list_windows()
        if not windows:
            QMessageBox.information(self, "SprigCast", "Window capture is only available on Windows.")
            return None
        labels = [w.label for w in windows]
        label, ok = QInputDialog.getItem(self, "Window capture", "Window:", labels, 0, False)
        return windows[labels.index(label)] if ok else None

    def _add_display(self) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        monitor = self._pick_monitor()
        if monitor is None:
            return
        config = SourceConfig(
            id=self.collection.new_id("display"),
            kind="display_capture",
            name=f"Monitor {monitor.index}",
            settings={"monitor": monitor.index, "cursor": True},
        )
        self._add_item(config)
        self._fit_item(size=(monitor.width, monitor.height))

    def _add_window(self) -> None:
        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        window = self._pick_window()
        if window is None:
            return
        name = window.title if len(window.title) <= 40 else window.title[:37] + "…"
        self._add_item(
            SourceConfig(
                id=self.collection.new_id("window"),
                kind="window_capture",
                name=name,
                settings={"title": window.title, "process": window.process, "cursor": True},
            )
        )

    def _camera_dialog(self, current: dict | None = None) -> dict | None:
        cameras = list_cameras()
        if not cameras:
            QMessageBox.information(self, "SprigCast", "No cameras found. Is one plugged in and not in use by another app?")
            return None
        dialog = CameraDialog(cameras, current, self)
        return dialog.settings() if dialog.exec() else None

    def _add_camera(self) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        settings = self._camera_dialog()
        if settings is None:
            return
        self._add_item(
            SourceConfig(id=self.collection.new_id("camera"), kind="camera", name=settings["name"], settings=settings)
        )
        # Start as a quarter-width overlay in the bottom-right corner, like a typical facecam.
        item, canvas = self._item(), self.collection.canvas(scene.canvas)
        if item is not None:
            item.scale = (canvas.width * 0.25) / settings["width"]
            item.x = canvas.width - settings["width"] * item.scale - 32
            item.y = canvas.height - settings["height"] * item.scale - 32
            self._changed()
            self._refresh_properties()

    def _fit_item(self, checked: bool = False, size: tuple[int, int] | None = None) -> None:
        """Scale the selected item to fit its canvas and centre it."""
        scene, item = self._scene(), self._item()
        if scene is None or item is None:
            return
        size = size or self.source_sizes.get(item.source)
        if not size or not size[0] or not size[1]:
            self.statusBar().showMessage("That source has no picture yet. Try again in a second.", 4000)
            return
        canvas = self.collection.canvas(scene.canvas)
        w, h = item.cropped_size(size)
        if not w or not h:
            return
        item.scale = min(canvas.width / w, canvas.height / h)
        item.rotation = 0.0
        item.x = (canvas.width - w * item.scale) / 2
        item.y = (canvas.height - h * item.scale) / 2
        self._changed()
        self._refresh_properties()

    def _remove_item(self) -> None:
        scene, index = self._scene(), self._item_index()
        if scene is None or index is None:
            return
        removed = scene.items.pop(index)
        self.collection.remove_source_if_unused(removed.source)
        self._changed()
        self._refresh_sources()

    def _move_item(self, direction: int) -> None:
        """direction +1 moves the item up the list (drawn later, so on top)."""
        scene, index = self._scene(), self._item_index()
        if scene is None or index is None:
            return
        target = index + direction
        if not 0 <= target < len(scene.items):
            return
        scene.items[index], scene.items[target] = scene.items[target], scene.items[index]
        self._changed()
        self._refresh_sources(keep_row=len(scene.items) - 1 - target)

    def _on_source_toggled(self, row: QListWidgetItem) -> None:
        if self._updating:
            return
        scene = self._scene()
        if scene is None:
            return
        index = len(scene.items) - 1 - self.source_list.row(row)
        scene.items[index].visible = row.checkState() == Qt.CheckState.Checked
        self._changed()

    def _on_transform_edited(self, key: str, value: float) -> None:
        if self._updating:
            return
        item = self._item()
        if item is None:
            return
        setattr(item, key, value)
        self._changed()

    def _on_name_edited(self) -> None:
        item = self._item()
        if self._updating or item is None or not self.prop_name.text().strip():
            return
        self.collection.source(item.source).name = self.prop_name.text().strip()
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())

    def _edit_source_settings(self) -> None:
        item = self._item()
        if item is None:
            return
        config = self.collection.source(item.source)
        if config.kind == "color":
            current = QColor(str(config.settings.get("color", "#000000"))[:7])
            color = QColorDialog.getColor(current, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
            if not color.isValid():
                return
            config.settings = {
                **config.settings,
                "color": "#{:02x}{:02x}{:02x}{:02x}".format(color.red(), color.green(), color.blue(), color.alpha()),
            }
        elif config.kind == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
            if not path:
                return
            config.settings = {**config.settings, "path": path}
        elif config.kind == "display_capture":
            monitor = self._pick_monitor(current=int(config.settings.get("monitor", 1)))
            if monitor is None:
                return
            config.settings = {**config.settings, "monitor": monitor.index}
            config.name = f"Monitor {monitor.index}"
        elif config.kind == "camera":
            settings = self._camera_dialog(config.settings)
            if settings is None:
                return
            config.settings = settings
            config.name = settings["name"]
        elif config.kind == "window_capture":
            window = self._pick_window()
            if window is None:
                return
            config.settings = {**config.settings, "title": window.title, "process": window.process}
            config.name = window.title if len(window.title) <= 40 else window.title[:37] + "…"
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())

    # Recording -----------------------------------------------------------------

    def _default_recordings_folder(self) -> str:
        videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        return str(Path(videos or Path.home()) / "SprigCast")

    def _recordings_folder(self) -> Path:
        return Path(self.collection.recording.folder or self._default_recordings_folder())

    def _on_record_canvases_changed(self) -> None:
        self.collection.recording.canvases = [cid for cid, box in self.record_canvas_boxes.items() if box.isChecked()]
        self.collection.save(self.scenes_path)

    def _recording_settings(self) -> None:
        default = self._default_recordings_folder()
        dialog = RecordingDialog(self.collection.recording, default, codec_label(pick_codec()), self)
        if dialog.exec():
            dialog.apply_to(self.collection.recording, default)
            self.collection.save(self.scenes_path)

    def _open_recordings_folder(self) -> None:
        folder = self._recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _toggle_recording(self) -> None:
        if self.recording is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self) -> None:
        settings = self.collection.recording
        canvases = [c for c in self.collection.canvases if c.enabled and c.id in settings.canvases]
        if not canvases:
            QMessageBox.information(self, "SprigCast", "Tick at least one canvas to record.")
            return
        self.record_button.setEnabled(False)
        self.record_status.setText("Starting…")
        try:
            self.recording = RecordingSession.start(
                self.engine, canvases, self._recordings_folder(), settings.bitrate_kbps, settings.container
            )
        except Exception as exc:
            self.recording = None
            QMessageBox.critical(self, "SprigCast", f"Could not start recording:\n{exc}")
            self.record_status.setText("Not recording")
            return
        finally:
            self.record_button.setEnabled(True)
        self.record_button.setText("Stop recording")
        self.record_button.setStyleSheet("background: #c0392b; color: white; font-weight: bold")
        for box in self.record_canvas_boxes.values():
            box.setEnabled(False)
        self.record_settings_button.setEnabled(False)
        self._refresh_recording_status()

    def _stop_recording(self) -> None:
        session, self.recording = self.recording, None
        if session is None:
            return
        self.record_status.setText("Finishing files…")
        self.record_status.repaint()
        session.stop()
        self.record_button.setText("Start recording")
        self.record_button.setStyleSheet("")
        for box in self.record_canvas_boxes.values():
            box.setEnabled(True)
        self.record_settings_button.setEnabled(True)
        errors = session.errors
        names = ", ".join(p.name for p in session.paths)
        if errors:
            self.record_status.setText("Recording failed")
            QMessageBox.warning(self, "SprigCast", "Recording stopped with an error:\n" + "\n".join(errors))
        else:
            self.record_status.setText(f"Saved {names}")

    def _refresh_recording_status(self) -> None:
        session = self.recording
        if session.errors:
            self._stop_recording()
            return
        seconds = int(session.elapsed)
        clock = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        codec = "NVENC" if session.recorders and session.recorders[0][1].codec == "h264_nvenc" else "x264 (CPU)"
        dropped = session.dropped
        text = f"● REC {clock}  ·  {codec}"
        if dropped:
            text += f"  ·  {dropped} frames dropped (encoder too slow)"
        self.record_status.setText(text)

    # Streaming -----------------------------------------------------------------

    def _refresh_stream_boxes(self) -> None:
        while self.stream_boxes_layout.count():
            widget = self.stream_boxes_layout.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()
        self.stream_boxes = {}
        outputs = [o for o in self.collection.outputs if o.type == "stream"]
        if not outputs:
            hint = QLabel("No stream outputs yet: press Stream… to add Twitch, YouTube or TikTok.")
            hint.setWordWrap(True)
            self.stream_boxes_layout.addWidget(hint)
        for output in outputs:
            box = QCheckBox(f"Stream to {output_label(output)}")
            box.setChecked(output.enabled)
            box.toggled.connect(lambda on, oid=output.id: self._on_stream_toggled(oid, on))
            self.stream_boxes[output.id] = box
            self.stream_boxes_layout.addWidget(box)

    def _on_stream_toggled(self, output_id: str, on: bool) -> None:
        for output in self.collection.outputs:
            if output.id == output_id:
                output.enabled = on
        self.collection.save(self.scenes_path)

    def _stream_settings(self) -> None:
        dialog = StreamDialog(self.collection, self)
        if dialog.exec() and dialog.apply_to(self.collection):
            self.collection.save(self.scenes_path)
            self._refresh_stream_boxes()

    def _toggle_streaming(self) -> None:
        if self.streaming is None:
            self._start_streaming()
        else:
            self._stop_streaming()

    def _start_streaming(self) -> None:
        outputs = [o for o in self.collection.outputs if o.type == "stream" and o.enabled]
        if not outputs:
            QMessageBox.information(self, "SprigCast", "Tick at least one place to stream to, or add one with Stream….")
            return
        targets, problems = [], []
        for output in outputs:
            service = SERVICES.get(output.service or "custom", SERVICES["custom"])
            server = service.server or output.server
            key = get_key(output.key_ref)
            canvas = next((c for c in self.collection.canvases if c.id == output.canvas and c.enabled), None)
            if not server:
                problems.append(f"{service.label}: paste the server URL in Stream…")
            elif not key:
                problems.append(f"{service.label}: add your stream key in Stream…")
            elif canvas is None:
                problems.append(f"{service.label}: the {output.canvas} canvas is not available")
            else:
                targets.append((f"{service.label} ({canvas.id})", canvas, server, key, output.bitrate_kbps))
        if problems:
            QMessageBox.warning(self, "SprigCast", "Not ready to go live:\n\n" + "\n".join(problems))
            return
        self.live_button.setEnabled(False)
        self.stream_status.setText("Connecting…")
        try:
            self.streaming = StreamSession.start(self.engine, targets, pick_codec())
        except Exception as exc:
            self.streaming = None
            QMessageBox.critical(self, "SprigCast", f"Could not go live:\n{exc}")
            self.stream_status.setText("Offline")
            return
        finally:
            self.live_button.setEnabled(True)
        self.live_button.setText("End stream")
        self.live_button.setStyleSheet("background: #c0392b; color: white; font-weight: bold")
        for box in self.stream_boxes.values():
            box.setEnabled(False)
        self.stream_settings_button.setEnabled(False)
        self._stream_status_at = 0.0
        self._refresh_stream_status()

    def _stop_streaming(self) -> None:
        session, self.streaming = self.streaming, None
        if session is None:
            return
        self.stream_status.setText("Ending stream…")
        self.stream_status.repaint()
        session.stop()
        self.live_button.setText("Go live")
        self.live_button.setStyleSheet("")
        for box in self.stream_boxes.values():
            box.setEnabled(True)
        self.stream_settings_button.setEnabled(True)
        seconds = int(session.elapsed)
        self.stream_status.setText(f"Offline (last stream {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d})")

    def _refresh_stream_status(self) -> None:
        now = time.monotonic()
        if now - self._stream_status_at < 1.0:
            return  # bitrate is measured over about a second
        self._stream_status_at = now
        session = self.streaming
        seconds = int(session.elapsed)
        lines = [f"● LIVE {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"]
        for _, st in session.streamers:
            kbps = st.measure_kbps()
            if st.connected:
                line = f"{st.name}: live, {kbps:.0f} kbps"
                if st.dropped:
                    line += f", {st.dropped} frames dropped"
            else:
                line = f"{st.name}: {st.status}"
            lines.append(line)
        self.stream_status.setText("\n".join(lines))
