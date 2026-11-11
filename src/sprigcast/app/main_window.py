"""Main window: two canvas previews plus Scenes, Sources, Properties, Audio and Recording docks."""

from __future__ import annotations

import html
import logging
import queue
import time
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.audio_dock import AudioPanel
from sprigcast.app.camera_dialog import CameraDialog
from sprigcast.app.destinations import DestinationsPanel, live_line
from sprigcast.app.filters_dialog import FiltersDialog
from sprigcast.app.hotkeys import SCENE_PREFIX, TOGGLE_RECORD, TOGGLE_STREAM, Hotkeys
from sprigcast.app.live_panel import LivePanel
from sprigcast.app.part_dialog import NewPartDialog
from sprigcast.app.preview import Preview
from sprigcast.app.settings_dialog import SettingsDialog
from sprigcast.app.settings_window import (
    CONFIRM_RECORDING,
    CONFIRM_STREAM,
    SettingsWindow,
    confirm_enabled,
)
from sprigcast.app.stats_panel import StatsPanel
from sprigcast.app.terms import PART, PART_MARK, PARTS, SHOW, SHOWS, a, cap
from sprigcast.core.events import (
    AudioLevels,
    EngineError,
    ReplaceCollection,
    SourceInfo,
    Stats,
)
from sprigcast.core.scene import (
    PART_KIND,
    Scene,
    SceneCollection,
    SceneItem,
    SourceConfig,
)
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
BUILT_IN_KINDS = {"display_capture", "window_capture", "camera", "color", "image", "text", "audio_input", "audio_output",
                  "scene"}


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


ON_AIR_STYLE = (
    "background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #e0604f, stop:1 #a93226);"
    " color: white; font-weight: bold; border: 1px solid #7d2219; border-top-color: #f08a7c; border-radius: 8px;"
)


