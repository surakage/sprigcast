"""Main window: two canvas previews plus Scenes, Sources, Properties, Audio and Recording docks."""

from __future__ import annotations

import html
import logging
import queue
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QApplication,
    QComboBox,
    QFrame,
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
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.audio_dock import AudioPanel
from sprigcast.app.camera_dialog import CameraDialog
from sprigcast.app.hotkeys import SCENE_PREFIX, TOGGLE_RECORD, TOGGLE_STREAM, Hotkeys
from sprigcast.app.preview import Preview
from sprigcast.app.settings_dialog import SettingsDialog
from sprigcast.app.stats_panel import StatsPanel
from sprigcast.app.settings_window import CONFIRM_RECORDING, CONFIRM_STREAM, SettingsWindow, confirm_enabled
from sprigcast.app.stream_dialog import output_label
from sprigcast.core.events import AudioLevels, EngineError, ReplaceCollection, SourceInfo, Stats
from sprigcast.core.scene import Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import known_kinds, source_class
from sprigcast.output.keys import get_key
from sprigcast.output.recorder import RecordingSession, codec_label, pick_codec
from sprigcast.output.stream import SERVICES, StreamSession, StreamTarget
from sprigcast.render.engine import Engine
from sprigcast.sources.audio import list_audio_devices
from sprigcast.sources.camera import list_cameras
from sprigcast.sources.win32_enum import list_monitors, list_windows

ROLE_ID = Qt.ItemDataRole.UserRole
log = logging.getLogger(__name__)


# Kinds that have their own button or dock; everything else with a `label` goes under + → Plugins.
BUILT_IN_KINDS = {"display_capture", "window_capture", "camera", "color", "image", "text", "audio_input", "audio_output"}


def _name_from(settings: dict, fallback: str) -> str:
    """A text source is named after its first line, so the sources list is readable."""
    first = str(settings.get("text", "")).strip().splitlines()
    if not first:
        return fallback
    return first[0] if len(first[0]) <= 30 else first[0][:29] + "…"


def _toolbar(*entries) -> QWidget:
    """A row of small buttons under a list, like OBS. Each entry is (text, tooltip, slot or QMenu); None is a gap."""
    bar = QWidget()
    row = QHBoxLayout(bar)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(2)
    for entry in entries:
        if entry is None:
            row.addStretch(1)
            continue
        text, tip, target = entry
        b = QToolButton()
        b.setText(text)
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.setMinimumSize(30, 28)
        if len(text) <= 2:  # symbol buttons (+ − ⚙ ▲ ▼) read better a bit larger
            font = b.font()
            font.setPointSizeF(font.pointSizeF() * 1.35)
            b.setFont(font)
        if isinstance(target, QMenu):
            b.setMenu(target)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        else:
            b.clicked.connect(lambda _=False, f=target: f())
        row.addWidget(b)
    if entries and entries[-1] is not None and None not in entries:
        row.addStretch(1)
    return bar


def _button(text: str, slot) -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(slot)
    return b