class MainWindow(QMainWindow):
    def __init__(self, collection: SceneCollection, scenes_path: Path, plugins_dir: Path | None = None):
        super().__init__()
        self.collection = collection
        self.scenes_path = scenes_path
        self.plugins_dir = plugins_dir
        self.plugin_report = None  # set by main.run() after loading plugins
        self.selected_scene_id: str | None = collection.active_scenes.get(collection.canvases[0].id)
        # A part being edited in place inside a show: canvas id, show id, placement source id, layout id.
        self.in_place: tuple[str, str, str, str] | None = None
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
            preview.opened.connect(self._on_preview_opened)
            self.previews[canvas.id] = preview
            splitter.addWidget(preview)
            splitter.setStretchFactor(splitter.count() - 1, 3 if canvas.width >= canvas.height else 1)
        # While a part is open in Build mode, the canvases make way for the part, edited in place.
        self.part_preview = Preview("part:", "Part", (16, 9))
        self.part_preview.transparent = True
        self.part_preview.selected.connect(self._on_preview_selected)
        self.part_preview.edited.connect(self._on_preview_edited)
        part_panel = QWidget()
        part_layout = QVBoxLayout(part_panel)
        part_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 8, 0)
        self.part_path = QLabel()
        self.part_path.setStyleSheet("font-weight: bold")
        header.addWidget(self.part_path, 1)
        # One button per canvas shape: switch to that layout, or make it ("+ Vertical layout").
        self.shape_buttons: dict[str, QPushButton] = {}
        for canvas in self.collection.canvases:
            button = QPushButton()
            button.setCheckable(True)
            button.clicked.connect(lambda _=False, cid=canvas.id: self._edit_shape(cid))
            self.shape_buttons[canvas.id] = button
            header.addWidget(button)
        self.remove_shape_button = QPushButton("Remove layout")
        self.remove_shape_button.setToolTip(f"{cap(SHOWS)} on this canvas go back to using the {PART}'s main layout")
        self.remove_shape_button.clicked.connect(self._remove_shape)
        header.addWidget(self.remove_shape_button)
        done = QPushButton("Done")
        done.setToolTip("Back to the canvases")
        done.clicked.connect(self._close_part)
        header.addWidget(done)
        part_layout.addLayout(header)
        self.part_empty = QLabel(
            f"Empty {PART}: add sources with Sources → +, or right-click sources in {a(SHOW)} → Turn into {a(PART)}."
        )
        self.part_empty.setWordWrap(True)
        self.part_empty.setStyleSheet("background: #3a3f36; color: #ece8e1; padding: 8px 12px; border-radius: 8px; margin: 4px 8px;")
        part_layout.addWidget(self.part_empty)
        part_layout.addWidget(self.part_preview, 1)
        self.center = QStackedWidget()
        self.center.addWidget(splitter)
        self.center.addWidget(part_panel)
        self.setCentralWidget(self.center)

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
        self._build_destinations_dock()
        self._build_controls_dock()
        self._build_live_dock()
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

        self.mode = "build"
        self._build_mode_bar()
        self._refresh_all()
        self._restore_layout()
        self._show_part_editor()

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree = QTreeWidget()
        self.scene_tree.setHeaderHidden(True)
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self.scene_tree.itemDoubleClicked.connect(lambda *_: self._make_active())
        self.scene_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scene_tree.customContextMenuRequested.connect(self._scene_context_menu)
        add_menu = QMenu(self)
        add_menu.addAction(f"New {SHOW}…").triggered.connect(self._add_scene)
        add_menu.addAction(f"New {PART}…").triggered.connect(self._add_part)
        bar = _toolbar(
            ("+", f"Add {a(SHOW)} (a scene you switch to on air) or {a(PART)} (a reusable piece for {SHOWS})", add_menu),
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
        if settings.value("window/mode", "build") == "live":
            self._set_mode("live")

    def _save_layout(self) -> None:
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue(self._state_key(self.mode), self.saveState(self.LAYOUT_VERSION))

    def _build_sources_dock(self) -> None:
        self.source_list = QListWidget()
        self.source_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
            ("Filters", "Effects for the selected source: color correction, chroma key, ...", self._edit_filters),
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
        parts_menu = menu.addMenu(cap(PART))
        scene = self._scene()
        parts = [p for p in self.collection.parts() if scene is None or not self.collection.contains(p, scene.id)]
        for part in parts:
            parts_menu.addAction(part.name).triggered.connect(lambda _=False, pid=part.id: self._add_part_source(pid))
        if not parts:
            empty = parts_menu.addAction(f"No {PARTS} yet: Scenes → + → New {PART}")
            empty.setEnabled(False)
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
            if item is not None and self.collection.source(item.source).kind == PART_KIND:
                menu.addAction(f"Edit {PART} in place").triggered.connect(self._open_part_item)
            count = len(self.source_list.selectedItems())
            turn = menu.addAction(f"Turn into {a(PART)}…" if count < 2 else f"Turn these {count} into {a(PART)}…")
            turn.setToolTip(f"Move the selected sources into a new reusable {PART}, right where they are")
            turn.triggered.connect(self._turn_into_part)
            menu.addSeparator()
            menu.addAction("Settings…").triggered.connect(self._edit_source_settings)
            menu.addAction("Filters…").triggered.connect(self._edit_filters)
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
            scene = self._scene()
            if scene.kind == "show":
                menu.addAction("Show").triggered.connect(self._make_active)
            else:
                part = self.collection.main_part(scene)
                for canvas in self.collection.canvases:
                    show = self.collection.active_scene(canvas.id)
                    if show is None:
                        continue
                    action = menu.addAction(f"Add to “{show.name}”  ({canvas.id})")
                    if self.collection.contains(part, show.id):
                        action.setEnabled(False)
                    action.triggered.connect(lambda _=False, pid=part.id, sid=show.id: self._add_part_source(pid, sid))
                menu.addSeparator()
            menu.addAction("Rename…").triggered.connect(self._rename_scene)
            menu.addAction("Remove").triggered.connect(self._remove_scene)
            menu.addSeparator()
        menu.addAction(f"New {SHOW}…").triggered.connect(self._add_scene)
        menu.addAction(f"New {PART}…").triggered.connect(self._add_part)
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
            None,
            ("Filters", "Noise gate, compressor, gain and more for the selected audio source", self._edit_audio_filters),
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
        hint = QLabel("Where to stream: the Destinations dock.")
        hint.setStyleSheet("color: gray")
        hint.setWordWrap(True)
        layout.addWidget(hint)
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

    def _build_destinations_dock(self) -> None:
        from sprigcast.output.keys import get_key

        def has_key(ref):
            try:
                return bool(ref and get_key(ref))
            except Exception:
                return False

        self.destinations = DestinationsPanel(self._on_stream_toggled, self._edit_destination, has_key)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.destinations)
        self._dock("Destinations", scroll)

    def _edit_destination(self, output_id: str | None) -> None:
        self._open_settings("Stream", output_id=output_id or "new")

    def _build_live_dock(self) -> None:
        self.live_panel = LivePanel(self._switch_to_scene)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(self.live_panel)
        self._dock("Live", scroll)

    def _switch_to_scene(self, scene_id: str) -> None:
        self.selected_scene_id = scene_id
        self._make_active()
        self._refresh_sources()

    # Modes -------------------------------------------------------------------------

    MODES = {"build": "Build", "live": "Live"}

    def _build_mode_bar(self) -> None:
        """Build (edit scenes, OBS-style docks) or Live (big scene buttons, destinations, mixer)."""
        bar = QToolBar("Mode")
        bar.setObjectName("modeBar")
        bar.setMovable(False)
        bar.setStyleSheet(
            "QToolButton { padding: 5px 18px; border-radius: 8px; margin: 2px; }"
            "QToolButton:checked { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #f3c3d1, stop:1 #de93aa);"
            " color: #1e1f1c; font-weight: bold; border: 1px solid #b9768b; }"
        )
        group = QActionGroup(self)
        group.setExclusive(True)
        self.mode_actions: dict[str, QAction] = {}
        for mode, label in self.MODES.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setToolTip({"build": "Edit scenes and sources (Ctrl+1)",
                               "live": "Run the show: big scene buttons, destinations and mixer (Ctrl+2)"}[mode])
            action.setShortcut(QKeySequence("Ctrl+1" if mode == "build" else "Ctrl+2"))
            action.triggered.connect(lambda _=False, m=mode: self._set_mode(m))
            group.addAction(action)
            bar.addAction(action)
            self.mode_actions[mode] = action
        self.mode_actions["build"].setChecked(True)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.mode_clock = QLabel("")
        bar.addWidget(self.mode_clock)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

    def _set_mode(self, mode: str) -> None:
        if mode == self.mode:
            self.mode_actions[mode].setChecked(True)
            return
        QSettings().setValue(self._state_key(self.mode), self.saveState(self.LAYOUT_VERSION))
        self.mode = mode
        self.mode_actions[mode].setChecked(True)
        state = QSettings().value(self._state_key(mode))
        if state is None or not self.restoreState(state, self.LAYOUT_VERSION):
            self._arrange_default_layout()
        editable = mode == "build"
        self._exit_in_place()
        self._show_part_editor()
        for preview in self.previews.values():
            preview.editable = editable
            preview.update()
        QSettings().setValue("window/mode", mode)

    @staticmethod
    def _state_key(mode: str) -> str:
        return "window/state" if mode == "build" else f"window/state_{mode}"

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

    LAYOUT_VERSION = 3  # bump when the set of docks changes, so an old saved layout is ignored

    DEFAULT_LAYOUTS = {
        # mode: (docks along the bottom, their widths)
        "build": (["Scenes", "Sources", "Audio Mixer", "Scene Transitions", "Destinations", "Controls"],
                  [200, 260, 330, 170, 260, 190]),
        "live": (["Live", "Audio Mixer", "Destinations", "Controls"], [560, 360, 300, 200]),
    }

    def _arrange_default_layout(self) -> None:
        """Build: OBS-style Scenes | Sources | Audio Mixer | Scene Transitions | Destinations | Controls.
        Live: Live (scene buttons) | Audio Mixer | Destinations | Controls."""
        order, widths = self.DEFAULT_LAYOUTS[getattr(self, "mode", "build")]
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
        for name, dock in self.docks.items():
            if name not in order:
                self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
                dock.hide()
        self.resizeDocks([self.docks[n] for n in order], widths, Qt.Orientation.Horizontal)
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
            ("Output", "Output (encoder)…"),
            ("Recording", "Recording…"),
            ("Audio", "Audio devices…"),
            ("Video", "Canvases…"),
            ("Hotkeys", "Hotkeys…"),
            ("Plugins", "Plugins…"),
        ):
            settings_menu.addAction(label).triggered.connect(lambda _=False, t=tab: self._open_settings(t))

        docks_menu = self.menuBar().addMenu("&Docks")
        for name in ["Scenes", "Sources", "Audio Mixer", "Scene Transitions", "Destinations", "Controls", "Live",
                     "Properties", "Stats"]:
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
        part_frame = self.engine.part_frames.take()
        if part_frame is not None and part_frame.canvas_id == self.part_preview.canvas_id:
            self.part_preview.set_frame(part_frame.data, part_frame.width, part_frame.height)
        elif part_frame is not None and self.in_place is not None and part_frame.canvas_id == f"part:{self.in_place[3]}":
            self.previews[self.in_place[0]].set_inner_frame(part_frame.data, part_frame.width, part_frame.height)
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
                for preview in [*self.previews.values(), self.part_preview]:
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
        if scene is None or index is None or not 0 <= index < len(scene.items):
            return None  # the list can briefly still show another scene's rows while it's rebuilt
        return scene.items[index]

    # Refreshing widgets ------------------------------------------------------

    def _refresh_all(self) -> None:
        self._refresh_audio()
        self._refresh_scenes()
        self._refresh_sources()
        self._refresh_previews()
        self._refresh_stream_boxes()

    def _refresh_previews(self) -> None:
        in_place = self._in_place_state()
        part_names = {s.id: s.name for s in self.collection.sources if s.kind == PART_KIND}
        for canvas_id, preview in self.previews.items():
            preview.part_names = part_names
            scene = self.collection.active_scene(canvas_id)
            if in_place is not None and in_place[0] == canvas_id:
                show, placement, layout, size = in_place[1:]
                preview.subtitle = f"{show.name}  ›  {PART_MARK} {layout.name}   ·   Esc to finish"
                preview.set_inner(placement, size)
                preview.set_scene(layout, self._item_index() if layout.id == self.selected_scene_id else None)
                continue
            if preview.inner is not None:
                preview.set_inner(None, None)
            preview.subtitle = scene.name if scene else "no scene"
            index = self._item_index() if scene is not None and scene.id == self.selected_scene_id else None
            preview.set_scene(scene, index)
        part = self._scene()
        if part is not None and part.kind == "part" and self.part_preview.canvas_id == f"part:{part.id}":
            self.part_preview.set_scene(part, self._item_index())
            self.part_empty.setVisible(not part.items)
        self.live_panel.show_scenes(self.collection)

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
        parts = self.collection.parts()
        if parts:
            group = QTreeWidgetItem([f"{cap(PARTS)}  (reusable pieces for {SHOWS})"])
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.scene_tree.addTopLevelItem(group)
            selected = self.collection.scene(self.selected_scene_id) if self.selected_scene_id in {
                s.id for s in self.collection.scenes} else None
            selected_part = self.collection.main_part(selected) if selected is not None else None
            for part in parts:
                uses = len(self.collection.part_uses(part.id))
                shapes = " + ".join(s.canvas for s in self.collection.shapes(part))
                child = QTreeWidgetItem([f"{PART_MARK} {part.name}   ({shapes} · used in {uses})"])
                child.setData(0, ROLE_ID, part.id)
                child.setToolTip(0, f"{cap(a(PART))}: add it to {SHOWS} with Sources → + → {cap(PART)}. Select it to edit it.")
                group.addChild(child)
                if part is selected_part:
                    child.setSelected(True)
            group.setExpanded(True)
        self._updating = False
        self.live_panel.show_scenes(self.collection)

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
        label = f"{PART_MARK} {config.name}   [{PART}]" if config.kind == PART_KIND else f"{config.name}   [{config.kind}]"
        if any(f.enabled for f in config.filters):
            label += "   ✦ filters"
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

    def _edit_audio_filters(self) -> None:
        source_id = self.audio_panel.selected_id()
        if source_id is None:
            QMessageBox.information(self, "SprigCast", "Select an audio source first (click its name).")
            return
        self._open_filters(source_id)

    def _edit_filters(self) -> None:
        item = self._item()
        if item is None:
            QMessageBox.information(self, "SprigCast", "Select a source first.")
            return
        self._open_filters(item.source)

    def _open_filters(self, source_id: str) -> None:
        FiltersDialog(self.collection.source(source_id), self._changed, self).exec()
        self._refresh_sources(keep_row=self.source_list.currentRow())

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
        if self.in_place is not None and self.in_place[0] != canvas_id:
            self._exit_in_place()
        state = self._in_place_state()
        if state is not None:
            scene = state[3]  # the part layout being edited in place
        elif canvas_id.startswith("part:"):
            scene = next((s for s in self.collection.part_scenes() if s.id == canvas_id[5:]), None)
        else:
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
            if self.in_place is not None:
                self.in_place = None
                self.engine.editing_part = None
                self._refresh_previews()
            self.selected_scene_id = scene_id
            self._refresh_sources()
            self._show_part_editor()

    def _make_active(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        if scene.kind == "part":
            self.statusBar().showMessage(
                f"{cap(PARTS)} don't go on air by themselves: add them to {a(SHOW)} with Sources → + → {cap(PART)}.", 6000)
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
        shows = [s for s in self.collection.scenes if s.kind == "show"]
        link_options = ["(not linked)"] + [s.name for s in shows if s.canvas != canvas_id]
        link, ok = QInputDialog.getItem(self, "Add scene", "Switch together with:", link_options, 0, False)
        if not ok:
            return
        linked = next((s.id for s in shows if s.name == link and s.canvas != canvas_id), None)
        scene = Scene(id=self.collection.new_id("scene"), name=name.strip(), canvas=canvas_id, linked_to=linked)
        self.collection.scenes.append(scene)
        self.collection.active_scenes.setdefault(canvas_id, scene.id)
        self.selected_scene_id = scene.id
        self._changed()
        self._refresh_all()

    def _add_part(self) -> None:
        scene = self._scene()
        show = scene if scene is not None and scene.kind == "show" else None
        answer = NewPartDialog.ask(self.collection, show, self)
        if answer is None:
            return
        name, canvas_id, add_to_show = answer
        part = Scene(id=self.collection.new_id("part"), name=name, canvas=canvas_id, kind="part")
        self.collection.scenes.append(part)
        if add_to_show and show is not None:
            self._add_part_source(part.id, show.id)
        self.selected_scene_id = part.id
        self._changed()
        self._refresh_all()
        self._show_part_editor()

    def _add_part_source(self, part_id: str, target_id: str | None = None) -> None:
        """Place a part in the selected scene (or in `target_id`, which becomes the selected scene)."""
        if target_id is not None:
            self._exit_in_place()
            self.selected_scene_id = target_id
            self._show_part_editor()
            self._refresh_sources()
        scene, part = self._scene(), self.collection.scene(part_id)
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        if self.collection.contains(part, scene.id):
            QMessageBox.information(self, "SprigCast", f"{part.name!r} already contains this scene, so it can't go inside it.")
            return
        config = SourceConfig(id=self.collection.new_id("part-src"), kind=PART_KIND, name=part.name,
                              settings={"scene": part.id})
        self._add_item(config)
        canvas = self.collection.canvas(self.collection.shape_for(part, scene.canvas).canvas)
        self._fit_item(size=(canvas.width, canvas.height))  # a horizontal-only part fits a vertical show too
        self._refresh_scenes()

    def _turn_into_part(self) -> None:
        """Move the selected sources into a new part, placed where they were, so nothing moves on screen."""
        scene = self._scene()
        rows = sorted({self.source_list.row(r) for r in self.source_list.selectedItems()})
        if scene is None or not rows:
            return
        indices = [len(scene.items) - 1 - row for row in rows]
        first = self.collection.source(scene.items[indices[0]].source).name
        name, ok = QInputDialog.getText(
            self, f"Turn into {a(PART)}", f"Name the new {PART} (you can reuse it in any {SHOW}):",
            text=first if len(rows) == 1 else "")
        if not ok or not name.strip():
            return
        part, placement = self.collection.make_part(scene, indices, name.strip())
        self._changed()
        index = next(i for i, it in enumerate(scene.items) if it.source == placement.id)
        self._refresh_scenes()
        self._refresh_sources(keep_row=len(scene.items) - 1 - index)
        self._refresh_previews()
        self.statusBar().showMessage(
            f"Made the {PART} {part.name!r}. Double-click it to edit.", 8000)

    def _show_part_editor(self) -> None:
        """A selected part opens in place of the canvases (Build mode only); selecting a show closes it."""
        scene = self._scene()
        if scene is None or scene.kind != "part" or self.mode != "build" or self.in_place is not None:
            self.engine.editing_part = None
            self.part_preview.canvas_id = "part:"
            self.center.setCurrentIndex(0)
            return
        canvas = self.collection.canvas(scene.canvas)
        main = self.collection.main_part(scene)
        uses = self.collection.part_uses(main.id)
        self.part_path.setText(
            f"{PART_MARK} {scene.name}   ·   {scene.canvas.capitalize()} {canvas.width}×{canvas.height}   ·   "
            + (f"in {', '.join(s.name for s in uses)}" if uses else f"not in any {SHOW} yet")
        )
        shapes = {s.canvas: s for s in self.collection.shapes(main)}
        for canvas_id, button in self.shape_buttons.items():
            has = canvas_id in shapes
            button.setText(canvas_id.capitalize() if has else f"+ {canvas_id.capitalize()}")
            button.setChecked(canvas_id == scene.canvas)
            button.setToolTip(
                f"Edit the layout {SHOWS} on the {canvas_id} canvas get" if has else
                f"Give this {PART} its own {canvas_id} layout. Until then, {canvas_id} {SHOWS} use the "
                f"{main.canvas} layout, scaled to fit."
            )
        self.remove_shape_button.setVisible(scene.shape_of is not None)
        if self.part_preview.canvas_id != f"part:{scene.id}":
            self.part_preview._image = None
        self.part_preview.canvas_id = f"part:{scene.id}"
        self.part_preview.title = scene.name
        self.part_preview.subtitle = "updates everywhere it's used"
        self.part_preview.aspect = (canvas.width, canvas.height)
        self.engine.editing_part = scene.id
        self.center.setCurrentIndex(1)
        self._refresh_previews()

    def _edit_shape(self, canvas_id: str) -> None:
        """Switch the part editor to the part's layout for a canvas, making that layout first if needed."""
        scene = self._scene()
        if scene is None or scene.kind != "part":
            return
        main = self.collection.main_part(scene)
        shape = next((s for s in self.collection.shapes(main) if s.canvas == canvas_id), None)
        if shape is None:
            shape = self.collection.add_shape(main, canvas_id, self.collection.new_id(f"{main.id}-{canvas_id}"),
                                              sizes=self.source_sizes)
            self._changed()
            self.statusBar().showMessage(
                f"Made a {canvas_id} layout for {main.name!r}, copied from its {main.canvas} one.", 6000)
        self.selected_scene_id = shape.id
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

    def _remove_shape(self) -> None:
        scene = self._scene()
        if scene is None or scene.shape_of is None:
            return
        main = self.collection.main_part(scene)
        question = (f"Remove the {scene.canvas} layout of {main.name!r}?\n\n"
                    f"{scene.canvas.capitalize()} {SHOWS} will use its {main.canvas} layout, scaled to fit.")
        if QMessageBox.question(self, "Remove layout", question) != QMessageBox.StandardButton.Yes:
            return
        self.collection.remove_shape(scene)
        self.selected_scene_id = main.id
        self._changed()
        self._refresh_all()
        self._show_part_editor()

    # Editing a part in place -------------------------------------------------------

    def _in_place_state(self):
        """(canvas id, show, placement item, part layout, layout size) while editing in place, else None.

        Drops the in-place edit when what it pointed at has gone (the show left the canvas, the
        placement or part was removed, or the part got another layout for this canvas).
        """
        if self.in_place is None:
            return None
        canvas_id, show_id, placement_id, layout_id = self.in_place
        show = self.collection.active_scene(canvas_id)
        placement = next((i for i in show.items if i.source == placement_id), None) if show else None
        part = self.collection.part_of(self.collection.source(placement_id)) if placement else None
        layout = self.collection.shape_for(part, canvas_id) if part else None
        if show is None or show.id != show_id or layout is None or layout.id != layout_id:
            self.in_place = None
            self.engine.editing_part = None
            if self.selected_scene_id == layout_id:
                self.selected_scene_id = show.id if show else None
            return None
        canvas = self.collection.canvas(layout.canvas)
        return canvas_id, show, placement, layout, (canvas.width, canvas.height)

    def _on_preview_opened(self, canvas_id: str, index: int) -> None:
        """Double-click in a preview: on a part, edit it in place; outside the part being edited, finish."""
        if self.in_place is not None:
            if index < 0 or self.in_place[0] != canvas_id:
                self._exit_in_place()
            return
        show = self.collection.active_scene(canvas_id)
        if show is None or not 0 <= index < len(show.items):
            return
        self._enter_in_place(canvas_id, show, show.items[index])

    def _open_part_item(self) -> None:
        """Open the selected part placement: in place when its show is on a canvas, else in the part editor."""
        scene, item = self._scene(), self._item()
        if scene is None or item is None:
            return
        part = self.collection.part_of(self.collection.source(item.source))
        if part is None:
            return
        if self.collection.active_scenes.get(scene.canvas) == scene.id:
            self._enter_in_place(scene.canvas, scene, item)
            return
        self.selected_scene_id = self.collection.shape_for(part, scene.canvas).id
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

    def _enter_in_place(self, canvas_id: str, show: Scene, item: SceneItem) -> None:
        part = self.collection.part_of(self.collection.source(item.source))
        if part is None:
            return
        layout = self.collection.shape_for(part, canvas_id)
        if item.rotation % 360 != 0:
            # Editing through a rotated placement would turn every drag sideways: use the part editor.
            self.selected_scene_id = layout.id
            self._refresh_scenes()
            self._refresh_sources()
            self._show_part_editor()
            return
        self.in_place = (canvas_id, show.id, item.source, layout.id)
        self.selected_scene_id = layout.id
        self._show_part_editor()  # closes the part editor if it was open: the show stays on screen
        self.engine.editing_part = layout.id  # read the part back alone, to draw it bright over the dimmed show
        self._refresh_scenes()
        self._refresh_sources()
        self._refresh_previews()
        self.previews[canvas_id].setFocus()
        uses = len(self.collection.part_uses(part.id))
        others = f" (also used in {uses - 1} other place{'s' if uses != 2 else ''})" if uses > 1 else ""
        self.statusBar().showMessage(f"Editing {part.name!r} in place{others}. Esc to finish.", 8000)

    def _exit_in_place(self) -> None:
        """Finish editing in place: back to the show, with the part's placement selected."""
        if self.in_place is None:
            return
        canvas_id, show_id, placement_id, _layout = self.in_place
        self.in_place = None
        self.engine.editing_part = None
        show = next((s for s in self.collection.scenes if s.id == show_id), None)
        row = None
        if show is not None:
            self.selected_scene_id = show.id
            index = next((i for i, it in enumerate(show.items) if it.source == placement_id), None)
            row = None if index is None else len(show.items) - 1 - index
        self._refresh_scenes()
        self._refresh_sources(keep_row=row)
        self._refresh_previews()

    def _close_part(self) -> None:
        canvas_id = self.collection.canvases[0].id
        self.selected_scene_id = self.collection.active_scenes.get(canvas_id)
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

    def _rename_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        name, ok = QInputDialog.getText(self, "Rename scene", "Scene name:", text=scene.name)
        if ok and name.strip():
            scene.name = name.strip()
            if scene.kind == "part":
                main = self.collection.main_part(scene)
                for shape in self.collection.shapes(main):
                    shape.name = scene.name
                for source in self.collection.sources:
                    if self.collection.part_of(source) is main:
                        source.name = scene.name
            self._changed()
            self._refresh_all()

    def _remove_scene(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        if scene.kind == "part":
            self._remove_part(self.collection.main_part(scene))
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

    def _remove_part(self, part: Scene) -> None:
        uses = self.collection.part_uses(part.id)
        question = f"Remove the {PART} {part.name!r}?"
        if uses:
            names = ", ".join(s.name for s in uses)
            question += f"\n\nIt's used in {len(uses)} scene{'s' if len(uses) != 1 else ''} ({names}) and will disappear from them."
        if QMessageBox.question(self, f"Remove {PART}", question) != QMessageBox.StandardButton.Yes:
            return
        placed = {s.id for s in self.collection.sources if self.collection.part_of(s) is part}
        for scene in self.collection.scenes:
            scene.items = [i for i in scene.items if i.source not in placed]
        self.collection.sources = [s for s in self.collection.sources if s.id not in placed]
        shapes = self.collection.shapes(part)
        self.collection.scenes = [s for s in self.collection.scenes if s not in shapes]
        for shape in shapes:
            for item in shape.items:
                self.collection.remove_source_if_unused(item.source)
        self.selected_scene_id = self.collection.active_scenes.get(self.collection.canvases[0].id)
        self._changed()
        self._refresh_all()
        self._show_part_editor()

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
        if config.kind == PART_KIND:
            self._open_part_item()
            return
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
                self.engine, canvases, self._recordings_folder(), settings.bitrate_kbps, settings.container,
                encoder=settings.encoder,
            )
        except Exception as exc:
            self.recording = None
            QMessageBox.critical(self, "SprigCast", f"Could not start recording:\n{exc}")
            self.record_status.setText("Not recording")
            return
        finally:
            self.record_button.setEnabled(True)
        self.record_button.setText("Stop recording")
        self.record_button.setStyleSheet(ON_AIR_STYLE)
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
            self.live_panel.set_recording("")
            QMessageBox.warning(self, "SprigCast", "Recording stopped with an error:\n" + "\n".join(errors))
        else:
            self.record_status.setText(f"Saved {names}")
            self.live_panel.set_recording("")

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
        self.live_panel.set_recording(text)

    # Streaming -----------------------------------------------------------------

    def _refresh_stream_boxes(self) -> None:
        self.destinations.show_outputs(self.collection)

    def _on_stream_toggled(self, output_id: str, on: bool) -> None:
        for output in self.collection.outputs:
            if output.id == output_id:
                output.enabled = on
        self.collection.save(self.scenes_path)
        self._refresh_stream_boxes()

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
                kbps = output.bitrate_kbps if output.ignore_recommendations else min(output.bitrate_kbps, service.max_kbps)
                target = StreamTarget(name, canvas, server, key, kbps, output.size, output.fps,
                                      output.bandwidth_test and service.bandwidth_test, output_id=output.id)
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
            self.streaming = StreamSession.start(self.engine, targets, pick_codec(), self.collection.stream_encoder)
        except Exception as exc:
            self.streaming = None
            QMessageBox.critical(self, "SprigCast", f"Could not go live:\n{exc}")
            self.stream_status.setText("Offline")
            return
        finally:
            self.live_button.setEnabled(True)
        self.live_button.setText("End stream")
        self.live_button.setStyleSheet(ON_AIR_STYLE)
        self.destinations.set_live(True)
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
                max_bitrate_kbps=output.enhanced_max_kbps or None,
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
        self.destinations.set_live(False)
        self._refresh_stream_boxes()
        self.live_panel.set_clock("Offline", False)
        self.mode_clock.setText("")
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
        clock = f"● LIVE {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        states, worst = {}, "good"
        rank = {"good": 0, "warning": 1, "bad": 2}
        for _, st in session.streamers:
            level, text = live_line(st)
            states[getattr(st, "output_id", st.name)] = (level, text)
            worst = max(worst, level, key=lambda lv: rank.get(lv, 0))
        self.destinations.show_live(states)
        names = ", ".join(html.escape(st.name) for _, st in session.streamers)
        self.stream_status.setText(f"{clock}<br>{names}")
        self.live_panel.set_clock(clock, True)
        self.mode_clock.setText(clock + ("" if worst == "good" else "  ·  check Destinations") + "  ")

    # Hotkeys -----------------------------------------------------------------

    def _apply_hotkeys(self) -> None:
        self.hotkeys.apply(self.collection.hotkeys)
        if self.collection.hotkeys and self.hotkeys.mode == "window":
            self.statusBar().showMessage("Global hotkeys aren't available here; they work while SprigCast is focused.", 10000)

    # Settings window -------------------------------------------------------------

    def _open_settings(self, tab: str = "General", output_id: str | None = None) -> None:
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
        if output_id == "new":
            window.stream_page.add_output()
        elif output_id:
            window.stream_page.select_output(output_id)
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