class MainWindow(QMainWindow):
    def __init__(self, collection: SceneCollection, scenes_path: Path, plugins_dir: Path | None = None):
        super().__init__()
        self.collection = collection
        self.scenes_path = scenes_path
        self.plugins_dir = plugins_dir
        self.plugin_report = None  # set by main.run() after loading plugins
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

        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
        )
        self.docks: dict[str, QDockWidget] = {}
        self._build_scenes_dock()
        self._build_sources_dock()
        self._build_audio_dock()
        self._build_transitions_dock()
        self._build_controls_dock()
        self._build_properties_dock()
        self.stats_panel = StatsPanel()
        self.stats_dock = self._dock("Stats", self.stats_panel)
        self._last_stats = None
        self._arrange_default_layout()
        self._build_menu()
        self.status = QLabel("Starting renderer…")
        self.statusBar().addPermanentWidget(self.status)

        # Engine --------------------------------------------------------------
        self.engine = Engine(collection)
        self.engine.start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_engine)
        self.timer.start(33)  # about 30 preview updates per second

        self.hotkeys = Hotkeys(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.apply(self.collection.hotkeys)

        self._refresh_all()
        self._restore_layout()

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree = QTreeWidget()
        self.scene_tree.setHeaderHidden(True)
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self.scene_tree.itemDoubleClicked.connect(lambda *_: self._make_active())
        self.scene_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scene_tree.customContextMenuRequested.connect(self._scene_context_menu)
        bar = _toolbar(
            ("+", "Add a scene", self._add_scene),
            ("−", "Remove the selected scene", self._remove_scene),
            ("✎", "Rename the selected scene", self._rename_scene),
            None,
            ("▶ Show", "Show this scene on its canvas (or double-click it)", self._make_active),
        )
        self._dock("Scenes", self.scene_tree, bar)

    def _build_transitions_dock(self) -> None:
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(0, 0, 0, 0)
        self.transition_kind = QComboBox()
        self.transition_kind.addItem("Fade", "fade")
        self.transition_kind.addItem("Cut", "cut")
        self.transition_kind.setCurrentIndex(max(0, self.transition_kind.findData(self.collection.transition.kind)))
        self.transition_kind.currentIndexChanged.connect(self._on_transition_changed)
        form.addRow("Type", self.transition_kind)
        self.transition_ms = QSpinBox()
        self.transition_ms.setRange(50, 5000)
        self.transition_ms.setSingleStep(50)
        self.transition_ms.setSuffix(" ms")
        self.transition_ms.setValue(self.collection.transition.duration_ms)
        self.transition_ms.setEnabled(self.collection.transition.kind == "fade")
        self.transition_ms.valueChanged.connect(self._on_transition_changed)
        form.addRow("Duration", self.transition_ms)
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)
        layout.addStretch(1)
        self._dock("Scene Transitions", wrapper)

    def _on_transition_changed(self) -> None:
        self.collection.transition.kind = self.transition_kind.currentData()
        self.collection.transition.duration_ms = self.transition_ms.value()
        self.transition_ms.setEnabled(self.collection.transition.kind == "fade")
        self._changed()

    # Window layout ---------------------------------------------------------------

    def _restore_layout(self) -> None:
        """Put the window and docks back where they were last time (if the screen still fits them)."""
        settings = QSettings()
        geometry = settings.value("window/geometry")
        state = settings.value("window/state")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if state is not None:
            self.restoreState(state, self.LAYOUT_VERSION)
        self._set_docks_locked(QSettings().value("window/locked", False, type=bool))

    def _save_layout(self) -> None:
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/state", self.saveState(self.LAYOUT_VERSION))

    def _build_sources_dock(self) -> None:
        self.source_list = QListWidget()
        self.source_list.itemSelectionChanged.connect(self._refresh_properties)
        self.source_list.itemChanged.connect(self._on_source_toggled)
        self.source_list.itemDoubleClicked.connect(lambda *_: self._edit_source_settings())
        self.source_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.source_list.customContextMenuRequested.connect(self._source_context_menu)
        self.add_source_menu = QMenu(self)
        self.add_source_menu.aboutToShow.connect(self._fill_add_source_menu)
        bar = _toolbar(
            ("+", "Add a source", self.add_source_menu),
            ("−", "Remove the selected source", self._remove_item),
            ("⚙", "Source settings (or double-click it)", self._edit_source_settings),
            ("▲", "Move up (in front)", lambda: self._move_item(+1)),
            ("▼", "Move down (behind)", lambda: self._move_item(-1)),
            None,
            ("Transform", "Position, scale, rotation and lock", self._show_transform),
        )
        self._dock("Sources", self.source_list, bar)

    def _fill_add_source_menu(self) -> None:
        menu = self.add_source_menu
        menu.clear()
        for label, slot in (
            ("Display capture", self._add_display),
            ("Window capture", self._add_window),
            ("Camera", self._add_camera),
            ("Image", self._add_image),
            ("Color", self._add_color),
            ("Text", self._add_text),
        ):
            menu.addAction(label).triggered.connect(lambda _=False, f=slot: f())
        plugins = menu.addMenu("Plugins")
        self.more_menu = plugins
        self._fill_more_menu()

    def _source_context_menu(self, pos) -> None:
        row = self.source_list.itemAt(pos)
        menu = QMenu(self)
        if row is None:
            self._fill_add_source_menu()
            menu.addMenu(self.add_source_menu).setText("Add")
        else:
            item = self._item()
            menu.addAction("Settings…").triggered.connect(self._edit_source_settings)
            menu.addAction("Transform…").triggered.connect(self._show_transform)
            menu.addAction("Fit to canvas").triggered.connect(self._fit_item)
            lock = menu.addAction("Lock")
            lock.setCheckable(True)
            lock.setChecked(bool(item and item.locked))
            lock.toggled.connect(lambda on: self.prop_locked.setChecked(on))
            menu.addAction("Rename…").triggered.connect(self._rename_item)
            menu.addSeparator()
            menu.addAction("Move up").triggered.connect(lambda: self._move_item(+1))
            menu.addAction("Move down").triggered.connect(lambda: self._move_item(-1))
            menu.addSeparator()
            menu.addAction("Remove").triggered.connect(self._remove_item)
        menu.exec(self.source_list.viewport().mapToGlobal(pos))

    def _scene_context_menu(self, pos) -> None:
        menu = QMenu(self)
        if self.scene_tree.itemAt(pos) is not None and self._scene() is not None:
            menu.addAction("Show").triggered.connect(self._make_active)
            menu.addAction("Rename…").triggered.connect(self._rename_scene)
            menu.addAction("Remove").triggered.connect(self._remove_scene)
            menu.addSeparator()
        menu.addAction("Add scene…").triggered.connect(self._add_scene)
        menu.exec(self.scene_tree.viewport().mapToGlobal(pos))

    def _rename_item(self) -> None:
        item = self._item()
        if item is None:
            return
        config = self.collection.source(item.source)
        name, ok = QInputDialog.getText(self, "Rename source", "Name:", text=config.name)
        if ok and name.strip():
            config.name = name.strip()
            self._changed()
            self._refresh_sources(keep_row=self.source_list.currentRow())

    def _show_transform(self) -> None:
        dock = self.docks["Properties"]
        dock.show()
        dock.raise_()

    def _build_properties_dock(self) -> None:
        """Name, position, scale, rotation and lock of the selected item. Hidden until asked for, like OBS."""
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
        row = QHBoxLayout()
        self.prop_extra = _button("", self._edit_source_settings)
        row.addWidget(self.prop_extra)
        self.prop_fit = _button("Fit to canvas", self._fit_item)
        row.addWidget(self.prop_fit)
        form.addRow("", row)
        self.prop_locked = QCheckBox("Locked (clicks in the preview pass through it)")
        self.prop_locked.toggled.connect(self._on_locked_toggled)
        form.addRow("", self.prop_locked)
        self.prop_status = QLabel("")
        self.prop_status.setWordWrap(True)
        form.addRow("Status", self.prop_status)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        self._dock("Properties", scroll)

    def _build_audio_dock(self) -> None:
        self.audio_panel = AudioPanel(self._on_audio_changed)
        add_menu = QMenu(self)
        add_menu.addAction("Microphone").triggered.connect(lambda: self._add_audio("audio_input"))
        add_menu.addAction("Desktop audio").triggered.connect(lambda: self._add_audio("audio_output"))
        bar = _toolbar(
            ("+", "Add a microphone or desktop audio", add_menu),
            ("−", "Remove the selected audio source (click its name first)", self._remove_audio),
            ("⚙", "Change the selected source's device", self._change_audio_device),
        )
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.audio_panel)
        self._dock("Audio Mixer", scroll, bar)

    def _build_controls_dock(self) -> None:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.live_button = QPushButton("Go live")
        self.live_button.setMinimumHeight(34)
        self.live_button.clicked.connect(self._on_live_clicked)
        layout.addWidget(self.live_button)
        self.record_button = QPushButton("Start recording")
        self.record_button.setMinimumHeight(34)
        self.record_button.clicked.connect(self._on_record_clicked)
        layout.addWidget(self.record_button)

        self.settings_button = _button("Settings", lambda: self._open_settings())
        layout.addWidget(self.settings_button)

        # Which outputs go live / which canvases record, then live status, folded away by default.
        self.stream_status = QLabel("Offline")
        self.stream_status.setWordWrap(True)
        layout.addWidget(self.stream_status)
        self.stream_boxes_layout = QVBoxLayout()
        self.stream_boxes_layout.setSpacing(2)
        layout.addLayout(self.stream_boxes_layout)
        self.stream_boxes: dict[str, QCheckBox] = {}
        self._refresh_stream_boxes()
        self.record_status = QLabel("Not recording")
        self.record_status.setWordWrap(True)
        layout.addWidget(self.record_status)
        self.record_canvas_boxes: dict[str, QCheckBox] = {}
        for canvas in self.collection.canvases:
            box = QCheckBox(f"Record {canvas.id}")
            box.setToolTip(f"{canvas.width}×{canvas.height}")
            box.setChecked(canvas.id in self.collection.recording.canvases)
            box.toggled.connect(self._on_record_canvases_changed)
            self.record_canvas_boxes[canvas.id] = box
            layout.addWidget(box)
        layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(panel)
        self._dock("Controls", scroll)

    def _dock(self, title: str, body: QWidget, toolbar: QWidget | None = None) -> QDockWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)
        layout.addWidget(body, 1)
        if toolbar is not None:
            layout.addWidget(toolbar)
        dock = QDockWidget(title, self)
        dock.setObjectName(title)
        dock.setWidget(wrapper)
        dock.setMinimumWidth(160)
        self.docks[title] = dock
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        return dock

    # Dock layout -----------------------------------------------------------------

    LAYOUT_VERSION = 2  # bump when the set of docks changes, so an old saved layout is ignored

    def _arrange_default_layout(self) -> None:
        """OBS-style: Scenes | Sources | Audio Mixer | Scene Transitions | Controls along the bottom."""
        order = ["Scenes", "Sources", "Audio Mixer", "Scene Transitions", "Controls"]
        for dock in self.docks.values():
            dock.setFloating(False)
            dock.show()
            self.removeDockWidget(dock)
        first = self.docks[order[0]]
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, first)
        first.show()
        for prev, name in zip(order, order[1:]):
            self.splitDockWidget(self.docks[prev], self.docks[name], Qt.Orientation.Horizontal)
            self.docks[name].show()
        for name in ("Properties", "Stats"):
            self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.docks[name])
            self.docks[name].hide()
        self.resizeDocks([self.docks[n] for n in order], [220, 280, 360, 180, 220], Qt.Orientation.Horizontal)
        self.resizeDocks([first], [300], Qt.Orientation.Vertical)

    def _reset_layout(self) -> None:
        self._set_docks_locked(False)
        self._arrange_default_layout()

    def _set_docks_locked(self, locked: bool) -> None:
        features = QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
        if not locked:
            features = (
                QDockWidget.DockWidgetFeature.DockWidgetMovable
                | QDockWidget.DockWidgetFeature.DockWidgetFloatable
                | QDockWidget.DockWidgetFeature.DockWidgetClosable
            )
        for dock in self.docks.values():
            dock.setFeatures(features)
        if self.lock_action.isChecked() != locked:
            self.lock_action.setChecked(locked)
        QSettings().setValue("window/locked", locked)

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
        plugins = QAction("Open plugins folder", self)
        plugins.triggered.connect(self._open_plugins_folder)
        plugins.setEnabled(self.plugins_dir is not None)
        file_menu.addAction(plugins)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        settings_menu = self.menuBar().addMenu("&Settings")
        open_settings = settings_menu.addAction("Settings…")
        open_settings.setShortcut(QKeySequence("Ctrl+,"))
        open_settings.triggered.connect(lambda: self._open_settings())
        settings_menu.addSeparator()
        for tab, label in (
            ("Stream", "Stream outputs…"),
            ("Recording", "Recording…"),
            ("Audio", "Audio devices…"),
            ("Video", "Canvases…"),
            ("Hotkeys", "Hotkeys…"),
        ):
            settings_menu.addAction(label).triggered.connect(lambda _=False, t=tab: self._open_settings(t))

        docks_menu = self.menuBar().addMenu("&Docks")
        for name in ["Scenes", "Sources", "Audio Mixer", "Scene Transitions", "Controls", "Properties", "Stats"]:
            action = self.docks[name].toggleViewAction()
            if name == "Stats":
                action.setShortcut(QKeySequence("Ctrl+Shift+S"))
            docks_menu.addAction(action)
        docks_menu.addSeparator()
        self.lock_action = QAction("Lock docks", self)
        self.lock_action.setCheckable(True)
        self.lock_action.toggled.connect(self._set_docks_locked)
        docks_menu.addAction(self.lock_action)
        reset = QAction("Reset layout", self)
        reset.triggered.connect(self._reset_layout)
        docks_menu.addAction(reset)

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
                self._last_stats = event
                if self.stats_dock.isVisible():
                    self._refresh_stats_panel()
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
        self._save_layout()
        self.hotkeys.stop()
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
                    "text": "Edit text…",
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

    def _add_text(self) -> None:
        self._add_from_class("text", "Text")

    def _add_from_class(self, kind: str, name: str) -> None:
        """Add a source whose settings form is described by its class's `defaults` (text and plugins)."""
        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        cls = source_class(kind)
        defaults = dict(getattr(cls, "defaults", {}) or {})
        dialog = SettingsDialog(f"New {name}", defaults, defaults, self)
        if not dialog.exec():
            return
        settings = dialog.values()
        scene = self._scene()
        self._add_item(
            SourceConfig(id=self.collection.new_id(kind), kind=kind, name=_name_from(settings, name), settings=settings)
        )
        # Start each new one a little lower so several don't pile up in the corner.
        canvas = self.collection.canvas(scene.canvas)
        stacked = sum(1 for i in scene.items[:-1] if self.collection.source(i.source).kind == kind)
        scene.items[-1].x = round(canvas.width * 0.05)
        scene.items[-1].y = round(canvas.height * 0.05) + (stacked * 100) % max(1, canvas.height // 2)
        self._changed()
        self._refresh_properties()

    def _fill_more_menu(self) -> None:
        """List source kinds from plugins (any registered class with a `label` that has no button of its own)."""
        self.more_menu.clear()
        entries = sorted(
            (getattr(source_class(kind), "label", None), kind)
            for kind in known_kinds()
            if kind not in BUILT_IN_KINDS and getattr(source_class(kind), "label", None)
        )
        for label, kind in entries:
            action = self.more_menu.addAction(label)
            action.triggered.connect(lambda _=False, k=kind, n=label: self._add_from_class(k, n))
        if not entries:
            empty = self.more_menu.addAction("No plugin sources installed")
            empty.setEnabled(False)
        self.more_menu.addSeparator()
        folder = self.more_menu.addAction("Open plugins folder…")
        folder.setEnabled(self.plugins_dir is not None)
        folder.triggered.connect(self._open_plugins_folder)

    def _open_plugins_folder(self) -> None:
        if self.plugins_dir is None:
            return
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.plugins_dir)))

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
        else:
            cls = source_class(config.kind)
            defaults = getattr(cls, "defaults", None) if cls is not None else None
            if not defaults:
                QMessageBox.information(self, "SprigCast", f"{config.kind!r} sources have no settings to edit.")
                return
            dialog = SettingsDialog(config.name, defaults, config.settings, self)
            if not dialog.exec():
                return
            old_auto = _name_from(config.settings, config.name)
            config.settings = dialog.values()
            if config.name == old_auto:  # the name followed the text, so keep it following
                config.name = _name_from(config.settings, config.name)
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

    def _open_recordings_folder(self) -> None:
        folder = self._recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _on_record_clicked(self) -> None:
        if self.recording is not None and confirm_enabled(CONFIRM_RECORDING):
            answer = QMessageBox.question(self, "SprigCast", "Stop recording?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._toggle_recording()

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
            hint = QLabel("No stream outputs yet: use Settings → Stream outputs… to add Twitch, YouTube or TikTok.")
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

    def _on_live_clicked(self) -> None:
        if self.streaming is not None and confirm_enabled(CONFIRM_STREAM):
            answer = QMessageBox.question(self, "SprigCast", "End the stream?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._toggle_streaming()

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
            if output.twitch_account and output.service == "twitch":
                key = self._fresh_twitch_key(output, key)
            canvas = next((c for c in self.collection.canvases if c.id == output.canvas and c.enabled), None)
            if not server:
                problems.append(f"{service.label}: paste the server URL in Stream…")
            elif not key:
                problems.append(f"{service.label}: add your stream key in Stream…")
            elif canvas is None:
                problems.append(f"{service.label}: the {output.canvas} canvas is not available")
            else:
                name = f"{service.label} ({canvas.id})" + (" test" if output.bandwidth_test and service.bandwidth_test else "")
                target = StreamTarget(name, canvas, server, key, output.bitrate_kbps, output.size, output.fps,
                                      output.bandwidth_test and service.bandwidth_test)
                if output.enhanced and output.service == "twitch":
                    target = self._enhanced_target(output, target)
                    if target is None:
                        return  # the user chose not to go live
                targets.append(target)
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
        self._stream_status_at = 0.0
        self._refresh_stream_status()

    def _enhanced_target(self, output, target):
        """Ask Twitch which qualities to send. Returns the target to use, or None to cancel going live."""
        from sprigcast import __version__
        from sprigcast.output import enhanced

        canvases = [target.canvas]
        if output.enhanced_vertical:
            vertical = next((c for c in self.collection.canvases if c.enabled and c.height > c.width
                             and c.id != target.canvas.id), None)
            if vertical is not None:
                canvases.append(vertical)
        self.stream_status.setText("Asking Twitch for the Enhanced Broadcasting settings…")
        self.stream_status.repaint()
        try:
            request = enhanced.build_request(
                target.key, [enhanced.CanvasInfo(c.width, c.height, c.fps) for c in canvases], __version__,
                max_tracks=output.enhanced_max_tracks or None,
            )
            config = enhanced.parse_config(enhanced.fetch_config(request), target.key, pick_codec(),
                                           bandwidth_test=target.bandwidth_test)
        except Exception as exc:
            log.warning("Enhanced Broadcasting unavailable: %s", exc)
            answer = QMessageBox.question(
                self, "SprigCast",
                f"Enhanced Broadcasting isn't available right now:\n\n{exc}\n\n"
                "Go live with a single quality instead?",
            )
            self.stream_status.setText("Offline")
            return target if answer == QMessageBox.StandardButton.Yes else None
        if config.warning:
            answer = QMessageBox.question(self, "SprigCast", f"Twitch says:\n\n{config.warning}\n\nGo live anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                self.stream_status.setText("Offline")
                return None
        log.info("Enhanced Broadcasting: %s", config.describe())
        vertical = any(t.canvas_index > 0 for t in config.tracks)
        target.name = f"Twitch Enhanced ({len(config.tracks)} qualities{', + vertical' if vertical else ''})"
        target.enhanced, target.canvases = config, canvases
        return target

    def _fresh_twitch_key(self, output, saved: str | None) -> str | None:
        """Ask Twitch for the current key (it changes if you reset it); fall back to the saved one."""
        from sprigcast.output import twitch_auth
        from sprigcast.output.keys import set_key

        self.stream_status.setText("Getting your stream key from Twitch…")
        self.stream_status.repaint()
        try:
            key = twitch_auth.fetch_stream_key()
        except Exception as exc:
            log.warning("could not fetch the Twitch stream key: %s", exc)
            self.statusBar().showMessage(f"{exc} Using the saved stream key.", 15000)
            return saved
        if key != saved and output.key_ref:
            try:
                set_key(output.key_ref, key)
            except Exception as exc:
                log.warning("could not save the fetched Twitch key: %s", exc)
        return key

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
        seconds = int(session.elapsed)
        lines = [f"Offline. Last stream {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"]
        lines += [st.summary() for _, st in session.streamers]
        self.stream_status.setText("\n".join(lines))
        self.stream_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for line in lines[1:]:
            log.info("stream summary: %s", line)

    def _refresh_stream_status(self) -> None:
        now = time.monotonic()
        if now - self._stream_status_at < 1.0:
            return  # bitrate is measured over about a second
        self._stream_status_at = now
        session = self.streaming
        seconds = int(session.elapsed)
        lines = [f"● LIVE {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"]
        colors = {"good": "#2ecc71", "warning": "#f1c40f", "bad": "#e74c3c"}
        for _, st in session.streamers:
            kbps = st.measure_kbps()
            level, reason = st.health()
            line = f"<span style='color:{colors[level]}'>●</span> {html.escape(st.name)}: "
            if st.connected:
                line += f"{kbps:.0f} kbps, {st.out_width}×{st.out_height} {st.fps} fps"
                if reason != "live":
                    line += f" · {html.escape(reason)}"
                if st.dropped:
                    line += f" · {st.dropped} dropped"
                if st.reconnects:
                    line += f" · {st.reconnects} reconnects"
            else:
                line += html.escape(st.status)
            lines.append(line)
        self.stream_status.setText("<br>".join(lines))

    # Hotkeys -----------------------------------------------------------------

    def _apply_hotkeys(self) -> None:
        self.hotkeys.apply(self.collection.hotkeys)
        if self.collection.hotkeys and self.hotkeys.mode == "window":
            self.statusBar().showMessage("Global hotkeys aren't available here; they work while SprigCast is focused.", 10000)

    # Settings window -------------------------------------------------------------

    def _open_settings(self, tab: str = "General") -> None:
        devices = {
            "audio_input": [d.name for d in list_audio_devices(loopback=False)],
            "audio_output": [d.name for d in list_audio_devices(loopback=True)],
        }
        window = SettingsWindow(
            self.collection,
            default_recordings_folder=self._default_recordings_folder(),
            encoder=codec_label(pick_codec()),
            audio_devices=devices,
            plugin_report=self.plugin_report,
            open_plugins_folder=self._open_plugins_folder if self.plugins_dir is not None else None,
            streaming=self.streaming is not None,
            recording=self.recording is not None,
            tab=tab,
            parent=self,
        )
        window.applied.connect(self._after_settings)
        window.exec()

    def _after_settings(self) -> None:
        """Settings were applied: save, and bring every dock up to date."""
        from sprigcast.app.theme import apply_theme

        apply_theme(QApplication.instance())
        self._changed()
        self._refresh_stream_boxes()
        self._updating = True
        for canvas_id, box in self.record_canvas_boxes.items():
            box.blockSignals(True)
            box.setChecked(canvas_id in self.collection.recording.canvases)
            box.blockSignals(False)
        self._updating = False
        self._refresh_audio()
        self._apply_hotkeys()

    def _on_hotkey(self, action: str) -> None:
        if action == TOGGLE_STREAM:
            self._toggle_streaming()
        elif action == TOGGLE_RECORD:
            self._toggle_recording()
        elif action.startswith(SCENE_PREFIX):
            scene_id = action[len(SCENE_PREFIX):]
            if any(s.id == scene_id for s in self.collection.scenes):
                self.collection.set_active(scene_id)
                self._changed()
                self._refresh_scenes()
                self._refresh_previews()

    # Stats -----------------------------------------------------------------------

    def _refresh_stats_panel(self) -> None:
        outputs = []
        if self.streaming is not None:
            outputs += [(f"Stream: {st.name}", st) for _, st in self.streaming.streamers]
        if self.recording is not None:
            outputs += [(f"Recording: {canvas_id}", rec) for canvas_id, rec in self.recording.recorders]
        self.stats_panel.refresh(self._last_stats, outputs, self._recordings_folder())
