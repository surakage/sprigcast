"""Main window: the Live and Build screens, their panels (app/panels.py), menus and the stats strip."""

from __future__ import annotations

import logging
import os
import queue
import time
import uuid
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QSize, QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
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
    QTabBar,
    QToolBar,
    QToolButton,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app.labels import literal
from sprigcast.app import custom_docks, doctor, icons, safe_zones, theme
from sprigcast.app.camera_dialog import CameraDialog
from sprigcast.app.destinations import live_line
from sprigcast.app.ducking_dialog import DuckingDialog, is_voice
from sprigcast.app.filters_dialog import FiltersDialog
from sprigcast.app.fit import FitButton
from sprigcast.app.hotkeys import (
    SAVE_REPLAY,
    SCENE_PREFIX,
    GAME_PREFIX,
    MEDIA_PREFIX,
    SCREENSHOT,
    STATE_PREFIX,
    TOGGLE_RECORD,
    TOGGLE_STREAM,
    Hotkeys,
)
from sprigcast.app.build_stage import (
    KIND_NAMES,
    ROLE_CHILDREN,
    ROLE_KIND,
    ROLE_NAME,
    ROLE_SPRIG,
    ROLE_LOCKED,
    ROLE_SWATCH,
    ROLE_TAG,
    BuildBar,
    BuildStage,
    InsideList,
)
from sprigcast.app.library import (
    PART_ROOT,
    SOUND_ROOT,
    ROLE_COLOUR,
    ROLE_FOLDER,
    ROLE_ID,
    ROLE_ON_AIR,
    ROLE_ROW,
    ROLE_SOURCE_KIND,
    ROLE_THUMB,
    Library,
    LibraryTree,
    heading_item,
)
from sprigcast.app.card import colour_row
from sprigcast.app.library import ROLE_NAME as LIB_NAME
from sprigcast.app.library import ROLE_TAG as LIB_TAG
from sprigcast.app.live_panel import LivePanel
from sprigcast.app.live_stage import Destination, LiveSide, LiveStage
from sprigcast.app.palette import Command, CommandPalette
from sprigcast.app.part_dialog import NewPartDialog
from sprigcast.app.preview import Preview
from sprigcast.app.resources import ResourceMeter, describe, ram_warning
from sprigcast.app.settings_dialog import SettingsDialog
from sprigcast.app.settings_window import (
    CONFIRM_RECORDING,
    CONFIRM_STREAM,
    SettingsWindow,
    confirm_enabled,
)
from sprigcast.app.states_dialog import StatesDialog
from sprigcast.app.panels import Panel, Panels
from sprigcast.app.stats_panel import StatsPanel, StatsStrip
from sprigcast.app.terms import PART, PARTS, SHOW, SHOWS, a, cap
from sprigcast.audio.mixer import source_tracks
from sprigcast.core.events import (
    AudioLevels,
    EngineError,
    ReplaceCollection,
    SourceInfo,
    Stats,
)
from sprigcast.core.edit import cover
from sprigcast.core.filters import filter_kind, settings_for
from sprigcast.core.looks import LOOKS, SHAPES
from sprigcast.core.scene import (
    FOLDER_COLOURS,
    Filter,
    Output,
    PART_KIND,
    Scene,
    SceneCollection,
    SceneItem,
    SourceConfig,
)
from sprigcast.core.source import known_kinds, source_class
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.recorder import codec_label, codec_short, pick_codec, resolve_codec
from sprigcast.output.shared import EncoderPool, SharedRecording, SharedReplay
from sprigcast.output.stream import SERVICES, StreamSession, StreamTarget
from sprigcast.render.engine import Engine
from sprigcast.sources.audio import list_audio_devices
from sprigcast.sources.camera import list_cameras
from sprigcast.sources.media import file_filter as media_file_filter, is_sound_file
from sprigcast.sources.win32_enum import list_monitors, list_windows

LEARNED_AFTER = 5  # sessions before Build's hints stop showing
BLOOM_FLAGS = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDragEnabled
FOLDER_FLAGS = (Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
                | Qt.ItemFlag.ItemIsDragEnabled)




def _dot(colour: str, size: int = 12) -> QIcon:
    """A small filled circle, the colour tag of a folder and its blooms."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(colour))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pix)
from sprigcast.app.sound_card import Sound, auto_group
from sprigcast.app.names import ask_name, plain_problem, short_name, short_status, status_summary, window_name

log = logging.getLogger(__name__)


# Kinds that have their own button or dock; everything else with a `label` goes under + → Plugins.
BUILT_IN_KINDS = {"display_capture", "window_capture", "camera", "color", "image", "text", "audio_input", "audio_output", "game_capture",
                  "app_audio", "scene", "browser", "chat", "media"}


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
    b = FitButton(text)
    b.clicked.connect(slot)
    return b




class MainWindow(QMainWindow):
    def __init__(self, collection: SceneCollection, scenes_path: Path, plugins_dir: Path | None = None,
                 shelf=None):
        super().__init__()
        self.collection = collection
        self.scenes_path = scenes_path
        from sprigcast.app.gardens_menu import GardenControl
        from sprigcast.core.gardens import Shelf

        if shelf is None:  # tests and tools: gardens.json beside the scenes file
            shelf = Shelf(scenes_path.parent)
            shelf.sync_path(collection)
        self.gardens = GardenControl(self, shelf)  # Gardens and Paths menus (scene collections, profiles)
        self.plugins_dir = plugins_dir
        self.plugin_report = None  # set by main.run() after loading plugins
        self.selected_scene_id: str | None = collection.active_scenes.get(collection.canvases[0].id)
        self.last_bloom_id: str | None = self.selected_scene_id  # where a sprig's ← Back goes
        self.selected_sound_id: str | None = None  # a sound picked in Build's library
        self._undo: list[str] = []  # earlier collections as JSON, oldest first (Edit → Undo, Ctrl+Z)
        self._redo: list[str] = []
        self._undo_current: str | None = collection.model_dump_json()
        self._undo_at = 0.0
        # Hints and tips show for your first few sessions, then get out of the way.
        sessions = QSettings().value("build/sessions", 0, type=int) + 1
        QSettings().setValue("build/sessions", sessions)
        self.learned = sessions > LEARNED_AFTER
        # A part being edited in place inside a show: canvas id, show id, placement source id, layout id.
        self.in_place: tuple[str, str, str, str] | None = None
        self._updating = False  # True while we fill widgets, so their signals do not echo back
        self.source_sizes: dict[str, tuple[int, int]] = {}
        self.source_statuses: dict[str, str] = {}
        self.recording: SharedRecording | None = None
        self.replay: SharedReplay | None = None
        self.streaming: StreamSession | None = None
        self._stream_status_at = 0.0
        # Build mode edits one canvas at a time, picked with the Horizontal / Vertical tabs.
        canvas_ids = [c.id for c in collection.canvases]
        saved = QSettings().value("build/canvas", canvas_ids[0])
        self.build_canvas: str = saved if saved in canvas_ids else canvas_ids[0]
        self.selected_folder_id: str | None = None  # the folder last picked in the Scenes list
        if self.selected_scene_id is not None:
            self.selected_scene_id = collection.active_scenes.get(self.build_canvas, self.selected_scene_id)

        self.update_title()
        self.resize(1440, 860)

        # Previews ------------------------------------------------------------
        self.previews: dict[str, Preview] = {}
        self.projectors: list = []  # full-screen canvases (app/projector.py)
        self.canvas_columns: dict[str, QSplitter] = {}
        splitter = QSplitter(Qt.Orientation.Horizontal)
        for canvas in collection.canvases:
            preview = Preview(canvas.id, canvas.id.capitalize(), (canvas.width, canvas.height))
            preview.selected.connect(self._on_preview_selected)
            preview.edited.connect(self._on_preview_edited)
            preview.opened.connect(self._on_preview_opened)
            preview.menu_requested.connect(self._on_preview_menu)
            preview.swap_requested.connect(self._swap_shape)
            preview.sprig_dropped.connect(self._sprig_dropped)
            self.previews[canvas.id] = preview
            column = QSplitter(Qt.Orientation.Vertical)
            column.addWidget(preview)
            self.canvas_columns[canvas.id] = column
            splitter.addWidget(column)
            splitter.setStretchFactor(splitter.count() - 1, 3 if canvas.width >= canvas.height else 1)
        # While a part is open in Build mode, the canvases make way for the part, edited in place.
        self.part_preview = Preview("part:", "Part", (16, 9))
        self.part_preview.selected.connect(self._on_preview_selected)
        self.part_preview.edited.connect(self._on_preview_edited)
        self.part_preview.sprig_dropped.connect(self._sprig_dropped)
        self.part_preview.menu_requested.connect(self._on_preview_menu)
        part_panel = QWidget()
        part_layout = QVBoxLayout(part_panel)
        part_layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 8, 0)
        self.part_path = QLabel()
        self.part_path.setStyleSheet(f"color: {theme.MUTED};")
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
        states = QPushButton("Live states…")
        states.setToolTip(f"Looks for this {PART} that you switch between in Live, like “Alert showing”")
        states.clicked.connect(lambda: self._scene() is not None and self._edit_states(self._scene().id))
        header.addWidget(states)
        self.part_back = QPushButton("← Back")
        self.part_back.setToolTip(f"Back to the {SHOW} you came from")
        self.part_back.clicked.connect(self._close_part)
        header.insertWidget(0, self.part_back)
        for button in (self.part_back, *self.shape_buttons.values(), self.remove_shape_button, states):
            button.setStyleSheet(theme.SMALL_PILL_STYLE + f"QPushButton:checked {{ background: {theme.SELECT_BG};"
                                 f" color: {theme.SELECT_TEXT}; }}")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        part_layout.addLayout(header)
        # Nesting, said plainly: what you change here changes in every bloom that uses this sprig.
        self.part_uses_note = QLabel()
        self.part_uses_note.setWordWrap(True)
        self.part_uses_note.setTextFormat(Qt.TextFormat.RichText)
        self.part_uses_note.linkActivated.connect(self._jump_to_bloom)
        self.part_uses_note.setStyleSheet(theme.NOTE_STYLE + " padding: 9px 14px; margin: 4px 8px 0 8px;")
        part_layout.addWidget(self.part_uses_note)
        part_layout.addWidget(self.part_preview, 1)
        self.splitter = splitter
        splitter.installEventFilter(self)  # Live re-shares the stage when it changes size
        # Build's shape tabs: one shape at a time, each with its own blooms (they sit at the top of the library).
        self.canvas_tabs = QTabBar()
        self.canvas_tabs.setObjectName("shapeTabs")
        self.canvas_tabs.setDrawBase(False)
        self.canvas_tabs.setExpanding(True)
        self.canvas_tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.canvas_tabs.setStyleSheet(
            f"#shapeTabs {{ background: {theme.PANEL}; border-radius: 19px; }}"
            f"#shapeTabs::tab {{ padding: 8px 12px; margin: 3px; border: none; border-radius: 16px;"
            f" background: transparent; color: {theme.MUTED}; font-weight: 600; }}"
            f"#shapeTabs::tab:hover {{ color: {theme.TEXT}; }}"
            f"#shapeTabs::tab:selected {{ background: {theme.RAISED_HOVER}; color: {theme.TEXT_BRIGHT}; }}"
        )
        for canvas in collection.canvases:
            mark = "▯" if canvas.height > canvas.width else "▭"
            self.canvas_tabs.addTab(f"{mark}  {canvas.id.capitalize()}")
            self.canvas_tabs.setTabToolTip(self.canvas_tabs.count() - 1,
                                           f"Build the {canvas.id} {SHOWS} ({canvas.width}×{canvas.height}). "
                                           f"Sound and {PARTS} are shared by both shapes.")
        self.canvas_tabs.setCurrentIndex(canvas_ids.index(self.build_canvas))
        self.canvas_tabs.currentChanged.connect(self._on_canvas_tab)
        canvas_page = QWidget()
        self.canvas_page = canvas_page
        page_layout = QVBoxLayout(canvas_page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(0)
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(8, 4, 8, 0)
        tabs_row.addStretch(1)
        # Safe zones: where TikTok / Shorts / Reels draw their own buttons over a vertical stream.
        self.safe_zone_button = QToolButton()
        self.safe_zone_button.setToolTip("Show where TikTok, YouTube Shorts and Reels put their buttons and captions, "
                                         "so faces and text stay clear of them. Approximate; not on the stream.")
        self.safe_zone_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        zone_menu = QMenu(self)
        zone_group = QActionGroup(self)
        self.zone_actions: dict[str, QAction] = {}
        saved_zone = QSettings().value("preview/safe_zones", safe_zones.ALL)
        if saved_zone not in safe_zones.CHOICES:
            saved_zone = safe_zones.ALL
        for choice in safe_zones.CHOICES:
            action = zone_menu.addAction(choice)
            action.setCheckable(True)
            action.setChecked(choice == saved_zone)
            zone_group.addAction(action)
            self.zone_actions[choice] = action
            action.triggered.connect(lambda _=False, c=choice: self._set_safe_zones(c))
        self.safe_zone_button.setMenu(zone_menu)
        tabs_row.addWidget(self.safe_zone_button)
        self.canvas_tabs_row = QWidget()
        self.canvas_tabs_row.setLayout(tabs_row)
        page_layout.addWidget(self.canvas_tabs_row)
        page_layout.addWidget(splitter, 1)
        self._set_safe_zones(saved_zone)
        self._set_live_safe_zones(QSettings().value("live/safe_zones", "Off"))
        self.center = QStackedWidget()
        self.center.addWidget(canvas_page)
        self.center.addWidget(part_panel)
        # Build's screen (build_stage.py) holds the canvases; Live's screen is added by _build_live_stage.
        self.new_menu = QMenu(self)
        self.new_menu.addAction(f"New {SHOW}…").triggered.connect(lambda: self._add_scene())
        self.new_menu.addAction("New folder…").triggered.connect(lambda: self._add_folder())
        self.new_menu.addAction(f"New {PART}…").triggered.connect(lambda: self._add_part())
        self.new_menu.addAction(f"New {PART} folder…").triggered.connect(lambda: self._add_folder(holds="part"))
        self.scene_tree = LibraryTree()  # every bloom and sprig, on Build's left (app/library.py)
        self.library = Library(self.scene_tree)
        self.build_bar = BuildBar()
        self.build_stage = BuildStage(self.library, self.build_bar)
        self.build_stage.rail.insertWidget(0, self.canvas_tabs)
        self.build_stage.card_slot.addWidget(self.center)
        from sprigcast.app.sound_strips import SoundStrips

        self.sound_strips = SoundStrips(self._on_sound_changed, self._open_build_sound, self._sound_strip_menu)
        self.build_stage.below_card.addWidget(self.sound_strips)
        self.modes = QStackedWidget()
        self.modes.addWidget(self.build_stage)
        self.setCentralWidget(self.modes)

        # No floating docks: extra panels sit in one fixed card per screen (app/panels.py).
        self.panels = Panels(self)
        self._build_scenes_dock()
        self._build_sources_dock()
        self._build_chat_panel()
        self._build_transitions_dock()
        self._build_live_stage()
        self.live_side.panel_slot.addWidget(self.panels.cards["live"])
        self._build_properties_dock()
        self._build_item_actions()
        self._fill_bloom_tools()
        self.stats_panel = StatsPanel()
        self._last_stats = None
        self.peak_watch = doctor.PeakWatch()
        self.status = QLabel("Starting renderer…")
        self.meter = ResourceMeter(gpu=not os.environ.get("SPRIGCAST_NO_GPU_METER"))
        self._warned_ram = False
        self.meter_label = QLabel("")
        self._meter_tip = ("What SprigCast uses right now: its share of the whole CPU, its memory, "
                           "and how busy the graphics card is (all apps)")
        self.meter_label.setToolTip(self._meter_tip)
        self.stats_strip = StatsStrip(self.status, self.meter_label, self.stats_panel)
        self.stats_strip.on_open = self._refresh_stats_panel
        self.statusBar().addPermanentWidget(self.stats_strip)
        self._build_menu()
        self.meter_timer = QTimer(self)
        self.meter_timer.timeout.connect(self._refresh_meter)
        self.meter_timer.start(2000)

        # Engine --------------------------------------------------------------
        from sprigcast.app.browser_host import start_host

        self.browser_host = start_host(self)  # draws browser sources; Chromium loads with the first one
        self.engine = Engine(collection)
        from sprigcast.app.settings_window import apply_naming, sleep_policy

        self.engine.sleep_policy = sleep_policy()
        apply_naming()  # file-name patterns from Settings → Advanced
        # Nothing is on screen yet: don't copy full-size frames back until _poll_engine says which
        # previews are showing and how big they are (each early one was an 8 MB frame).
        self.engine.previews = frozenset()
        self.engine.start()
        self.encoders = EncoderPool(self.engine)  # one encoder per canvas, shared by recording and replay
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_engine)
        self.timer.start(33)  # about 30 preview updates per second

        self.hotkeys = Hotkeys(self)
        self.hotkeys.triggered.connect(self._on_hotkey)
        self.hotkeys.apply(self.collection.hotkeys)
        self.camera = None  # SprigCast Camera (Settings → Output → Virtual camera), see _sync_camera
        self._camera_timer = QTimer(self, interval=1000)
        self._camera_timer.timeout.connect(lambda: self.camera is not None and self.camera.poll())
        QTimer.singleShot(1500, self._sync_camera)

        self.mode = "build"
        self._load_thumbs()
        self._build_mode_bar()
        self._refresh_all()
        custom_docks.apply(self, custom_docks.load())  # before the saved panels are put back
        self._restore_layout()
        self._sync_canvas_choice()
        self._show_part_editor()
        from sprigcast.app.control_server import start_for

        self.control = start_for(self)  # Streamer.bot, Stream Deck... (Settings → Connections; off by default)
        self.chat_panel.hub.happened.connect(self.control.native_broadcast)  # TikTok gifts, likes... to apps
        from sprigcast.app.updates import Updater

        # New versions from GitHub Releases (app/updates.py); never shown or installed while live or recording.
        self.updater = Updater(self, busy=lambda: self.streaming is not None or self.recording is not None)
        self.updater.start()
        if self.collection.recording.clips_on_start:  # once the renderer has its first frames
            QTimer.singleShot(1500, lambda: self.replay is None and self._toggle_replay(True))

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree.dropped.connect(lambda: QTimer.singleShot(0, self._scenes_dropped))
        self.scene_tree.delegate.plus_clicked.connect(self._library_plus)
        self.library.search.textChanged.connect(lambda _text: self._refresh_scenes())
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self.scene_tree.itemDoubleClicked.connect(lambda *_: self._make_active())
        self.scene_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scene_tree.dot_clicked.connect(self._pick_folder_colour)
        self.scene_tree.customContextMenuRequested.connect(self._scene_context_menu)
        self.scene_tree.itemExpanded.connect(lambda item: self._folder_expanded(item, True))
        self.scene_tree.itemCollapsed.connect(lambda item: self._folder_expanded(item, False))
        add_menu = QMenu(self)
        add_menu.addAction(f"New {SHOW}…").triggered.connect(self._add_scene)
        add_menu.addAction("New folder…").triggered.connect(self._add_folder)
        add_menu.addAction(f"New {PART}…").triggered.connect(self._add_part)
        bar = _toolbar(
            ("+", f"Add {a(SHOW)} (a scene you switch to on air) or {a(PART)} (a reusable piece for {SHOWS})", add_menu),
            ("−", "Remove the selected scene", self._remove_scene),
            ("✎", "Rename the selected scene", self._rename_scene),
            None,
            ("▶ Show", "Show this scene on its canvas (or double-click it)", self._make_active),
        )
        self.link_menu = QMenu(self)
        self.link_menu.aboutToShow.connect(lambda: self._fill_link_menu(self.link_menu))
        self.link_button = QToolButton()
        self.link_button.setText("⇄ Link")
        self.link_button.setAutoRaise(True)
        self.link_button.setMinimumSize(30, 28)
        self.link_button.setMenu(self.link_menu)
        self.link_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bar.layout().insertWidget(bar.layout().count() - 1, self.link_button)
        self.panels.shelve(bar)  # the list itself is Build's left rail; its old buttons stay off screen

    def _fill_add_sound_menu(self, menu: QMenu) -> None:
        """+ beside Sound (under the canvas): A to Z."""
        self._fill_app_sound_menu(menu.addMenu("App sound"))
        menu.addAction("Desktop sound…").triggered.connect(lambda: self._add_audio("audio_output"))
        menu.addAction("Microphone…").triggered.connect(lambda: self._add_audio("audio_input"))
        menu.addAction("Sound file…").triggered.connect(lambda: self._add_media(sound=True))

    def _sound_strip_menu(self, source_id: str | None, at) -> None:
        """Right-click a sound strip (or the + beside Sound)."""
        menu = QMenu(self)
        if source_id is None:
            self._fill_add_sound_menu(menu)
        else:
            self._fill_sound_menu(menu, source_id)
        menu.exec(at)

    def _library_plus(self, key: str) -> None:
        """The round + beside Blooms or Sprigs."""
        menu = QMenu(self)
        if key == SOUND_ROOT:
            self._fill_add_sound_menu(menu)
        elif key == PART_ROOT:
            menu.addAction(f"New {PART}…").triggered.connect(lambda: self._add_part())
            menu.addAction(f"New {PART} folder…").triggered.connect(lambda: self._add_folder(holds="part"))
        else:
            menu.addAction(f"New {SHOW}…").triggered.connect(lambda: self._add_scene())
            menu.addAction(f"New {SHOW} folder…").triggered.connect(lambda: self._add_folder())
        menu.exec(QCursor.pos())

    def _fill_app_sound_menu(self, menu: QMenu) -> None:
        from sprigcast.sources.app_audio import SOUND_TYPES

        for kind in SOUND_TYPES:
            action = menu.addAction(literal(kind.name))
            action.setToolTip(kind.hint)
            action.triggered.connect(lambda _=False, k=kind.key: self._add_app_sound(k))

    def _build_chat_panel(self) -> None:
        """Chat on Live: your Twitch, YouTube and Kick chats mixed, or any chat link, in this one panel."""
        from sprigcast.app.chat_panel import KEY, ChatPanel

        self.chat_panel = ChatPanel(self._connect_twitch)
        self.panels.add(Panel(KEY, "Chat", self.chat_panel, on_show=self.chat_panel.activate), shown_in=("live",))

        def chat_in_use() -> None:  # off every screen and not popped out: stop reading until it's back
            if not self.panels.is_popped(KEY) and not any(KEY in keys for keys in self.panels.state.shown.values()):
                self.chat_panel.sleep()

        self.panels.changed.connect(chat_in_use)
        self._chat_sources_changed()

    def _chat_sources_changed(self) -> None:
        """A chat source on any bloom keeps your chats read, even with the Chat panel closed."""
        panel = getattr(self, "chat_panel", None)
        if panel is not None:
            panel.hub.want("source", any(s.kind == "chat" for s in self.collection.sources))

    def _connect_twitch(self) -> None:
        from sprigcast.app.twitch_connect import TwitchConnectDialog

        TwitchConnectDialog(self).exec()  # the Chat panel hears about the new account itself

    def _build_transitions_dock(self) -> None:
        from sprigcast.app.transitions_panel import TransitionsPanel
        from sprigcast.render.stinger import probe

        self.transitions_panel = TransitionsPanel(self.collection.transition,
                                                  lambda: self._transition_edited(self.transitions_panel), probe,
                                                  collection=self.collection)
        scroll = QScrollArea()  # a short card (Build's left column) scrolls instead of squashing the pills
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(self.transitions_panel)
        # On Live, transitions have their own card under the canvas (see _build_live_transitions).
        self.panels.add(Panel("transitions", "Transitions", self._dock("Scene Transitions", scroll), modes=("build",)))
        self._build_transition_menu()

    def _transition_edited(self, source=None) -> None:
        """One transitions view changed the settings: show them in the others, then save."""
        for panel in (getattr(self, "transitions_panel", None), getattr(self, "live_transitions", None)):
            if panel is not None and panel is not source:
                panel.load(self.collection.transition)
        self._show_transition_chip()
        self._changed()

    def _build_live_transitions(self) -> None:
        """Live's Transitions card: the choices and their settings; the bloom-to-bloom table is a pop-up."""
        from sprigcast.app.live_stage import heading
        from sprigcast.app.transitions_panel import TransitionsPanel
        from sprigcast.render.stinger import probe

        card = self.live_stage.transitions_card
        card.layout().addWidget(heading("Transitions"))
        self.live_transitions = TransitionsPanel(self.collection.transition,
                                                 lambda: self._transition_edited(self.live_transitions), probe,
                                                 collection=self.collection, compact=True)
        self.live_transitions.layout().setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(self.live_transitions)
        card.layout().addWidget(scroll, 1)

    def _show_transition_chip(self) -> None:
        from sprigcast.app.transitions_panel import summary

        chip = getattr(getattr(self, "live_panel", None), "transition_chip", None)
        if chip is not None:
            chip.setText(f"{summary(self.collection.transition)}  ▾")

    def _build_transition_menu(self) -> None:
        """The Live chip's menu: pick a transition in one click, or open the panel for its settings."""
        from sprigcast.app.transitions_panel import KINDS

        if not hasattr(self, "transitions_panel") or not hasattr(self, "live_panel"):
            return  # called again once both exist
        chip = self.live_panel.transition_chip
        menu = QMenu(chip)
        picks = {}
        for kind, label, tip in KINDS:
            action = menu.addAction(label, lambda k=kind: self.transitions_panel.set_kind(k))
            action.setToolTip(tip)
            action.setCheckable(True)
            picks[kind] = action
        menu.addSeparator()
        menu.addAction("Settings…", self._show_transitions_panel)
        menu.aboutToShow.connect(lambda: [a.setChecked(k == self.collection.transition.kind) for k, a in picks.items()])
        chip.setMenu(menu)
        self._show_transition_chip()

    def _show_transitions_panel(self) -> None:
        self.panels.set_shown("transitions", True)
        self.panels.select("transitions")

    # Window layout ---------------------------------------------------------------

    def _restore_layout(self) -> None:
        """Put the window and docks back where they were last time (if the screen still fits them)."""
        settings = QSettings()
        geometry = settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        if settings.value("window/mode", "build") == "live":
            self._set_mode("live")
        self.panels.set_mode(self.mode)
        self.panels.restore_popped()

    def _save_layout(self) -> None:
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())

    def _build_sources_dock(self) -> None:
        self.source_list = InsideList()
        self.source_list.moved.connect(self._on_inside_moved)
        self.source_list.action_clicked.connect(self._row_action)
        self.source_list.sprig_dropped.connect(lambda part_id: self._sprig_dropped("", part_id))
        self.source_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.source_list.itemSelectionChanged.connect(self._refresh_properties)
        self.source_list.itemChanged.connect(self._on_source_toggled)
        self.source_list.itemDoubleClicked.connect(lambda *_: self._edit_source_settings())
        self.source_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.source_list.customContextMenuRequested.connect(self._source_context_menu)
        self.add_source_menu = QMenu(self)
        self.add_source_menu.aboutToShow.connect(self._fill_add_source_menu)
        # Build's left column: "Inside" with + Add, the list, then a quiet row of tools.
        self.source_list.setObjectName("insideList")
        self.source_list.setStyleSheet(
            "#insideList { background: transparent; border: none; }"
            "#insideList::item, #insideList::item:hover, #insideList::item:selected { background: transparent; }")
        add = QPushButton("+ Add")
        add.setStyleSheet(theme.SMALL_PILL_STYLE)
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.setToolTip("Add a camera, screen, window, image, text, sound or sprig")
        add.setMenu(self.add_source_menu)
        self.build_stage.add_slot.addWidget(add)
        self.build_stage.left.addWidget(self.source_list, 1)
        from sprigcast.app.empty import EmptyState

        self.inside_empty = EmptyState("Nothing here yet. Add a camera, screen, text or sprig.", "+ Add something",
                                       lambda: self.add_source_menu.exec(QCursor.pos()), scale=0.8)
        self.inside_empty.hide()
        self.build_stage.left.addWidget(self.inside_empty)
        self.build_stage.left.addWidget(self.panels.cards["build"], 1)  # panels you choose for Build

    def _fill_bloom_add_menu(self, menu: QMenu, parts: list[Scene]) -> None:
        """+ Add in a bloom: its sprigs, by folder, and New sprig. Sources are added inside a sprig."""
        sprig = icons.icon("sprig", theme.ACCENT)
        folders = {f.id: f for f in self.collection.part_folders()}
        for part in parts:
            if part.folder not in folders:
                menu.addAction(sprig, literal(part.name)).triggered.connect(
                    lambda _=False, pid=part.id: self._add_part_source(pid))
        for folder in folders.values():
            inside = [p for p in parts if p.folder == folder.id]
            if not inside:
                continue
            sub = menu.addMenu(_dot(folder.colour, 12), literal(folder.name))
            for part in inside:
                sub.addAction(sprig, literal(part.name)).triggered.connect(
                    lambda _=False, pid=part.id: self._add_part_source(pid))
        if menu.actions():
            menu.addSeparator()
        menu.addAction(f"New {PART}…").triggered.connect(lambda: self._add_part())

    def _fill_add_source_menu(self) -> None:
        """+ Add: your real cameras, screens and windows by name (one click adds them), then the rest.
        The device lists are read when their submenu opens, so opening + Add stays instant."""
        menu = self.add_source_menu
        menu.clear()
        scene = self._scene()
        parts = [p for p in self.collection.parts() if scene is None or not self.collection.contains(p, scene.id)]
        if scene is not None and scene.kind == "show":  # blooms hold sprigs only
            self._fill_bloom_add_menu(menu, parts)
            return
        parts_menu = menu.addMenu(icons.icon("sprig", theme.ACCENT), cap(PART))
        for part in parts:
            parts_menu.addAction(literal(part.name)).triggered.connect(lambda _=False, pid=part.id: self._add_part_source(pid))
        if not parts:
            parts_menu.addAction(f"No {PARTS} yet: + beside {cap(PARTS)} on the left").setEnabled(False)
        menu.addSeparator()

        def lazy(title: str, fill) -> None:
            sub = menu.addMenu(title)
            sub.aboutToShow.connect(lambda: (sub.clear(), fill(sub)) if not sub.actions() else None)

        def cameras(sub: QMenu) -> None:
            found = list_cameras()
            for cam in found:
                sub.addAction(literal(cam.name)).triggered.connect(lambda _=False, c=cam: self._add_camera(c))
            if not found:
                sub.addAction("No camera found (plugged in? in use?)").setEnabled(False)
            sub.addSeparator()
            sub.addAction("Pick size and frame rate…").triggered.connect(lambda: self._add_camera())

        def screens(sub: QMenu) -> None:
            found = list_monitors()
            for monitor in found:
                sub.addAction(monitor.label).triggered.connect(lambda _=False, m=monitor: self._add_display(m))
            if not found:
                sub.addAction("Screen capture works on Windows").setEnabled(False)

        def windows(sub: QMenu) -> None:
            found = list_windows()
            for window in found[:30]:
                sub.addAction(window_name(window.title, window.process)).triggered.connect(
                    lambda _=False, w=window: self._add_window(w))
            if not found:
                sub.addAction("Window capture works on Windows").setEnabled(False)

        def games(sub: QMenu) -> None:
            sub.addAction("Any full-screen game").triggered.connect(lambda: self._add_game())
            found = list_windows()
            if found:
                sub.addSection("A game's window")
            for window in found[:30]:
                sub.addAction(window_name(window.title, window.process)).triggered.connect(
                    lambda _=False, w=window: self._add_game(w))

        def devices(kind: str):
            def fill(sub: QMenu) -> None:
                sub.addAction("Default (follows Windows)").triggered.connect(
                    lambda _=False: self._add_audio(kind, "default"))
                for device in list_audio_devices(loopback=kind == "audio_output"):
                    sub.addAction(literal(device.name)).triggered.connect(
                        lambda _=False, d=device.name: self._add_audio(kind, d))
            return fill

        def mirrors(sub: QMenu) -> None:
            here = {i.source for i in scene.items} if scene is not None else set()
            placed = {i.source for s in self.collection.scenes for i in s.items}
            seen = [c for c in self.collection.sources if c.id in placed and c.id not in here
                    and c.kind != PART_KIND and c.kind not in self.AUDIO_KINDS]
            for config in sorted(seen, key=lambda c: c.name.lower()):
                sub.addAction(literal(config.name)).triggered.connect(
                    lambda _=False, sid=config.id: self._add_mirror(sid))
            if not seen:
                sub.addAction("Nothing to mirror yet").setEnabled(False)

        # A to Z, the way OBS lists them (sprigs stay on top: they're SprigCast's own).
        entries = [
            ("App sound", lambda sub: self._fill_app_sound_menu(sub)),
            ("Chat", self._add_chat),
            ("Colour…", self._add_color),
            ("Desktop sound", devices("audio_output")),
            ("Game capture", games),
            ("Microphone", devices("audio_input")),
            ("Mirror", mirrors),
            ("Picture…", self._add_image),
            ("Screen", screens),
            ("Text", self._add_text),
            ("Video capture", cameras),
            ("Video or sound file…", self._add_media),
            ("Web page…", self._add_browser),
            ("Window", windows),
        ]
        tips = {
            "App sound": "One kind of sound on its own row: games, music, voice chat…",
            "Desktop sound": "Everything a speaker or headset plays",
            "Game capture": "A game: the full-screen one in front, or a game's window. Its sound comes with it",
            "Microphone": "A mic: plays in every bloom unless you pick some",
            "Mirror": "Show a leaf from another bloom here too. It's the same leaf: change one, both change",
            "Video capture": "A webcam, capture card or virtual camera",
        }
        for label, action in sorted(entries, key=lambda e: e[0].lower()):
            if action in (games, screens, windows, cameras, mirrors) or label in ("App sound", "Desktop sound",
                                                                                   "Microphone"):
                sub = menu.addMenu(label)
                sub.setToolTipsVisible(True)
                sub.menuAction().setToolTip(tips.get(label, ""))
                sub.aboutToShow.connect(lambda s=sub, f=action: (s.clear(), f(s)) if not s.actions() else None)
            else:
                act = menu.addAction(label)
                act.setToolTip(tips.get(label, ""))
                act.triggered.connect(lambda _=False, f=action: f())
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        plugins = menu.addMenu("Plugins")
        self.more_menu = plugins
        plugins.aboutToShow.connect(self._fill_more_menu)  # store plugins can arrive while SprigCast runs
        self._fill_more_menu()

    def _source_context_menu(self, pos) -> None:
        on_item = self.source_list.itemAt(pos) is not None
        self._source_menu(on_item).exec(self.source_list.viewport().mapToGlobal(pos))

    def _on_preview_menu(self, canvas_id: str, index: int, where) -> None:
        self._preview_menu(canvas_id, index).exec(where)

    def _preview_menu(self, canvas_id: str, index: int) -> QMenu:
        """Right-click in a preview: the same menu as the Sources list for what's under the mouse.
        In Live (nothing to edit) it offers Pop out and this canvas's settings."""
        if canvas_id.startswith("part:") or (self.mode == "live" and canvas_id in self.previews):
            menu = QMenu(self)
        else:
            menu = self._source_menu(index >= 0 and self._item() is not None)
        if index < 0 and not canvas_id.startswith("part:"):
            if menu.actions():
                menu.addSeparator()
            if self.mode == "live":
                self._live_canvas_menu(menu, canvas_id)
                return menu
            self._fill_projector_menu(self._pop_out_menu(menu), canvas_id)
            if self.mode != "live":
                menu.addAction("Canvas size and frame rate…").triggered.connect(lambda: self._open_settings("Video"))
        return menu

    def _live_canvas_menu(self, menu: QMenu, canvas_id: str) -> None:
        """Right-click a canvas on Live: things about this canvas only (nothing here edits a bloom)."""
        canvas = self.collection.canvas(canvas_id)
        menu.setToolTipsVisible(True)
        self._fill_projector_menu(self._pop_out_menu(menu), canvas_id)
        preview = self.previews.get(canvas_id)
        if preview is not None and preview.zoom > 1.0:
            menu.addAction("Fit to view  (Ctrl+0)").triggered.connect(preview.fit_view)
        if canvas is not None and canvas.height > canvas.width:  # the vertical apps' buttons and captions
            menu.addSeparator()
            zones = menu.addMenu(icons.icon("panel", theme.MUTED), "Safe zones")
            zones.setToolTip("Where TikTok, Shorts and Reels draw over your stream (only you see these)")
            current = QSettings().value("live/safe_zones", "Off")
            for choice in safe_zones.CHOICES:
                act = zones.addAction(choice)
                act.setCheckable(True)
                act.setChecked(choice == current)
                act.triggered.connect(lambda _=False, c=choice: self._set_live_safe_zones(c))
        menu.addSeparator()
        size = f"{canvas.width}×{canvas.height}, {canvas.fps} fps" if canvas is not None else ""
        settings = menu.addAction(icons.icon("gear", theme.MUTED), f"Canvas settings…  {size}".rstrip())
        settings.setToolTip("Size and frame rate (Settings → Video)")
        settings.triggered.connect(lambda: self._open_settings("Video"))

    # Projectors ------------------------------------------------------------------------

    def _fill_projector_menu(self, menu: QMenu, canvas_id: str, kind: str = "canvas", ref: str = "") -> None:
        """Monitors to show this on full screen, or a window. kind: "canvas" (canvas_id is what's on air),
        "bloom" (ref is a bloom or sprig id) or "source" (ref is a source id)."""
        from sprigcast.app.projector import screen_label

        target = (kind, ref or canvas_id)
        menu.setToolTipsVisible(True)
        for i, screen in enumerate(QGuiApplication.screens()):
            act = menu.addAction(icons.icon("monitor", theme.MUTED), screen_label(i, screen))
            act.setToolTip("Fills that screen. Press Esc to close it.")
            act.triggered.connect(lambda _=False, sc=screen: self.open_projector(canvas_id, sc, kind, ref))
        menu.addAction(icons.icon("panel", theme.MUTED), "In a window").triggered.connect(
            lambda: self.open_projector(canvas_id, None, kind, ref))
        if any(p.target == target for p in self.projectors):
            menu.addSeparator()
            menu.addAction("Close pop-outs").triggered.connect(
                lambda: [p.close() for p in list(self.projectors) if p.target == target])

    def _live_bloom_menu(self, menu: QMenu, scene_id: str) -> None:
        """Live's bloom tile menu (clicking the tile puts it on air): how it comes in, a screenshot, pop out,
        its name and key."""
        from sprigcast.app.transitions_panel import KINDS
        from sprigcast.core.scene import ANY

        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return
        into = menu.addMenu(icons.icon("leaf", theme.MUTED), "Comes in with")
        into.setToolTipsVisible(True)
        rule = next((r for r in self.collection.transition_rules
                     if r.from_scene == ANY and r.to_scene == scene_id), None)
        usual = into.addAction(f"Usual ({self.collection.transition.kind.capitalize()})")
        usual.setCheckable(True)
        usual.setChecked(rule is None)
        usual.triggered.connect(lambda: self._bloom_transition(scene_id, None))
        into.addSeparator()
        for kind, label, tip in KINDS:
            if kind == "stinger":  # needs a clip: set it up in the rules
                continue
            act = into.addAction(label)
            act.setToolTip(tip)
            act.setCheckable(True)
            act.setChecked(rule is not None and rule.transition.kind == kind)
            act.triggered.connect(lambda _=False, k=kind: self._bloom_transition(scene_id, k))
        into.addSeparator()
        more = getattr(self, "live_transitions", None)
        if more is not None:
            into.addAction("More, like a stinger…").triggered.connect(more.open_rules)
        shot = menu.addAction(icons.icon("camera", theme.MUTED), "Save screenshot")
        shot.setToolTip("A PNG of this bloom, on air or not, in your Screenshots folder")
        shot.triggered.connect(lambda: self._bloom_screenshot(scene_id))
        self._projector_menu(menu, "bloom", scene_id)
        menu.addSeparator()
        menu.addAction("Rename…").triggered.connect(lambda: self._rename_bloom(scene_id))
        menu.addAction(icons.icon("key", theme.MUTED), "Set a key…").triggered.connect(
            lambda: self._open_settings("Hotkeys"))

    def _bloom_transition(self, scene_id: str, kind: str | None) -> None:
        """Going to this bloom uses `kind` (None: the usual transition), from any bloom."""
        from sprigcast.core.scene import ANY, Transition, TransitionRule

        rules = [r for r in self.collection.transition_rules if not (r.from_scene == ANY and r.to_scene == scene_id)]
        if kind is not None:
            usual = self.collection.transition
            rules.append(TransitionRule(from_scene=ANY, to_scene=scene_id,
                                        transition=Transition(kind=kind, duration_ms=usual.duration_ms)))
        self.collection.transition_rules = rules
        panel = getattr(self, "live_transitions", None)
        if panel is not None:
            panel.show_rule_count()
        self._changed()

    def _bloom_screenshot(self, scene_id: str) -> None:
        """Save a PNG of one bloom, drawn once off air if need be (an engine view, gone right after)."""
        from sprigcast.app import screenshot

        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return
        self.engine.set_view("screenshot", scene.canvas, "bloom", scene_id)
        try:
            image = screenshot.grab(self.engine, ["view:screenshot"]).get("view:screenshot")
        finally:
            self.engine.set_view("screenshot", None)
        if image is None:
            self._say("Couldn't take a screenshot: nothing is drawing.")
            return
        folder = self._recordings_folder() / "Screenshots"
        path = screenshot.save(image, folder, scene.name)
        self._say(f"Screenshot saved to {path}")
        self._tidy_saves()

    def _rename_bloom(self, scene_id: str) -> None:
        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", f"{cap(SHOW)} name:", text=scene.name)
        if ok and name.strip() and name.strip() != scene.name:
            scene.name = name.strip()
            self._changed()
            self._refresh_all()

    def _pop_out_menu(self, menu: QMenu) -> QMenu:
        """ "Pop out": show it big on another screen, or in its own window (a projector)."""
        sub = menu.addMenu(icons.icon("monitor", theme.MUTED), "Pop out")
        sub.menuAction().setToolTip("Show it on its own: full screen on a monitor, or in a window")
        return sub

    def _projector_menu(self, menu: QMenu, kind: str, ref: str) -> None:
        """Add "Pop out" for a bloom, sprig or source."""
        if kind == "bloom":
            scene = next((sc for sc in self.collection.scenes if sc.id == ref), None)
            if scene is None:
                return
            canvas_id = scene.canvas
        else:  # a source: on the shape of the bloom it's in
            scene = self._scene()
            canvas_id = scene.canvas if scene is not None else self.collection.canvases[0].id
        self._fill_projector_menu(self._pop_out_menu(menu), canvas_id, kind, ref)

    def open_projector(self, canvas_id: str, screen, kind: str = "canvas", ref: str = ""):
        """Show a canvas (or a bloom, sprig or source on that canvas's size) full screen on `screen`
        (None: in a window). One per screen: a new one replaces it."""
        from sprigcast.app.projector import Projector

        if screen is not None:
            for old in [p for p in self.projectors if p.screen() is screen and p.isFullScreen()]:
                old.close()
        if kind == "canvas":
            canvas = self.collection.canvas(canvas_id)
            projector = Projector(canvas_id, canvas_id.capitalize() if canvas else canvas_id, screen,
                                  self._projector_closed)
        else:
            self._projector_count = getattr(self, "_projector_count", 0) + 1
            view = f"projector{self._projector_count}"
            if kind == "bloom":
                title = next((sc.name for sc in self.collection.scenes if sc.id == ref), ref)
            else:
                config = next((c for c in self.collection.sources if c.id == ref), None)
                title = config.name if config is not None else ref
            projector = Projector(canvas_id, title, screen, self._projector_closed, view=view)
            self.engine.set_view(view, canvas_id, kind, ref)
            self.engine.add_frame_tap(f"view:{view}", projector.tap)
        projector.target = (kind, ref or canvas_id)
        self.projectors.append(projector)
        return projector

    def _projector_closed(self, projector) -> None:
        if projector in self.projectors:
            self.projectors.remove(projector)
        if projector.view is not None:
            self.engine.remove_frame_tap(f"view:{projector.view}", projector.tap)
            self.engine.set_view(projector.view, None)

    def _fill_capture_sound_menu(self, menu: QMenu, capture_id: str) -> None:
        """A capture's own sound, like OBS's Capture audio: none, the app's own, or an output such as
        Elgato Wave Link's Game channel."""
        from sprigcast.core.capture_sound import APP, sound_choice
        from sprigcast.sources.audio import device_label

        current = sound_choice(self.collection, capture_id)
        group = QActionGroup(menu)
        choices = [(None, "No sound of its own"), (APP, "The app's own sound")]
        devices = [d.name for d in list_audio_devices(loopback=True, rescan=False)]
        if current not in (None, APP) and current not in devices:
            devices.append(current)  # not plugged in right now: still shown, ticked
        for i, (value, text) in enumerate(choices + [(d, device_label(d)) for d in devices]):
            if i == 2:
                menu.addSeparator()
                note = menu.addAction("From an output")
                note.setEnabled(False)
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(value == current)
            group.addAction(act)
            act.triggered.connect(lambda _=False, v=value: self._set_capture_sound(capture_id, v))

    def _set_capture_sound(self, capture_id: str, choice: str | None) -> None:
        from sprigcast.core.capture_sound import set_capture_sound

        capture = self.collection.source(capture_id)
        sound = set_capture_sound(self.collection, capture, choice)
        self._changed()
        self._refresh_audio()
        if sound is not None:
            self.statusBar().showMessage(f"{sound.name!r} is in Sound now, with its own volume and tracks.", 6000)

    def _source_menu(self, on_item: bool) -> QMenu:
        menu = QMenu(self)
        if not on_item:
            self._fill_add_source_menu()
            menu.addMenu(self.add_source_menu).setText("Add")
        else:
            item = self._item()
            if item is not None and self.collection.source(item.source).kind == PART_KIND:
                menu.addAction(f"Open {PART}").triggered.connect(self._open_part_item)
                self._fill_tweaks_menu(menu.addMenu("Just in this " + SHOW), item.source)
            if item is not None and self.collection.source(item.source).kind in ("window_capture", "game_capture"):
                self._fill_capture_sound_menu(menu.addMenu("Sound"), item.source)
            if item is not None and self.collection.source(item.source).kind == "browser" and self.browser_host:
                menu.addAction("Refresh page").triggered.connect(
                    lambda _=False, sid=item.source: self.browser_host.reload(sid))
                menu.addAction("Interact…").triggered.connect(
                    lambda _=False, sid=item.source: self._interact(sid))
            if item is not None and self.collection.source(item.source).kind not in self.AUDIO_KINDS:
                if self.collection.source(item.source).kind == PART_KIND:
                    part = self.collection.source(item.source).settings.get("scene", "")
                    self._projector_menu(menu, "bloom", part)
                else:
                    self._projector_menu(menu, "source", item.source)
            count = len(self.source_list.selectedItems())
            turn = menu.addAction(f"Turn into {a(PART)}…" if count < 2 else f"Turn these {count} into {a(PART)}…")
            turn.setToolTip(f"Move the selected sources into a new reusable {PART}, right where they are")
            turn.triggered.connect(self._turn_into_part)
            menu.addSeparator()
            acts = self.item_actions
            for key in ("settings", "filters", "rename"):
                menu.addAction(acts[key])
            if item is not None and self.collection.source(item.source).kind not in self.AUDIO_KINDS:
                self._fill_quick_filter_menu(menu.addMenu("Shape"), item.source, "shape", "shape", SHAPES)
                self._fill_quick_filter_menu(menu.addMenu("Color look"), item.source, "lut", "look", LOOKS)
            if item is not None and self.collection.source(item.source).kind == "media":
                menu.addSeparator()
                self._fill_media_menu(menu, item.source)
            if item is not None:
                self._fill_plugin_menu(menu, item.source)
            menu.addSeparator()
            size = menu.addMenu("Size and place")
            for key in ("fit", "fill", "stretch", "center", "reset", "uncrop"):
                size.addAction(acts[key])
            size.addSeparator()
            hint = size.addAction("Crop: hold Alt and drag a side")
            hint.setEnabled(False)
            order = menu.addMenu("Order")
            for key in ("front", "up", "down", "back"):
                order.addAction(acts[key])
            acts["lock"].setCheckable(True)
            acts["lock"].setChecked(bool(item and item.locked))
            menu.addAction(acts["hide"])
            menu.addAction(acts["lock"])
            menu.addSeparator()
            menu.addAction(acts["remove"])
        return menu

    def _scene_context_menu(self, pos) -> None:
        menu = QMenu(self)
        under = self.scene_tree.itemAt(pos)
        folder_id = under.data(0, ROLE_FOLDER) if under is not None else None
        if folder_id:
            self._folder_menu(menu, self.collection.folder(folder_id))
            menu.exec(self.scene_tree.viewport().mapToGlobal(pos))
            return
        if under is not None and under.data(0, ROLE_ROW) == "sound":
            self._fill_sound_menu(menu, under.data(0, ROLE_ID))
            menu.exec(self.scene_tree.viewport().mapToGlobal(pos))
            return
        if under is not None and self._scene() is not None:
            scene = self._scene()
            if scene.kind == "show":
                menu.addAction("Put on air").triggered.connect(self._make_active)
                menu.addAction("Duplicate").triggered.connect(lambda _=False, sid=scene.id: self._copy_bloom(sid))
                self._fill_move_menu(menu.addMenu("Move to folder"), scene)
                if scene.canvas != self.collection.canvases[0].id:
                    link = menu.addMenu(icons.icon("link"), "Switch together with")
                    self._fill_link_menu(link)
            else:
                part = self.collection.main_part(scene)
                for canvas in self.collection.canvases:
                    show = self.collection.active_scene(canvas.id)
                    if show is None:
                        continue
                    action = menu.addAction(f"Add to “{literal(show.name)}”  ({canvas.id})")
                    if self.collection.contains(part, show.id):
                        action.setEnabled(False)
                    action.triggered.connect(lambda _=False, pid=part.id, sid=show.id: self._add_part_source(pid, sid))
                menu.addSeparator()
                states = menu.addAction("Live states…" + (f"  ({len(part.states)})" if part.states else ""))
                states.setToolTip(f"Looks for this {PART} that you switch between in Live, like “Alert showing”")
                states.triggered.connect(lambda _=False, pid=part.id: self._edit_states(pid))
                self._fill_move_menu(menu.addMenu("Move to folder"), part)
                menu.addSeparator()
            self._projector_menu(menu, "bloom", scene.id)
            menu.addSeparator()
            menu.addAction("Rename…").triggered.connect(self._rename_scene)
            menu.addAction("Remove").triggered.connect(self._remove_scene)
            menu.addSeparator()
        new = menu.addMenu("New")
        new.addAction(cap(SHOW)).triggered.connect(lambda: self._add_scene())
        new.addAction(f"{cap(SHOW)} folder").triggered.connect(lambda: self._add_folder())
        new.addSeparator()
        new.addAction(cap(PART)).triggered.connect(lambda: self._add_part())
        new.addAction(f"{cap(PART)} folder").triggered.connect(lambda: self._add_folder(holds="part"))
        menu.exec(self.scene_tree.viewport().mapToGlobal(pos))

    # Sounds in Build's library --------------------------------------------------------

    def _fill_sound_menu(self, menu: QMenu, source_id: str) -> None:
        """A sound's right-click menu: its own short list, not the bloom one."""
        config = next((c for c in self.collection.sources if c.id == source_id), None)
        if config is None:
            return
        menu.setToolTipsVisible(True)
        menu.addAction("Rename…").triggered.connect(lambda _=False: self._rename_sound(source_id))
        muted = bool(config.settings.get("muted", False))
        menu.addAction("Unmute" if muted else "Mute").triggered.connect(
            lambda _=False: self._build_tune_changed(source_id, {"muted": not muted}))
        self._fill_plays_in_menu(menu.addMenu("Plays in"), source_id)
        menu.addSeparator()
        menu.addAction("Volume and sound…").triggered.connect(lambda _=False: self._select_sound(source_id))
        menu.addAction("Filters…").triggered.connect(lambda _=False: self._open_filters(source_id, "audio"))
        menu.addAction("Tracks and sync…").triggered.connect(lambda _=False: self._open_settings("Audio/Sounds"))
        if config.kind == "media":
            self._fill_media_menu(menu.addMenu("Playback"), source_id)
        menu.addSeparator()
        remove = menu.addAction("Remove")
        placed = [s for s in self.collection.scenes if any(i.source == source_id for i in s.items)]
        if placed:
            remove.setEnabled(False)
            remove.setToolTip(f"It's in {self._names(placed)}: remove it there")
        else:
            remove.triggered.connect(lambda _=False: self._remove_sound(source_id))

    def _fill_plays_in_menu(self, menu: QMenu, source_id: str) -> None:
        """Tick the blooms a sound plays in. None ticked (or Every bloom) means it plays everywhere."""
        config = self.collection.source(source_id)
        picked = set(self.collection.sound_blooms(config))
        every = menu.addAction(f"Every {SHOW}")
        every.setCheckable(True)
        every.setChecked(not picked)
        every.triggered.connect(lambda _=False: self._set_sound_blooms(source_id, []))
        menu.addSeparator()
        several = len(self.collection.canvases) > 1
        first = self.collection.canvases[0].id
        for bloom in self.collection.bloom_choices():
            label = bloom.name if not several or bloom.canvas == first else f"{bloom.name}  ({bloom.canvas})"
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(bloom.id in picked)
            action.triggered.connect(lambda on, b=bloom.id: self._set_sound_blooms(
                source_id, [x for x in picked if x != b] + ([b] if on else [])))

    def _set_sound_blooms(self, source_id: str, blooms: list[str]) -> None:
        config = self.collection.source(source_id)
        order = [b.id for b in self.collection.bloom_choices()]
        settings = {k: v for k, v in config.settings.items() if k != "blooms"}
        if blooms and set(blooms) != set(order):  # every bloom ticked is the same as Every bloom
            settings["blooms"] = sorted(blooms, key=order.index)
        config.settings = settings
        self._changed()
        self._refresh_scenes()
        self._refresh_audio()
        if self.selected_sound_id == source_id:
            self._open_build_sound(source_id)

    def _plays_in_text(self, config) -> str:
        blooms = self.collection.sound_blooms(config)
        if not blooms:
            return f"Plays in every {SHOW}"
        return "Plays in " + self._names([self.collection.scene(b) for b in blooms])

    def _plays_in_tag(self, config) -> str:
        """The quiet words on a sound's row: nothing for every bloom, else the bloom or how many."""
        blooms = self.collection.sound_blooms(config)
        if not blooms:
            return ""
        return self.collection.scene(blooms[0]).name if len(blooms) == 1 else f"{len(blooms)} {SHOWS}"

    @staticmethod
    def _names(scenes) -> str:
        names = [s.name for s in scenes]
        return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

    def _select_sound(self, source_id: str) -> None:
        self._open_build_sound(source_id)

    def _rename_sound(self, source_id: str) -> None:
        config = self.collection.source(source_id)
        name, ok = QInputDialog.getText(self, "Rename sound", "Name:", text=config.name)
        if ok and name.strip() and name.strip() != config.name:
            config.name = name.strip()
            self._changed()
            self._refresh_scenes()
            self._refresh_audio()
            if self.selected_sound_id == source_id:
                self._open_build_sound(source_id)

    def _remove_sound(self, source_id: str) -> None:
        """Remove a sound that isn't placed in any bloom (a mic, desktop or app sound, a sound file)."""
        if self.selected_sound_id == source_id:
            self._close_build_sound(refresh=False)
        audio = self.collection.audio
        if audio.global_sources is not None and source_id in audio.global_sources:
            audio.global_sources = [i for i in audio.global_sources if i != source_id]
        elif audio.global_sources is None and source_id in self.collection.global_device_ids():
            audio.global_sources = [i for i in self.collection.global_device_ids() if i != source_id]
        self.collection.remove_source_if_unused(source_id)
        self._changed()
        self._refresh_scenes()
        self._refresh_audio()

    # Folders -------------------------------------------------------------------------

    def _folder_menu(self, menu: QMenu, folder) -> None:
        sprigs = folder.holds == "part"
        add = self._add_part if sprigs else self._add_scene
        menu.addAction(f"New {PART if sprigs else SHOW} here…").triggered.connect(
            lambda: (setattr(self, "selected_folder_id", folder.id), add()))
        menu.addSeparator()
        if not sprigs:
            live = menu.addAction("Show in Live")
            live.setCheckable(True)
            live.setChecked(folder.live)
            live.setToolTip(f"Its {SHOWS} become the buttons in Live mode")
            live.toggled.connect(lambda on: self._set_folder_live(folder.id, on))
        menu.addAction("Rename…").triggered.connect(lambda: self._rename_folder(folder.id))
        menu.addSeparator()
        menu.addAction(colour_row(menu, folder.colour, lambda c: self._recolour_folder(folder.id, c)))
        menu.addSeparator()
        remove = menu.addAction("Remove folder")
        remove.setToolTip(f"Its {PARTS if sprigs else SHOWS} stay, out of the folder")
        remove.triggered.connect(lambda: self._remove_folder(folder.id))

    def _fill_move_menu(self, menu: QMenu, scene: Scene) -> None:
        loose = menu.addAction("No folder")
        loose.setCheckable(True)
        loose.setChecked(scene.folder is None)
        loose.triggered.connect(lambda: self._move_to_folder(scene.id, None))
        folders = self.collection.part_folders() if scene.kind == "part" else self.collection.folders_for(scene.canvas)
        if folders:
            menu.addSeparator()
        for folder in folders:
            action = menu.addAction(_dot(folder.colour, 14), literal(folder.name))
            action.setCheckable(True)
            action.setChecked(scene.folder == folder.id)
            action.triggered.connect(lambda _=False, fid=folder.id: self._move_to_folder(scene.id, fid))
        menu.addSeparator()
        menu.addAction("New folder…").triggered.connect(lambda: self._add_folder(move=scene.id, holds=scene.kind))

    def _add_folder(self, move: str | None = None, holds: str = "show") -> None:
        example = "Browser Scenes" if holds == "part" else f"Main {cap(SHOWS)}"
        title = f"New {PART} folder" if holds == "part" else "New folder"
        name, ok = QInputDialog.getText(self, title, f"Folder name (e.g. {example}):")
        if not ok or not name.strip():
            return
        folder = self.collection.add_folder(self.build_canvas, name.strip(), holds=holds)
        self.selected_folder_id = folder.id
        if move is not None:
            self.collection.scene(move).folder = folder.id
        else:
            self.selected_scene_id = None  # select the new folder, ready for "New bloom"
        self._changed()
        self._refresh_scenes()
        self._refresh_sources()

    def _rename_folder(self, folder_id: str) -> None:
        folder = self.collection.folder(folder_id)
        name, ok = QInputDialog.getText(self, "Rename folder", "Folder name:", text=folder.name)
        if ok and name.strip():
            folder.name = name.strip()
            self._changed()
            self._refresh_scenes()

    def _set_folder_live(self, folder_id: str, on: bool) -> None:
        self.collection.folder(folder_id).live = on
        self._changed()
        self._refresh_scenes()

    def _pick_folder_colour(self, folder_id: str, at) -> None:
        """Click a folder's dot: a small palette to recolour it."""
        menu = QMenu(self)
        menu.addAction(colour_row(menu, self.collection.folder(folder_id).colour,
                                  lambda c: self._recolour_folder(folder_id, c)))
        menu.exec(at)

    def _recolour_folder(self, folder_id: str, colour: str) -> None:
        self.collection.folder(folder_id).colour = colour
        self._changed()
        self._refresh_scenes()

    def _remove_folder(self, folder_id: str) -> None:
        self.collection.remove_folder(folder_id)
        if self.selected_folder_id == folder_id:
            self.selected_folder_id = None
        self._changed()
        self._refresh_scenes()

    def _move_to_folder(self, scene_id: str, folder_id: str | None) -> None:
        self.collection.scene(scene_id).folder = folder_id
        self.selected_folder_id = folder_id
        self._changed()
        self._refresh_scenes()

    def _move_scene(self, scene_id: str, step: int) -> None:
        """Swap with the neighbour in the same list (same canvas and folder)."""
        scenes = self.collection.scenes
        scene = self.collection.scene(scene_id)
        group = [i for i, s in enumerate(scenes)
                 if s.kind == "show" and s.canvas == scene.canvas and s.folder == scene.folder]
        at = group.index(scenes.index(scene))
        if not 0 <= at + step < len(group):
            return
        i, j = group[at], group[at + step]
        scenes[i], scenes[j] = scenes[j], scenes[i]
        self._changed()
        self._refresh_scenes()

    def _scenes_dropped(self) -> None:
        """Read the order, folders and folder order back from the library after a drag. Anything odd is undone."""
        def read(heading_key: str) -> tuple[list[tuple[str, str | None]], list[str]] | None:
            top = next((self.scene_tree.topLevelItem(i) for i in range(self.scene_tree.topLevelItemCount())
                        if self.scene_tree.topLevelItem(i).data(0, ROLE_FOLDER) == heading_key), None)
            placed: list[tuple[str, str | None]] = []
            folder_order: list[str] = []
            if top is None:
                return placed, folder_order
            for i in range(top.childCount()):
                row = top.child(i)
                if row.data(0, ROLE_ID):
                    if row.childCount():
                        return None  # dropped onto a bloom or sprig
                    placed.append((row.data(0, ROLE_ID), None))
                elif row.data(0, ROLE_FOLDER):
                    folder_id = row.data(0, ROLE_FOLDER)
                    folder_order.append(folder_id)
                    for j in range(row.childCount()):
                        child = row.child(j)
                        if not child.data(0, ROLE_ID) or child.childCount():
                            return None  # a folder in a folder, or something dropped onto a row
                        placed.append((child.data(0, ROLE_ID), folder_id))
            return placed, folder_order

        shows, parts = self.collection.scenes_for(self.build_canvas), self.collection.parts()
        show_folders = [f.id for f in self.collection.folders_for(self.build_canvas)]
        part_folders = [f.id for f in self.collection.part_folders()]
        got_shows, got_parts = read(""), read(PART_ROOT)
        if (got_shows is None or got_parts is None
                or sorted(sid for sid, _ in got_shows[0]) != sorted(s.id for s in shows)
                or sorted(pid for pid, _ in got_parts[0]) != sorted(p.id for p in parts)
                or sorted(got_shows[1]) != sorted(show_folders) or sorted(got_parts[1]) != sorted(part_folders)):
            self._refresh_scenes()  # dropped somewhere it can't go (a bloom among sprigs, onto a row): put it back
            return
        for group, placed in ((shows, got_shows[0]), (parts, got_parts[0])):
            slots = [i for i, s in enumerate(self.collection.scenes) if s in group]
            by_id = {s.id: s for s in group}
            for slot, (scene_id, folder_id) in zip(slots, placed, strict=True):
                scene = by_id[scene_id]
                scene.folder = folder_id
                self.collection.scenes[slot] = scene
        for order in (got_shows[1], got_parts[1]):  # folders keep their new order, in the slots they had
            folders = self.collection.folders
            slots = [i for i, f in enumerate(folders) if f.id in order]
            by_id = {f.id: f for f in folders}
            for slot, folder_id in zip(slots, order, strict=True):
                folders[slot] = by_id[folder_id]
        self._changed()
        self._refresh_scenes()
        self.live_panel.show_scenes(self.collection)

    def _folder_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        folder_id = item.data(0, ROLE_FOLDER)
        if self._updating or not folder_id:
            return
        self.collection.folder(folder_id).collapsed = not expanded
        self._changed()

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
        """Position, scale and rotation live in Build's inspector now."""
        self._set_mode("build")
        self.spin["x"].setFocus()

    def _build_properties_dock(self) -> None:
        """Build's inspector (right column): the selected thing's name, where it sits, its look,
        and which shapes show it."""
        from sprigcast.app.sound_card import TunePage

        self.build_tune = TunePage(self._build_tune_changed, lambda sid: self._open_filters(sid, "audio"),
                                   self._close_build_sound, self._pick_app_sound_apps,
                                   on_blooms=lambda _sid: self._plays_in_popup())
        self.build_stage.sound_slot.addWidget(self.build_tune)
        self.build_stage.sound_slot.addStretch(1)
        right = self.build_stage.right
        panel = QWidget()
        panel.setStyleSheet(theme.SOFT_FIELD_STYLE)
        col = QVBoxLayout(panel)
        col.setContentsMargins(6, 0, 16, 8)  # breathing room from the window edge
        col.setSpacing(10)

        self.prop_sections: list[QWidget] = []  # hidden while nothing is selected
        self.prop_section_toggles: dict[str, tuple[QPushButton, QWidget]] = {}

        from sprigcast.app.inspector_fields import Card, NumberField, link_button

        def section(title: str) -> QVBoxLayout:
            """A foldable rounded card (remembered open or closed). Only Place starts open."""
            card = Card(title, open_by_default=title == "Place")
            col.addWidget(card)
            self.prop_sections.append(card)
            self.prop_section_toggles[title] = (card.header, card.body)
            return card.box

        top = QVBoxLayout()
        top.setSpacing(2)
        self.prop_name = QLineEdit()
        self.prop_name.setPlaceholderText("Pick something to change it")
        self.prop_name.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; padding: 2px 0; color: {theme.TEXT_BRIGHT};"
            f" font-size: 17px; font-weight: 600; }} QLineEdit:focus {{ background: {theme.PANEL}; }}")
        self.prop_name.editingFinished.connect(self._on_name_edited)
        top.addWidget(self.prop_name)
        self.prop_status = QLabel("")
        self.prop_status.setWordWrap(True)
        self.prop_status.setStyleSheet(theme.MUTED_STYLE)
        top.addWidget(self.prop_status)
        # A problem, said plainly, with the one button that fixes it.
        self.prop_problem = QFrame()
        self.prop_problem.setObjectName("propProblem")
        self.prop_problem.setStyleSheet(f"#propProblem {{ background: {theme.CARD}; border-radius: 14px; }}")
        problem_box = QVBoxLayout(self.prop_problem)
        problem_box.setContentsMargins(12, 10, 12, 10)
        problem_box.setSpacing(8)
        self.prop_problem_text = QLabel()
        self.prop_problem_text.setWordWrap(True)
        self.prop_problem_text.setStyleSheet(f"color: {theme.WARN};")
        problem_box.addWidget(self.prop_problem_text)
        self.prop_fix = QPushButton("Fix")
        self.prop_fix.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.prop_fix.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prop_fix.clicked.connect(self._fix_problem)
        problem_box.addWidget(self.prop_fix, 0, Qt.AlignmentFlag.AlignLeft)
        self.prop_problem.hide()
        top.addWidget(self.prop_problem)
        # A camera another app makes (Camera Hub, OBS...): the real one is much lighter on the GPU.
        self.prop_vcam = QFrame()
        self.prop_vcam.setObjectName("propVcam")
        self.prop_vcam.setStyleSheet(f"#propVcam {{ background: {theme.SELECT_BG}; border-radius: 16px; }}"
                                     f"QLabel {{ color: {theme.SELECT_TEXT}; background: transparent; }}")
        vcam_box = QVBoxLayout(self.prop_vcam)
        vcam_box.setContentsMargins(14, 12, 12, 12)
        vcam_box.setSpacing(10)
        vcam_text = QLabel("Virtual cameras use extra GPU. Your real camera is much lighter.")
        vcam_text.setWordWrap(True)
        vcam_box.addWidget(vcam_text)
        vcam_row = QHBoxLayout()
        vcam_row.setSpacing(8)
        self.prop_vcam_use = QPushButton("Use the real camera")
        self.prop_vcam_use.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 15px; padding: 7px 14px; }")
        self.prop_vcam_use.clicked.connect(self._use_real_camera)
        vcam_hide = QPushButton("Hide")
        vcam_hide.setStyleSheet(theme.LINK_STYLE + f"QPushButton {{ color: {theme.SELECT_TEXT}; }}")
        vcam_hide.clicked.connect(self._hide_vcam_tip)
        for button in (self.prop_vcam_use, vcam_hide):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            vcam_row.addWidget(button)
        vcam_row.addStretch(1)
        vcam_box.addLayout(vcam_row)
        self.prop_vcam.hide()
        top.addWidget(self.prop_vcam)
        # A sprig placement: open it, and see (and jump to) every bloom it's in.
        self.prop_sprig = QWidget()
        sprig_box = QVBoxLayout(self.prop_sprig)
        sprig_box.setContentsMargins(0, 10, 0, 0)
        sprig_box.setSpacing(8)
        self.prop_open_sprig = QPushButton(f"Open {PART} to edit inside")
        self.prop_open_sprig.setIcon(icons.icon("sprig", theme.ACCENT_TEXT, 16))
        self.prop_open_sprig.setStyleSheet(theme.PRIMARY_STYLE + "QPushButton { border-radius: 17px; padding: 9px 14px; }")
        self.prop_open_sprig.setCursor(Qt.CursorShape.PointingHandCursor)
        self.prop_open_sprig.setToolTip(f"Edit what's inside this {PART} (or double-click it)")
        self.prop_open_sprig.clicked.connect(self._open_part_item)
        sprig_box.addWidget(self.prop_open_sprig)
        self.prop_used_in = QLabel()
        self.prop_used_in.setWordWrap(True)
        self.prop_used_in.setTextFormat(Qt.TextFormat.RichText)
        self.prop_used_in.setStyleSheet(theme.MUTED_STYLE)
        self.prop_used_in.linkActivated.connect(self._jump_to_bloom)
        sprig_box.addWidget(self.prop_used_in)
        self.prop_sprig.hide()
        top.addWidget(self.prop_sprig)
        from sprigcast.app.empty import EmptyState

        self.prop_empty = EmptyState("Click something on the canvas or in Leaves to move it, change its look, "
                                     "or see which shapes show it.", scale=0.8)
        top.addWidget(self.prop_empty)
        col.addLayout(top)

        place = section("Place")
        from sprigcast.app.pin_grid import PinGrid
        from sprigcast.app.starter import combo_style

        self.spin: dict[str, QDoubleSpinBox] = {}

        def field(key: str, label: str, low: float, high: float, decimals: int = 0, suffix: str = "") -> NumberField:
            made = NumberField(label, low, high, decimals, suffix,
                               lambda value, k=key: self._on_transform_edited(k, value))
            self.spin[key] = made.box
            return made

        def pair(*widgets) -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(8)
            for widget in widgets:
                row.addWidget(widget, 0 if isinstance(widget, QPushButton) else 1)
            return row

        place.addLayout(pair(field("x", "X", -20000, 20000), field("y", "Y", -20000, 20000)))
        self.keep_shape = link_button(QSettings().value("edit/keep_aspect", True, type=bool), self._set_keep_shape)
        width, height = field("w", "W", 0, 40000), field("h", "H", 0, 40000)
        for key in ("w", "h"):
            self.spin[key].setSpecialValueText("–")  # shown until the source's size is known
        place.addLayout(pair(width, self.keep_shape, height))
        self.pin_grid = PinGrid(lambda align: self._on_pin_picked("align", align))
        self.pin_grid.setToolTip("Pin point: it turns around here")
        turn = QHBoxLayout()
        turn.setSpacing(12)
        turn.addWidget(field("rotation", "Turn", -360, 360, 1, "°"), 1, Qt.AlignmentFlag.AlignVCenter)
        turn.addWidget(self.pin_grid)
        place.addLayout(turn)

        bounds = section("Bounds")
        self.bounds_mode = QComboBox()
        self.bounds_mode.setStyleSheet(combo_style() + f"QComboBox {{ background: {theme.CARD};"
                                       f" border-color: {theme.EDGE}; border-radius: 16px; padding-left: 14px; }}")
        self.bounds_mode.setFixedHeight(36)
        self.bounds_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        for key, text in (("off", "Off"), ("fit", "Fit inside"), ("fill", "Fill and crop"), ("stretch", "Stretch")):
            self.bounds_mode.addItem(text, key)
        self.bounds_mode.currentIndexChanged.connect(lambda _i: self._on_bounds_mode())
        self.bounds_grid = PinGrid(lambda align: self._on_pin_picked("bounds_align", align))
        self.bounds_grid.setToolTip("Where it sits in its box")
        brow = QHBoxLayout()
        brow.setSpacing(12)
        brow.addWidget(self.bounds_mode, 1, Qt.AlignmentFlag.AlignVCenter)
        brow.addWidget(self.bounds_grid)
        bounds.addLayout(brow)

        crop = section("Crop")
        self.crop_spin: dict[int, QDoubleSpinBox] = {}
        crop_fields = []
        for i, label in enumerate(("Left", "Top", "Right", "Bottom")):
            made = NumberField(label, 0, 20000, 0, "", lambda value, side=i: self._on_crop_edited(side, int(value)))
            made.setToolTip("Or hold Alt and drag a side on the canvas")
            self.crop_spin[i] = made.box
            crop_fields.append(made)
        crop.addLayout(pair(crop_fields[0], crop_fields[2]))
        crop.addLayout(pair(crop_fields[1], crop_fields[3]))
        self.prop_locked = QCheckBox("Locked (clicks pass through it)")
        self.prop_locked.toggled.connect(self._on_locked_toggled)
        self.prop_locked.hide()  # kept for Ctrl+L; the lock lives on each Leaves row

        look = section("Look")
        chips = QHBoxLayout()
        chips.setSpacing(8)
        chip_style = theme.SMALL_PILL_STYLE + (
            f"QPushButton {{ background: {theme.CARD}; border: 1px solid {theme.EDGE}; border-radius: 16px;"
            f" padding: 8px 14px; }} QPushButton:hover {{ background: {theme.RAISED};"
            f" border-color: {theme.EDGE_STRONG}; }}")
        self.prop_extra = QPushButton("")
        self.prop_extra.clicked.connect(self._edit_source_settings)
        self.prop_filters = QPushButton("Filters")
        self.prop_filters.setToolTip("Color fix, chroma key, blur and more")
        self.prop_filters.clicked.connect(self._edit_filters)
        self.prop_fit = QPushButton("Fit")
        self.prop_fit.setToolTip("Fit to canvas (Ctrl+F)")
        self.prop_fit.clicked.connect(self._fit_item)
        self.prop_uncrop = QPushButton("Uncrop")
        self.prop_uncrop.setToolTip("Show the whole picture again")
        self.prop_uncrop.clicked.connect(self._reset_crop)
        for chip in (self.prop_extra, self.prop_filters, self.prop_fit, self.prop_uncrop):
            chip.setStyleSheet(chip_style)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chips.addWidget(chip)
        chips.addStretch(1)
        look.addLayout(chips)

        shapes = section("Shapes")
        self.prop_shapes: dict[str, QLabel] = {}
        for canvas in self.collection.canvases:
            row = QHBoxLayout()
            row.addWidget(QLabel(canvas.id.capitalize()), 1)
            state = QLabel("")
            row.addWidget(state)
            self.prop_shapes[canvas.id] = state
            shapes.addLayout(row)
        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(panel)
        right.addWidget(scroll, 1)

    def _fill_bloom_tools(self) -> None:
        """Above the canvas, on the right: Put on air, Link (vertical blooms) and Safe zones."""
        style = theme.SMALL_PILL_STYLE
        self.link_button.setText("⇄ Link")
        self.on_air_button = QPushButton("▶ Put on air")
        self.on_air_button.setToolTip("Show this bloom on its canvas now (and the blooms linked to it)")
        self.on_air_button.clicked.connect(self._put_on_air)
        for button in (self.on_air_button, self.link_button, self.safe_zone_button):
            button.setStyleSheet(style)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.build_bar.tools.addWidget(button)

    def _shape_states(self, item: SceneItem | None) -> dict[str, str]:
        """For the inspector: is the selected thing in each shape's bloom right now?"""
        scene = self._scene()
        states = {}
        for canvas in self.collection.canvases:
            if not canvas.enabled:
                states[canvas.id] = "Off"
                continue
            if item is None or scene is None:
                states[canvas.id] = ""
                continue
            if scene.kind == "show" and scene.canvas == canvas.id:
                states[canvas.id] = "Shown" if item.visible else "Hidden"
                continue
            other = self._twin_scene(canvas.id)
            match = next((i for i in other.items if i.source == item.source), None) if other else None
            states[canvas.id] = ("Shown" if match.visible else "Hidden") if match else "Not here"
        return states

    def _twin_scene(self, canvas_id: str) -> Scene | None:
        """The bloom a twin shows: the partner of the bloom being edited, else that shape's bloom on air."""
        scene = self._scene()
        if scene is not None and scene.kind == "show":
            partner = next((s for s in self.collection.linked_with(scene.id) if s.canvas == canvas_id), None)
            if partner is not None:
                return partner
        return self._shown_scene(canvas_id)

    def _edit_destination(self, output_id: str | None) -> None:
        """Open Settings → Stream on a destination. "new:<service>" (Live's + Add menu) adds one first."""
        if output_id and output_id.startswith("new:"):
            output_id = self._new_destination(output_id.split(":", 1)[1])
            if output_id is None:
                return  # ready to go: a Twitch account gives the key
        if output_id is None:
            first = next((o.id for o in self.collection.outputs if o.type == "stream"), None)
            output_id = first or "new"
        self._open_settings("Stream", output_id=output_id)

    def _new_destination(self, service_key: str) -> str | None:
        """Add a place to stream. Returns its id when it still needs a key or server (Settings opens on it)."""
        from sprigcast.output import twitch_auth

        service = SERVICES.get(service_key, SERVICES["custom"])
        taken = {o.id for o in self.collection.outputs} | self.gardens.taken_output_ids()  # ids name stream keys
        n = 1
        while f"out-{n}" in taken:
            n += 1
        canvas = service.canvas if any(c.id == service.canvas for c in self.collection.canvases) \
            else self.collection.canvases[0].id
        output = Output(id=f"out-{n}", type="stream", service=service_key, canvas=canvas,
                        bitrate_kbps=service.kbps, key_ref=f"sprigcast/out-{n}", enabled=True)
        if service_key == "twitch" and twitch_auth.connected_account():
            output.twitch_account = True
        self.collection.outputs.append(output)
        self.collection.save(self.scenes_path)
        self._refresh_stream_boxes()
        return None if output.twitch_account else output.id

    def _place_action(self, output_id: str, action: str) -> None:
        """Live's Edit mode: remove a place, swap its canvas, or flip Twitch's bandwidth test."""
        output = next((o for o in self.collection.outputs if o.id == output_id), None)
        if output is None or self.streaming is not None:
            return
        if action == "remove":
            self.collection.outputs.remove(output)
            if output.key_ref and not any(o.key_ref == output.key_ref for o in self.collection.outputs) \
                    and output.key_ref not in self.gardens.shelf.other_key_refs():
                try:
                    set_key(output.key_ref, "")  # its saved key goes too
                except Exception:  # no credential store: nothing saved to delete
                    log.debug("could not delete key %s", output.key_ref, exc_info=True)
        elif action == "shape":
            ids = [c.id for c in self.collection.canvases]
            output.canvas = ids[(ids.index(output.canvas) + 1) % len(ids)] if output.canvas in ids else ids[0]
            output.size = None  # the old size was for the other shape
        elif action == "test":
            output.bandwidth_test = not output.bandwidth_test
        self.collection.save(self.scenes_path)
        self._refresh_stream_boxes()

    def _build_live_stage(self) -> None:
        """Live mode's own screen (app/live_stage.py): no docks, the stage, bloom tiles and a side column."""
        self.live_panel = LivePanel(self._switch_to_scene, self._set_sprig_state, on_edit=self._edit_in_build)
        self.live_panel.on_project = self._live_bloom_menu
        self._build_transition_menu()
        self.live_side = LiveSide(self._on_live_clicked, self._on_record_clicked, self._on_clip_clicked,
                                  self._edit_destination, self._on_stream_toggled, self._place_action,
                                  lambda: self._open_settings(), self._on_sound_changed,
                                  lambda sid: self._open_filters(sid, "audio"), self._set_mic_first,
                                  self._toggle_camera, self._camera_settings)
        self.live_side.sound.on_settings = lambda: self._open_settings("Audio/Sounds")
        self.live_side.on_health = lambda: self._open_health()
        self.live_side.set_health_link(bool(self._health_records()))
        self.live_stage = LiveStage(self.live_side)
        self.live_tiles = QScrollArea()
        self.live_tiles.setWidgetResizable(True)
        self.live_tiles.setFrameShape(QFrame.Shape.NoFrame)
        self.live_tiles.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.live_tiles.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.live_tiles.setWidget(self.live_panel)
        self.live_tiles.viewport().installEventFilter(self)  # re-fits the tiles' height when the width changes
        self.live_stage.tiles_slot.addWidget(self.live_tiles, 1)
        self.live_panel.transition_chip.hide()  # the Transitions card beside the blooms shows it now
        self._build_live_transitions()
        self.modes.addWidget(self.live_stage)  # page 1
        self._thumbs_at = 0.0
        self._thumb_seen: dict[str, str | None] = {}  # Build: the bloom each preview showed at the last look
        self.live_side.set_clip(False, self.collection.recording.replay_seconds)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt naming)
        if event.type() == QEvent.Type.Resize and watched is self.live_tiles.viewport():
            QTimer.singleShot(0, self._fit_tiles)
        if event.type() == QEvent.Type.Resize and watched is getattr(self, "splitter", None) and self.mode == "live":
            QTimer.singleShot(0, self._share_stage)
        return super().eventFilter(watched, event)

    def _fit_tiles(self) -> None:
        """Nothing to fit any more (kept for older callers)."""
        # The blooms now sit in their own card of fixed height under the canvas and scroll inside it.

    def _place_canvases(self) -> None:
        """The canvas previews sit on Live's stage in Live mode and on Build's canvas card in Build mode."""
        if self.mode == "live":
            if self.splitter.parent() is not self.live_stage.card:
                self.live_stage.card.holder.addWidget(self.splitter)
                self.splitter.show()
            for preview in self.previews.values():
                preview.live_look = True
                preview.show_title = False  # Live: just the picture, no name or size above it
            QTimer.singleShot(0, self._fit_tiles)
            QTimer.singleShot(0, self._share_stage)
        else:
            if self.splitter.parent() is not self.canvas_page:
                self.canvas_page.layout().addWidget(self.splitter, 1)
                self.splitter.show()
            for preview in self.previews.values():
                preview.live_look = False
                preview.align = "center"
                preview.show_title = preview.twin or self.in_place is not None
        self.modes.setCurrentIndex(1 if self.mode == "live" else 0)
        self._sync_engine_staged()
        for preview in self.previews.values():
            preview.update()

    def _share_stage(self) -> None:
        """Give each canvas stage width in proportion to its shape, so a 16:9 and a 9:16 both fill the height."""
        shown = [c for c in self.collection.canvases if c.enabled]
        if len(shown) < 2:
            return
        total = self.splitter.width()
        order = [self.splitter.widget(i) for i in range(self.splitter.count())]
        by_column = {id(self.canvas_columns[c.id]): c for c in self.collection.canvases}
        ratios = {c.id: c.width / c.height for c in shown}
        if self.mode == "build":  # one shape at a time: it gets the whole card
            return
        canvases = [by_column[id(w)] for w in order]
        visible = [c for c in canvases if c in shown]
        # Each canvas as wide as the stage's height allows; any width left over goes to the outer
        # sides, half each, and the two canvases lean together so the gap between them stays small.
        height = max(1, self.splitter.height() - 24 - 6)
        needed = {c.id: ratios[c.id] * height + 12 for c in visible}
        spare = total - self.splitter.handleWidth() * (len(visible) - 1) - sum(needed.values())
        if spare > 0:
            sizes = [int(needed[c.id] + spare / 2) if c is visible[0] or c is visible[-1] else int(needed[c.id])
                     if c in shown else 0 for c in canvases]
        else:
            weight = sum(ratios.values())
            sizes = [int(total * ratios[c.id] / weight) if c in shown else 0 for c in canvases]
        self.splitter.setSizes(sizes)
        for c in canvases:
            preview = self.previews.get(c.id)
            if preview is not None:
                lean = "right" if c is visible[0] else "left" if c is visible[-1] else "center"
                preview.align = lean if spare > 0 else "center"
                preview.update()

    def _on_clip_clicked(self) -> None:
        """Save clip, or first start keeping the last seconds in memory."""
        if self.replay is None:
            self._toggle_replay(True)
        else:
            self._save_replay()

    def _grab_thumbs(self) -> None:
        """Every few seconds, keep a small picture of each bloom: the ones on air in Live, the ones
        you're looking at in Build. Live tiles and Build's library swatches both show them."""
        now = time.monotonic()
        if now - self._thumbs_at < 3.0:
            return
        self._thumbs_at = now
        for canvas_id, preview in self.previews.items():
            image = preview._image
            if image is None or self.in_place is not None:
                continue
            if self.mode == "live":
                scene_id = self.collection.active_scenes.get(canvas_id)
            elif not preview.isVisible():
                continue  # the other shape's preview isn't drawn in Build: its picture is stale
            else:
                scene = preview.scene
                scene_id = scene.id if scene is not None and scene.kind == "show" else None
                if self._thumb_seen.get(canvas_id) != scene_id:  # just switched: the frame may be the old bloom's
                    self._thumb_seen[canvas_id] = scene_id
                    continue
            if scene_id is None:
                continue
            thumb = image.scaled(320, 180, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            self.live_panel.set_thumb(scene_id, thumb)
            self.scene_tree.set_thumb(scene_id, thumb)

    def _thumbs_dir(self) -> Path:
        """Pictures of the blooms: thumbs/ for the first garden, gardens/thumbs/<garden> for the others."""
        if self.scenes_path.name == "scenes.json":
            return self.scenes_path.parent / "thumbs"
        return self.scenes_path.parent / "thumbs" / self.scenes_path.stem

    def _load_thumbs(self) -> None:
        """Pictures saved last time, so swatches and tiles aren't blank until each bloom is seen again."""
        ids = {s.id for s in self.collection.scenes if s.kind == "show"}
        folder = self._thumbs_dir()
        if not folder.is_dir():
            return
        for path in folder.glob("*.png"):
            if path.stem in ids:
                image = QImage(str(path))
                if not image.isNull():
                    self.live_panel.thumbs[path.stem] = image

    def _save_thumbs(self) -> None:
        """Once, on close: a small PNG per bloom (old blooms' pictures are cleared out)."""
        ids = {s.id for s in self.collection.scenes if s.kind == "show"}
        folder = self._thumbs_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            for path in folder.glob("*.png"):
                if path.stem not in ids:
                    path.unlink(missing_ok=True)
            for scene_id, image in self.live_panel.thumbs.items():
                if scene_id in ids and image is not None and not image.isNull():
                    image.save(str(folder / f"{scene_id}.png"))
        except OSError as exc:
            log.info("couldn't save bloom pictures: %s", exc)

    def _live_destinations(self, levels: dict[str, str] | None = None, notes: dict[str, str] | None = None) -> None:
        from sprigcast.app.destinations import problem
        from sprigcast.output.keys import probably_has_key

        places = []
        streams = [o for o in self.collection.outputs if o.type == "stream"]
        for output in streams:
            service = SERVICES.get(output.service or "custom", SERVICES["custom"])
            issue = problem(output, self.collection, probably_has_key) if output.enabled else None
            note = (notes or {}).get(output.id) or issue or ("Ready" if output.enabled else "Off")
            name = service.label
            if sum(1 for o in streams if o.service == output.service) > 1:  # two Twitch rows: say which shape
                name += f" · {output.canvas}"
            places.append(Destination(output.id, name, output.enabled, note, output.canvas,
                                      output.bandwidth_test if service.bandwidth_test else None))
        self.live_side.show_destinations(places, levels or {})
        if self.streaming is None and hasattr(self, "mode_clock"):
            ready = sum(1 for p in places if p.on and p.note == "Ready")
            self._set_header_clock(f"Offline  ·  {ready} ready to go live" if ready else "Offline", False)

    def _build_view(self) -> dict[str, str]:
        """Build mode: canvas id -> the bloom being edited there when it isn't the one on air.

        Editing a bloom doesn't put it on air: the engine draws it for the preview only, so you can build "Be right back" while "Just Chatting" stays live."""
        scene = self._scene()
        if getattr(self, "mode", "build") != "build" or scene is None or scene.kind != "show":
            return {}
        view = {}
        for shown in self.collection.linked_with(scene.id):
            if shown.id != self.collection.active_scenes.get(shown.canvas):
                view[shown.canvas] = shown.id
        return view

    def _shown_scene(self, canvas_id: str) -> Scene | None:
        """The bloom a canvas preview shows: the one being edited in Build, else the one on air."""
        scene_id = self._build_view().get(canvas_id)
        return self.collection.scene(scene_id) if scene_id else self.collection.active_scene(canvas_id)

    def _sync_engine_staged(self) -> None:
        staged = self._build_view()
        if staged != self.engine.staged:
            self.engine.staged = staged  # a fresh dict: the render thread reads it without a lock
            for canvas_id in staged:
                self.previews[canvas_id]._image = None  # don't show the on-air picture as if it were this bloom

    def _switch_to_scene(self, scene_id: str) -> None:
        self.selected_scene_id = scene_id
        self._make_active()
        self._refresh_sources()

    def _edit_in_build(self, scene_id: str) -> None:
        """From Live: open this scene in Build mode, on its own canvas tab."""
        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return
        self._set_mode("build")
        ids = [c.id for c in self.collection.canvases]
        if scene.kind == "show" and scene.canvas in ids:
            self.canvas_tabs.setCurrentIndex(ids.index(scene.canvas))
        self.selected_scene_id = scene.id
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

    # Modes -------------------------------------------------------------------------

    MODES = {"live": "Live", "build": "Build"}  # Live first: it's where the show runs

    def _build_mode_bar(self) -> None:
        """The header: logo, the Live | Build pill, search, and the on-air clock (or Offline)."""
        bar = QToolBar("Mode")
        bar.setObjectName("modeBar")
        bar.setMovable(False)
        bar.setStyleSheet(f"QToolBar#modeBar {{ background: {theme.CHROME}; border: none; padding: 10px 18px; spacing: 18px; }}")
        brand = QWidget()
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(0, 0, 0, 0)
        brand_row.setSpacing(10)
        logo = QLabel()
        from sprigcast.app.main import brand_mark

        icon = brand_mark()
        if icon.isNull():
            icon = QApplication.windowIcon()
        if not icon.isNull():
            logo.setPixmap(icon.pixmap(30, 30))
        brand_row.addWidget(logo)
        name = QLabel("SprigCast")
        font = name.font()
        font.setPixelSize(19)
        font.setWeight(font.Weight.DemiBold)
        name.setFont(font)
        name.setStyleSheet(f"color: {theme.TEXT_BRIGHT};")
        brand_row.addWidget(name)
        bar.addWidget(brand)
        pills = QFrame()
        pills.setObjectName("modePills")
        pills.setStyleSheet(
            f"#modePills {{ background: {theme.PANEL}; border-radius: 18px; }}"
            f"#modePills QToolButton {{ border: none; border-radius: 14px; padding: 6px 20px; color: {theme.MUTED};"
            f" background: transparent; font-weight: 500; }}"
            f"#modePills QToolButton:hover {{ color: {theme.TEXT}; }}"
            f"#modePills QToolButton:checked {{ background: {theme.RAISED_HOVER}; color: {theme.TEXT_BRIGHT};"
            f" font-weight: 600; }}"
        )
        pill_row = QHBoxLayout(pills)
        pill_row.setContentsMargins(4, 4, 4, 4)
        pill_row.setSpacing(0)
        group = QActionGroup(self)
        group.setExclusive(True)
        self.mode_actions: dict[str, QAction] = {}
        for mode, label in self.MODES.items():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setToolTip({"build": "Make and edit blooms (Ctrl+2)",
                               "live": "Run the show (Ctrl+1)"}[mode])
            action.setShortcut(QKeySequence("Ctrl+1" if mode == "live" else "Ctrl+2"))
            action.triggered.connect(lambda _=False, m=mode: self._set_mode(m))
            group.addAction(action)
            self.addAction(action)  # keeps the shortcut working
            button = QToolButton()
            button.setDefaultAction(action)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            pill_row.addWidget(button)
            self.mode_actions[mode] = action
        self.mode_actions["build"].setChecked(True)
        bar.addWidget(pills)
        bar.addWidget(self.gardens.make_chip())  # the garden and path in use; click to switch
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        self.screenshot_button = QToolButton()
        self.screenshot_button.setIcon(icons.icon("shutter", theme.MUTED, 18))
        self.screenshot_button.setIconSize(QSize(18, 18))
        self.screenshot_button.setFixedSize(38, 38)
        self.screenshot_button.setToolTip("Screenshot every canvas, to your Screenshots folder")
        self.screenshot_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screenshot_button.clicked.connect(lambda: self._screenshot())
        bar.addWidget(self.screenshot_button)
        self.palette_button = QToolButton()
        self.palette_button.setIcon(icons.icon("search", theme.MUTED, 18))
        self.palette_button.setIconSize(QSize(18, 18))
        self.palette_button.setFixedSize(38, 38)
        self.palette_button.setToolTip("Search commands, blooms and settings (Ctrl+K)")
        self.palette_button.setStyleSheet(
            f"QToolButton {{ background: {theme.PANEL}; border: none; border-radius: 19px; color: {theme.MUTED}; }}"
            f"QToolButton:hover {{ background: {theme.RAISED}; color: {theme.TEXT}; }}")
        self.palette_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.palette_button.clicked.connect(lambda: self._open_palette())
        self.screenshot_button.setStyleSheet(self.palette_button.styleSheet())
        bar.addWidget(self.palette_button)
        self.settings_button = QToolButton()
        self.settings_button.setIcon(icons.icon("gear", theme.MUTED, 18))
        self.settings_button.setIconSize(QSize(18, 18))
        self.settings_button.setFixedSize(38, 38)
        self.settings_button.setToolTip("Settings (Ctrl+,)")
        self.settings_button.setStyleSheet(self.palette_button.styleSheet())
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(lambda: self._open_settings())
        bar.addWidget(self.settings_button)
        self.mode_clock = QLabel("")
        bar.addWidget(self.mode_clock)
        self._set_header_clock("Offline", False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

    def _set_header_clock(self, text: str, live: bool) -> None:
        self.mode_clock.setText(text)
        font = self.mode_clock.font()
        font.setPixelSize(16 if live else 13)
        font.setWeight(font.Weight.DemiBold if live else font.Weight.Normal)
        self.mode_clock.setFont(font)
        self.mode_clock.setStyleSheet(f"color: {theme.LIVE_TEXT if live else theme.MUTED}; padding-left: 6px;")

    def _set_mode(self, mode: str) -> None:
        if mode == self.mode:
            self.mode_actions[mode].setChecked(True)
            return
        self.mode = mode
        self.mode_actions[mode].setChecked(True)
        self.panels.set_mode(mode)
        editable = mode == "build"
        self._exit_in_place()
        self._place_canvases()
        self._show_part_editor()
        for preview in self.previews.values():
            preview.editable = editable
            preview.update()
        self._apply_canvas_view()
        QSettings().setValue("window/mode", mode)

    # Build mode's canvas tabs ---------------------------------------------------------

    def _apply_canvas_view(self) -> None:
        """Build shows only the canvas of the current tab; Live shows every canvas side by side."""
        build = self.mode == "build"
        self.canvas_tabs_row.setVisible(False)
        # The shape tabs only show when both shapes are on (nothing to switch between otherwise).
        self.canvas_tabs.setVisible(sum(c.enabled for c in self.collection.canvases) > 1)
        for canvas in self.collection.canvases:
            column = self.canvas_columns[canvas.id]
            # Build shows just the shape on the tab you're on; the other one isn't drawn (or rendered) at all.
            column.setVisible(canvas.enabled and (not build or canvas.id == self.build_canvas))
            preview = self.previews[canvas.id]
            twin = build and canvas.id != self.build_canvas
            preview.twin = twin
            preview.editable = build and not twin
            preview.title = f"{canvas.id.capitalize()} {canvas.width}×{canvas.height}"
            preview.setToolTip(f"Click to edit the {canvas.id} shape" if twin else "")
            preview.keep_aspect = self.keep_shape.isChecked()
            preview.setCursor(Qt.CursorShape.PointingHandCursor if twin else Qt.CursorShape.ArrowCursor)
            preview.update()
        # Build: the shape being edited comes first and the twin sits to its right; Live: canvas order.
        first = self.build_canvas if build else self.collection.canvases[0].id
        if self.splitter.indexOf(self.canvas_columns[first]) != 0:
            self.splitter.insertWidget(0, self.canvas_columns[first])
        if build:
            self._share_stage()  # now, so the swap doesn't show a frame of the old sizes
            QTimer.singleShot(0, self._share_stage)
        primary = self.collection.canvases[0].id
        self.link_button.setVisible(self.build_canvas != primary)
        canvas = self.collection.canvas(self.build_canvas)
        self.safe_zone_button.setVisible(canvas.height > canvas.width)  # only vertical canvases get zones

    # One canvas or both ----------------------------------------------------------------

    def canvas_choice(self) -> str:
        """"both", or the id of the only canvas switched on."""
        on = [c.id for c in self.collection.canvases if c.enabled]
        return on[0] if len(on) == 1 else "both"

    def show_canvases(self, choice: str) -> bool:
        """Keep "both" canvases, or just one (by id). A canvas switched off isn't rendered, previewed,
        recorded or streamed, and leaves Build's tabs and the Live dock until it's switched back on."""
        for canvas in self.collection.canvases:
            wanted = choice == "both" or canvas.id == choice
            if canvas.enabled and not wanted and (self.streaming is not None or canvas.id in self.encoders.encoders):
                QMessageBox.information(self, "SprigCast", f"The {canvas.id} canvas is recording or live. "
                                        "Stop that first, then switch it off.")
                self._sync_canvas_choice()
                return False
        changed = False
        for canvas in self.collection.canvases:
            wanted = choice == "both" or canvas.id == choice
            changed |= canvas.enabled != wanted
            canvas.enabled = wanted
        self._sync_canvas_choice()
        if changed:
            self._changed()
            self._refresh_scenes()
            self._refresh_stream_boxes()
        return True

    def _sync_canvas_choice(self) -> None:
        """Bring tabs, previews, the record boxes and the menu in line with which canvases are on."""
        for i, canvas in enumerate(self.collection.canvases):
            self.canvas_tabs.setTabVisible(i, canvas.enabled)
        if not self.collection.canvas(self.build_canvas).enabled:
            first = next((i for i, c in enumerate(self.collection.canvases) if c.enabled), None)
            if first is not None:
                self.canvas_tabs.setCurrentIndex(first)  # moves Build to a canvas that's on
        choice = self.canvas_choice()
        for key, action in getattr(self, "canvas_choice_actions", {}).items():
            action.setChecked(key == choice)
        self._apply_canvas_view()

    def _set_safe_zones(self, choice: str) -> None:
        for preview in self.previews.values():
            preview.safe_zones = choice
            preview.update()
        self.safe_zone_button.setText(f"Safe zones: {choice}")
        if choice in self.zone_actions:
            self.zone_actions[choice].setChecked(True)
        QSettings().setValue("preview/safe_zones", choice)

    def _set_live_safe_zones(self, choice: str) -> None:
        """Live's own safe zones choice (Build's is the button over its canvas); off by default."""
        choice = choice if choice in safe_zones.CHOICES else "Off"
        for preview in self.previews.values():
            preview.live_safe_zones = choice
            preview.update()
        QSettings().setValue("live/safe_zones", choice)

    def _on_canvas_tab(self, index: int) -> None:
        canvas_id = self.collection.canvases[index].id
        if canvas_id == self.build_canvas:
            return
        self._exit_in_place()
        self.build_canvas = canvas_id
        self.selected_folder_id = None
        QSettings().setValue("build/canvas", canvas_id)
        scene = self._scene()
        if scene is None or (scene.kind == "show" and scene.canvas != canvas_id):
            # Keep editing the same bloom in its other shape when it has one (its linked partner).
            partner = next((s for s in self.collection.linked_with(scene.id) if s.canvas == canvas_id),
                           None) if scene is not None and scene.kind == "show" else None
            active = partner or self.collection.active_scene(canvas_id)
            shows = self.collection.scenes_for(canvas_id)
            self.selected_scene_id = active.id if active else (shows[0].id if shows else None)
        self._apply_canvas_view()
        self._refresh_scenes()
        self._refresh_sources()
        self._refresh_previews()
        self._show_part_editor()

    def _link_target(self, scene: Scene) -> Scene | None:
        return next((s for s in self.collection.scenes if s.id == scene.linked_to), None) if scene.linked_to else None

    def _link_label(self, scene: Scene) -> str:
        """'⇄ Other name' for the scenes that switch together with this one (⇄ like the Link button)."""
        target = self._link_target(scene)
        names = [target.name] if target else [s.name for s in self.collection.scenes if s.linked_to == scene.id]
        return f"   ⇄ {', '.join(names)}" if names else ""

    def _fill_link_menu(self, menu: QMenu) -> None:
        """Pick which bloom on the main canvas this one switches together with."""
        menu.clear()
        scene = self._scene()
        if scene is None or scene.kind != "show" or scene.canvas == self.collection.canvases[0].id:
            menu.addAction(f"Select a {self.build_canvas} {SHOW} first").setEnabled(False)
            return
        others = [s for s in self.collection.scenes if s.kind == "show" and s.canvas != scene.canvas]
        none = menu.addAction("Not linked")
        none.setCheckable(True)
        none.setChecked(scene.linked_to is None)
        none.triggered.connect(lambda: self._link_scene(scene.id, None))
        if others:
            menu.addSeparator()
        for other in others:
            action = menu.addAction(f"{literal(other.name)}   ({other.canvas})")
            action.setCheckable(True)
            action.setChecked(scene.linked_to == other.id)
            action.triggered.connect(lambda _=False, oid=other.id: self._link_scene(scene.id, oid))

    def _link_scene(self, scene_id: str, target_id: str | None) -> None:
        """Switching to the target (on any canvas) also switches to this scene, and the other way round."""
        scene = self.collection.scene(scene_id)
        scene.linked_to = target_id
        if target_id is not None:
            target = self.collection.scene(target_id)
            if self.collection.active_scenes.get(target.canvas) == target_id:
                self.collection.active_scenes[scene.canvas] = scene.id
        self._changed()
        self._refresh_scenes()
        self._refresh_previews()
        linked = f"linked to “{self.collection.scene(target_id).name}”" if target_id else "not linked"
        self.statusBar().showMessage(f"“{scene.name}” is {linked}.", 4000)

    def _dock(self, title: str, body: QWidget, toolbar: QWidget | None = None) -> QWidget:
        """Wrap a view with its toolbar. It stays off screen unless it's offered as a panel."""
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(body, 1)
        if toolbar is not None:
            layout.addWidget(toolbar)
        if isinstance(body, QScrollArea):
            # Contents shrink to the panel's width (see app/fit.py); only scroll up and down.
            body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.panels.shelve(wrapper)
        return wrapper

    def update_title(self) -> None:
        """SprigCast — the garden and path in use, so you always know which setup is on screen."""
        shelf = self.gardens.shelf
        self.setWindowTitle(f"SprigCast  ·  {shelf.garden().name}  ·  {shelf.path().name}")

    def _build_menu(self) -> None:
        """File · Edit · View · Panels · Gardens · Paths · Tools · Help."""
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        open_folder = QAction("Show scenes file location", self)
        open_folder.triggered.connect(
            lambda: QMessageBox.information(self, "SprigCast", f"This garden is saved to:\n{self.scenes_path}")
        )
        file_menu.addAction(open_folder)
        recordings = QAction("Open recordings folder", self)
        recordings.triggered.connect(self._open_recordings_folder)
        file_menu.addAction(recordings)
        plugins = QAction("Open plugins folder", self)
        plugins.triggered.connect(self._open_plugins_folder)
        plugins.setEnabled(self.plugins_dir is not None)
        file_menu.addAction(plugins)
        file_menu.addAction("Open logs folder").triggered.connect(lambda: self._open_logs())
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Edit: the selected thing in Build (the same actions and shortcuts as its right-click menu).
        edit_menu = bar.addMenu("&Edit")
        undo = edit_menu.addAction("Undo")
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        undo.triggered.connect(self.undo)
        redo = edit_menu.addAction("Redo")
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        redo.triggered.connect(self.redo)
        edit_menu.addSeparator()
        a = self.item_actions
        for key in ("rename", "settings", "filters"):
            edit_menu.addAction(a[key])
        edit_menu.addSeparator()
        size = edit_menu.addMenu("Size and place")
        for key in ("fit", "fill", "stretch", "center", "reset", "uncrop"):
            size.addAction(a[key])
        order = edit_menu.addMenu("Order")
        for key in ("front", "up", "down", "back"):
            order.addAction(a[key])
        edit_menu.addSeparator()
        for key in ("hide", "lock", "remove"):
            edit_menu.addAction(a[key])

        # View: which screen, which canvases, projectors.
        view_menu = bar.addMenu("&View")
        live = view_menu.addAction("Live")
        live.setShortcut(QKeySequence("Ctrl+1"))
        live.triggered.connect(lambda: self._set_mode("live"))
        build = view_menu.addAction("Build")
        build.setShortcut(QKeySequence("Ctrl+2"))
        build.triggered.connect(lambda: self._set_mode("build"))
        view_menu.addSeparator()
        shown = view_menu.addMenu("Show canvases")
        shown_group = QActionGroup(self)
        self.canvas_choice_actions: dict[str, QAction] = {}
        labels = [("both", "Horizontal and vertical")]
        labels += [(c.id, f"{c.id.capitalize()} only") for c in self.collection.canvases]
        for key, label in labels:
            action = shown.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self.canvas_choice())
            shown_group.addAction(action)
            action.triggered.connect(lambda _=False, k=key: self.show_canvases(k))
            self.canvas_choice_actions[key] = action
        projectors = view_menu.addMenu("Pop out a canvas")
        projectors.aboutToShow.connect(lambda: self._fill_projectors_menu(projectors))
        self._fill_projectors_menu(projectors)
        view_menu.addSeparator()
        palette_action = QAction("Command palette…", self)
        palette_action.setShortcuts([QKeySequence("Ctrl+K"), QKeySequence("Ctrl+Shift+P")])
        palette_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        palette_action.triggered.connect(lambda: self._open_palette())
        view_menu.addAction(palette_action)

        self.docks_menu = bar.addMenu("&Panels")
        self.stats_action = QAction("Stats", self)
        self.stats_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.stats_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.stats_action.triggered.connect(lambda: self.stats_strip.toggle())
        self.addAction(self.stats_action)
        self.reset_layout_action = QAction("Reset panels", self)
        self.reset_layout_action.triggered.connect(lambda: self.panels.reset())
        self.custom_docks_action = QAction("Add or edit web panels…", self)
        self.custom_docks_action.setToolTip("A web page as a panel: activity feed, Streamer.bot, TikFinity…")
        self.custom_docks_action.triggered.connect(lambda: self._edit_custom_docks())
        self.docks_menu.aboutToShow.connect(self._fill_docks_menu)
        self._fill_docks_menu()

        self.gardens.build_menus(bar)

        settings_menu = bar.addMenu("&Tools")
        open_settings = settings_menu.addAction("Settings…")
        open_settings.setShortcut(QKeySequence("Ctrl+,"))
        open_settings.triggered.connect(lambda: self._open_settings())
        settings_menu.addAction("Starter kit…").triggered.connect(lambda: self.open_starter())
        settings_menu.addAction("Plugin store…").triggered.connect(lambda: self._open_settings("Plugins"))
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
        settings_menu.addSeparator()
        settings_menu.addAction("Stream health…").triggered.connect(lambda: self._open_health())
        doctor_action = settings_menu.addAction("Stream doctor…")
        doctor_action.setShortcut(QKeySequence("Ctrl+Shift+D"))
        doctor_action.triggered.connect(lambda: self._open_doctor())
        settings_menu.addAction("Ducking (mic first)…").triggered.connect(lambda: self._open_ducking())
        settings_menu.addAction("Optimize for this PC…").triggered.connect(lambda: self.open_optimizer())
        settings_menu.addAction("Light settings for a slower PC…").triggered.connect(
            lambda: self.use_light_settings())

        help_menu = bar.addMenu("&Help")
        help_menu.addAction("Keyboard shortcuts").triggered.connect(lambda: self._show_shortcuts())
        help_menu.addAction("Open logs folder (for bug reports)").triggered.connect(lambda: self._open_logs())
        help_menu.addSeparator()
        help_menu.addAction("Check for updates…").triggered.connect(lambda: self.updater.check(manual=True))
        help_menu.addAction("About SprigCast").triggered.connect(lambda: self._show_about())

    def _fill_projectors_menu(self, menu: QMenu) -> None:
        menu.clear()
        for canvas in self.collection.canvases:
            self._fill_projector_menu(menu.addMenu(canvas.id.capitalize()), canvas.id)

    def shortcut_lines(self) -> list[tuple[str, str]]:
        """(keys, what) for Help → Keyboard shortcuts: every menu action with a shortcut, plus canvas moves."""
        lines: list[tuple[str, str]] = []

        def walk(menu: QMenu, prefix: str) -> None:
            for action in menu.actions():
                if action.menu() is not None:
                    walk(action.menu(), prefix)
                elif action.shortcut().toString():
                    lines.append((action.shortcut().toString(QKeySequence.SequenceFormat.NativeText),
                                  f"{prefix}{action.text().replace('&', '')}"))

        for top in self.menuBar().actions():
            if top.menu() is not None:
                walk(top.menu(), "")
        lines += [("Ctrl + wheel", "Zoom the canvas"), ("Space + drag, middle drag", "Pan the canvas"),
                  ("Ctrl+0", "Fit the canvas"), ("Alt + drag a side", "Crop"),
                  ("Shift + drag a corner", "Free shape (or keep it, if Keep shape is off)")]
        seen, unique = set(), []
        for keys, text in lines:
            if (keys, text) not in seen:
                seen.add((keys, text))
                unique.append((keys, text))
        return unique

    def _show_shortcuts(self) -> None:
        rows = "".join(f"<tr><td style='padding:3px 18px 3px 0'><b>{k}</b></td><td>{t}</td></tr>"
                       for k, t in self.shortcut_lines())
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard shortcuts")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<table>{rows}</table>")
        box.exec()

    def _show_about(self) -> None:
        from sprigcast.app.about import show_about

        show_about(self)

    def _fill_docks_menu(self) -> None:
        """Each panel this screen offers (ticked when shown), Pop out, then adding web panels and Stats."""
        self.panels.fill_menu(self.docks_menu, [self.custom_docks_action, self.stats_action,
                                                self.reset_layout_action])

    def _edit_custom_docks(self) -> None:
        if not custom_docks.web_engine_available():
            QMessageBox.warning(self, "SprigCast", "Web panels need Qt WebEngine, which isn't installed.")
            return
        dialog = custom_docks.CustomDocksDialog(custom_docks.load(), self)
        if dialog.exec():
            custom_docks.apply(self, dialog.result_docks())

    # Engine link ------------------------------------------------------------

    def _refresh_meter(self) -> None:
        self.last_resources = self.meter.sample()
        self.meter_label.setText(describe(self.last_resources))
        self.live_side.stats.setText(describe(self.last_resources))
        warning = ram_warning(self.last_resources)
        self.meter_label.setStyleSheet(f"color: {theme.WARN}" if warning else "")
        self.meter_label.setToolTip(warning or self._meter_tip)
        if warning and not self._warned_ram:
            log.warning(warning)
            self.statusBar().showMessage(warning, 15000)
        self._warned_ram = bool(warning)

    def _watched_previews(self) -> frozenset[str]:
        """Previews on screen right now; the engine skips copying back the rest (or everything when minimised)."""
        projected = {p.canvas_id for p in self.projectors if p.view is None}
        if self.isMinimized() or not self.isVisible():
            return frozenset(projected)  # a projector keeps showing while the main window is down
        keys = projected | {cid for cid, p in self.previews.items() if p.isVisible() and p.width() > 1}
        if self.mode == "build":
            keys |= {f"next:{cid}" for cid in self.engine.staged if self.previews[cid].isVisible()}
        if self.part_preview.isVisible() or self.in_place is not None:
            keys.add("part")
        return frozenset(keys)

    def _preview_pixel_sizes(self) -> dict[str, tuple[int, int]]:
        """How many physical pixels each preview's canvas area covers, so the engine copies no more."""
        sizes = {}
        edited = [(f"next:{c}", self.previews[c]) for c in self.engine.staged] if self.mode == "build" else []
        for key, preview in [*self.previews.items(), *edited]:
            if preview.isVisible():
                r, dpr = preview.canvas_rect(), preview.devicePixelRatioF()
                aw, ah = preview.aspect  # zoomed in, never ask for more pixels than the canvas has
                sizes[key] = (min(int(r.width() * dpr), int(aw)), min(int(r.height() * dpr), int(ah)))
        for projector in [p for p in self.projectors if p.view is None]:  # a screen's worth of pixels
            pw, ph = projector.pixel_size()
            old = sizes.get(projector.canvas_id, (0, 0))
            sizes[projector.canvas_id] = (max(old[0], pw), max(old[1], ph))
        return sizes

    def _poll_engine(self) -> None:
        for projector in self.projectors:
            if projector.view is not None:
                projector.take_pending()
        watched = self._watched_previews()
        if watched != self.engine.previews:
            self.engine.previews = watched
        sizes = self._preview_pixel_sizes()
        if sizes != self.engine.preview_sizes:
            self.engine.preview_sizes = sizes
        for canvas_id, mailbox in list(self.engine.frames.items()):
            frame = mailbox.take()
            if frame is None:
                continue
            view = self.engine.staged if self.mode == "build" else {}
            for projector in self.projectors:
                if projector.view is None and projector.canvas_id == canvas_id:
                    projector.set_frame(frame.data, frame.width, frame.height)
            if canvas_id in self.previews:
                if canvas_id not in view:  # else the preview shows the bloom being edited (next:)
                    self.previews[canvas_id].set_frame(frame.data, frame.width, frame.height, frame.canvas_size)
            elif canvas_id.startswith("next:") and canvas_id[5:] in view:
                self.previews[canvas_id[5:]].set_frame(frame.data, frame.width, frame.height, frame.canvas_size)
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
                if self.stats_strip.is_open():
                    self._refresh_stats_panel()
                self._show_render_health(event)
            elif isinstance(event, AudioLevels):
                if self.mode == "live":
                    self.live_side.set_levels(event.levels)
                else:
                    self.sound_strips.set_levels(event.levels)
                self.peak_watch.feed(event.levels)
                self.control.meters(event.levels)  # only when a connected app asked for meters
            elif isinstance(event, SourceInfo):
                self.source_sizes, self.source_statuses = event.sizes, event.statuses
                for preview in [*self.previews.values(), self.part_preview]:
                    preview.sizes, preview.statuses = self.source_sizes, self.source_statuses
                self._refresh_source_labels()
                selected = self._item()
                if selected is not None and self.spin["w"].value() == 0 and selected.source in self.source_sizes:
                    self._updating = True
                    self._show_spin_values(selected)  # its size just became known
                    self._updating = False
            elif isinstance(event, EngineError):
                self.statusBar().showMessage(event.message, 10000)
                if "compositor" in event.message:
                    QMessageBox.critical(self, "SprigCast", event.message)
        if self.recording is not None:
            self._refresh_recording_status()
        if self.streaming is not None:
            self._refresh_stream_status()
        self._grab_thumbs()

    def _changed(self, save: bool = True) -> None:
        """Hand a copy of the collection to the engine after every edit, and save it unless mid-drag."""
        if not save:  # mid-drag: at most one copy per frame, the newest (copying on every mouse move lagged)
            timer = getattr(self, "_drag_send", None)
            if timer is None:
                timer = self._drag_send = QTimer(self)
                timer.setSingleShot(True)
                timer.setInterval(16)
                timer.timeout.connect(lambda: self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True))))
            if not timer.isActive():
                timer.start()
            return
        timer = getattr(self, "_drag_send", None)
        if timer is not None:
            timer.stop()  # this full send carries the drag's end
        self.collection.save(self.scenes_path)
        self._remember_for_undo()
        self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True)))
        self._chat_sources_changed()
        control = getattr(self, "control", None)
        if control is not None:
            control.soon()  # apps connected to the control server hear what changed

    # Undo and redo ------------------------------------------------------------------------

    UNDO_STEPS = 50
    UNDO_MERGE_S = 0.8  # changes this close together (a slider, nudging with arrows) are one step

    def _remember_for_undo(self) -> None:
        now_json = self.collection.model_dump_json()
        if self._undo_current is None or now_json == self._undo_current:
            self._undo_current = now_json
            return
        at = time.monotonic()
        if not (self._undo and at - self._undo_at < self.UNDO_MERGE_S):
            self._undo.append(self._undo_current)
            del self._undo[:-self.UNDO_STEPS]
        self._undo_at = at
        self._undo_current = now_json
        self._redo.clear()

    def undo(self) -> None:
        if not self._undo:
            self.statusBar().showMessage("Nothing to undo.", 3000)
            return
        self._redo.append(self._undo_current)
        self._restore_state(self._undo.pop())
        self.statusBar().showMessage("Undone. Ctrl+Y puts it back.", 3000)

    def redo(self) -> None:
        if not self._redo:
            self.statusBar().showMessage("Nothing to redo.", 3000)
            return
        self._undo.append(self._undo_current)
        self._restore_state(self._redo.pop())

    def _restore_state(self, text: str) -> None:
        """Put the whole collection back as it was (in place: everything holding it sees the change)."""
        restored = SceneCollection.model_validate_json(text)
        for name in type(self.collection).model_fields:
            setattr(self.collection, name, getattr(restored, name))
        self._undo_current = text
        self._undo_at = 0.0
        self.collection.save(self.scenes_path)
        self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True)))
        ids = {s.id for s in self.collection.scenes}
        if self.selected_scene_id not in ids:
            self.selected_scene_id = self.collection.active_scenes.get(self.build_canvas)
        if self.selected_sound_id not in {c.id for c in self.collection.sources}:
            self._close_build_sound(refresh=False)
        self._exit_in_place()
        keep = self.source_list.currentRow()
        self._refresh_all()
        if 0 <= keep < self.source_list.count():
            self.source_list.setCurrentRow(keep)
        self._show_part_editor()
        self.live_panel.show_scenes(self.collection)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        for projector in list(self.projectors):
            projector.close()
        self.panels.close_windows()
        self.chat_panel.shutdown()
        self._save_layout()
        self._save_thumbs()
        self.gardens.close()
        self.hotkeys.stop()
        self.meter.stop()
        self.control.stop()
        if self.camera is not None:
            self._camera_timer.stop()
            self.camera.stop()
            self.camera = None
        if self.streaming is not None:
            self._stop_streaming()
        if self.recording is not None:
            self._stop_recording()
        if self.replay is not None:
            self.replay.stop()
            self.replay = None
        self.timer.stop()
        self.engine.stop()
        if self.browser_host is not None:
            self.browser_host.shutdown()
        self.updater.install_on_quit()  # a downloaded update runs once SprigCast has closed
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

    def _has_content(self, scene: Scene, seen: frozenset = frozenset()) -> bool:
        """Anything to see: a source, or a sprig with something in it (at any depth)."""
        sources = {c.id: c for c in self.collection.sources}
        for item in scene.items:
            config = sources.get(item.source)
            part = self.collection.part_of(config) if config is not None else None
            if part is None:
                return True
            if part.id not in seen and self._has_content(self.collection.shape_for(part, scene.canvas), seen | {part.id}):
                return True
        return False

    def _empty_hint(self, scene: Scene | None) -> tuple[str, str] | None:
        """The card in the middle of an empty bloom or sprig."""
        if scene is None or self._has_content(scene):
            return None
        if scene.kind == "part":
            return f"Empty {PART}", f"+ Add a camera, screen, text or another {PART}"
        sprig = self.collection.bloom_sprig(scene)
        if sprig is None:
            return f"Empty {SHOW}", f"+ Add {a(PART)} to fill it"
        return f"{sprig.name} is empty", "Double-click to open it and add things"

    def _refresh_previews(self) -> None:
        self._sync_engine_staged()
        in_place = self._in_place_state()
        part_names = {s.id: s.name for s in self.collection.sources if s.kind == PART_KIND}
        for canvas_id, preview in self.previews.items():
            preview.part_names = part_names
            scene = self._shown_scene(canvas_id)
            if in_place is not None and in_place[0] == canvas_id:
                show, placement, layout, size = in_place[1:]
                preview.subtitle = f"{show.name}  ›  {layout.name}   ·   Esc to finish"
                preview.show_title = True
                preview.set_inner(placement, size)
                preview.set_scene(layout, self._item_index() if layout.id == self.selected_scene_id else None)
                continue
            if preview.inner is not None:
                preview.set_inner(None, None)
            preview.hint = None
            preview.subtitle = scene.name if scene else "no scene"
            if scene is not None and scene.id != self.collection.active_scenes.get(canvas_id):
                preview.subtitle += "  ·  not on air"
            if preview.twin:
                preview.subtitle = ""  # small: just the shape and its size (the tooltip says click to edit)
            # Build's big canvas: the bloom's name and "on air" are right above the card already.
            preview.show_title = self.mode == "build" and preview.twin  # Live shows just the picture
            index = self._item_index() if scene is not None and scene.id == self.selected_scene_id else None
            preview.hint = self._empty_hint(scene) if self.mode == "build" else None
            preview.set_scene(scene, index)
            selected = self._item()
            preview.ghost_index = next((i for i, it in enumerate(scene.items) if it.source == selected.source),
                                       None) if preview.twin and scene is not None and selected is not None else None
        part = self._scene()
        if part is not None and part.kind == "part" and self.part_preview.canvas_id == f"part:{part.id}":
            self.part_preview.hint = self._empty_hint(part)
            self.part_preview.set_scene(part, self._item_index())
        self.live_panel.show_scenes(self.collection)
        if getattr(self, "mode", "build") == "live":
            QTimer.singleShot(0, self._fit_tiles)

    def _refresh_scenes(self) -> None:
        """Build's left rail: Blooms (this shape's, by folder), then Sprigs (by folder)."""
        self._updating = True
        self.scene_tree.clear()
        canvas_id = self.build_canvas
        searching = bool(self.library.search.text().strip())
        thumbs = self.live_panel.thumbs
        active = self.collection.active_scenes.get(canvas_id)
        folder_items: list[tuple[QTreeWidgetItem, object]] = []

        def folder_row(holder: QTreeWidgetItem, folder, count: int) -> QTreeWidgetItem:
            row = QTreeWidgetItem([f"{folder.name}   ({count})" + ("   · Live" if folder.live else "")])
            row.setData(0, ROLE_ROW, "folder")
            row.setData(0, ROLE_FOLDER, folder.id)
            row.setData(0, LIB_NAME, folder.name)
            row.setData(0, LIB_TAG, "on Live" if folder.live else str(count))
            row.setData(0, ROLE_COLOUR, folder.colour)
            row.setFlags(FOLDER_FLAGS)
            row.setToolTip(0, "Folder: click to open or fold it, drag things in, right-click to rename, "
                              "recolour or remove.")
            holder.addChild(row)
            folder_items.append((row, folder))
            if folder.id == self.selected_folder_id and self.selected_scene_id is None:
                row.setSelected(True)
            return row

        blooms = heading_item(cap(SHOWS), "", "")
        blooms.setToolTip(0, f"{cap(SHOWS)} are the scenes you put on air. + adds one, or a folder.")
        self.scene_tree.addTopLevelItem(blooms)
        for folder, scenes in self.collection.grouped_scenes(canvas_id):
            holder = blooms if folder is None else folder_row(blooms, folder, len(scenes))
            for scene in scenes:
                label = scene.name + self._link_label(scene)
                child = QTreeWidgetItem([("▶ " if scene.id == active else "") + label])
                child.setData(0, ROLE_ID, scene.id)
                child.setData(0, ROLE_ROW, "bloom")
                child.setData(0, LIB_NAME, scene.name)
                child.setData(0, LIB_TAG, self._link_label(scene).strip())
                child.setData(0, ROLE_COLOUR, folder.colour if folder is not None else None)
                child.setData(0, ROLE_ON_AIR, scene.id == active)
                child.setData(0, ROLE_THUMB, thumbs.get(scene.id))
                child.setFlags(BLOOM_FLAGS)
                child.setToolTip(0, f"{scene.name}: click to edit it" + ("  ·  on air" if scene.id == active else ""))
                holder.addChild(child)
                if scene.id == self.selected_scene_id:
                    child.setSelected(True)
        blooms.setExpanded(True)

        sprigs = heading_item(cap(PARTS), "", PART_ROOT)
        sprigs.setToolTip(0, f"{cap(PARTS)} are scenes you build once and put inside {SHOWS} (nested scenes). "
                             f"Change one and every {SHOW} using it changes too.")
        self.scene_tree.addTopLevelItem(sprigs)
        selected = self._scene()
        selected_part = self.collection.main_part(selected) if selected is not None and selected.kind == "part" else None
        for folder, parts in self.collection.grouped_parts():
            holder = sprigs if folder is None else folder_row(sprigs, folder, len(parts))
            for part in parts:
                uses = len(self.collection.part_uses(part.id))
                shapes = " + ".join(s.canvas for s in self.collection.shapes(part))
                child = QTreeWidgetItem([f"{part.name}   ({shapes} · used in {uses})"])
                child.setIcon(0, icons.icon("sprig", theme.ACCENT))
                child.setData(0, ROLE_ID, part.id)
                child.setData(0, ROLE_ROW, "sprig")
                child.setData(0, LIB_NAME, part.name)
                child.setData(0, LIB_TAG, f"in {uses}" if uses else "unused")
                child.setFlags(BLOOM_FLAGS)
                child.setToolTip(0, f"{cap(a(PART))} ({shapes}), in {uses} {SHOW if uses == 1 else SHOWS}. Click to "
                                    f"edit it: every {SHOW} using it changes too.")
                holder.addChild(child)
                if part is selected_part:
                    child.setSelected(True)
        sprigs.setExpanded(True)

        for holder, folder in folder_items:
            holder.setExpanded(searching or not folder.collapsed)
        self.library.apply_filter()
        self._updating = False
        self.live_panel.show_scenes(self.collection)
        self._refresh_build_bar()

    def _refresh_build_bar(self) -> None:
        """Above the canvas: the bloom you're editing and whether it's on air; Inside's heading names it too."""
        active = self.collection.active_scenes.get(self.build_canvas)
        selected = self._scene()
        selected_show = selected.id if selected is not None and selected.kind == "show" else None
        if selected is None:
            self.build_bar.show_place("", f"Pick {a(SHOW)} or {a(PART)} on the left")
            self.build_stage.inside_note.setText("")
        elif selected.kind == "part":
            main = self.collection.main_part(selected)
            uses = len(self.collection.part_uses(main.id))
            self.build_bar.show_place(main.name, f"{PART}  ·  in {uses} {SHOW if uses == 1 else SHOWS}", sprig=True)
            self.build_stage.inside_note.setText(main.name)
        else:
            self.build_bar.show_place(selected.name, "on air" if selected.id == active else "not on air")
            self.build_stage.inside_note.setText(selected.name)
        if hasattr(self, "on_air_button"):
            self.on_air_button.setVisible(selected_show is not None and selected_show != active)

    def _pick_bloom(self, scene_id: str) -> None:
        """A bloom pill: edit that bloom. It goes on air only with "Put on air" (or from Live)."""
        self.selected_scene_id = scene_id
        self.selected_folder_id = self.collection.scene(scene_id).folder
        self.last_bloom_id = scene_id
        if self.in_place is not None:
            self._exit_in_place()
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

    def _put_on_air(self) -> None:
        if self.streaming is not None and confirm_enabled(CONFIRM_STREAM):
            scene = self._scene()
            answer = QMessageBox.question(self, "SprigCast", f"You're live. Put “{scene.name}” on air now?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._make_active()
        self._refresh_sources()

    def _swap_shape(self, canvas_id: str) -> None:
        """The twin was clicked: edit that shape instead (the old one becomes the twin)."""
        ids = [c.id for c in self.collection.canvases]
        if canvas_id in ids and self.mode == "build":
            selected = self._item()
            self.setUpdatesEnabled(False)  # swap in one repaint, not a flicker of half-done layouts
            try:
                self.canvas_tabs.setCurrentIndex(ids.index(canvas_id))
            finally:
                self.setUpdatesEnabled(True)
            scene = self._scene()
            if selected is not None and scene is not None:  # keep the same thing selected if it's here too
                for row, item in enumerate(reversed(scene.items)):
                    if item.source == selected.source:
                        self._refresh_sources(keep_row=row)
                        break

    def _refresh_sources(self, keep_row: int | None = None) -> None:
        self._updating = True
        self.source_list.clear()
        scene = self._scene()
        if scene is not None:
            for item in reversed(scene.items):  # top-most first, like OBS
                row = QListWidgetItem(self._source_label(item.source))
                self._inside_roles(row, item.source, scene.canvas)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
                row.setCheckState(Qt.CheckState.Checked if item.visible else Qt.CheckState.Unchecked)
                row.setData(ROLE_LOCKED, bool(item.locked))
                self.source_list.addItem(row)
        if keep_row is not None and 0 <= keep_row < self.source_list.count():
            self.source_list.setCurrentRow(keep_row)
        empty = self.source_list.count() == 0
        self.source_list.setVisible(not empty)
        self.inside_empty.setVisible(empty)
        self._updating = False
        self._refresh_properties()

    def _inside_roles(self, row: QListWidgetItem, source_id: str, canvas_id: str) -> None:
        """What the Inside list draws: the plain name, a quiet tag, and a sprig's own contents."""
        config = self.collection.source(source_id)
        tags = []
        hidden = len(self.collection.hidden_here(config)) if config.kind == PART_KIND else 0
        if hidden:
            tags.append(f"{hidden} hidden here")
        if any(f.enabled for f in config.filters):
            tags.append("filters")
        status = self.source_statuses.get(source_id, "ok")
        if short_status(status):
            tags.append(short_status(status))
        row.setData(ROLE_NAME, config.name)
        row.setData(ROLE_TAG, " · ".join(tags))  # only what needs saying: problems, hidden bits, filters
        row.setData(ROLE_SPRIG, config.kind == PART_KIND)
        row.setData(ROLE_KIND, config.kind)
        row.setData(ROLE_SWATCH, config.settings.get("color") if config.kind == "color" else None)
        row.setData(ROLE_CHILDREN, self._sprig_children(config, canvas_id))
        detail = "" if status in ("ok", "") else f"\n{status}"
        row.setToolTip(f"{config.name}  ({PART if config.kind == PART_KIND else config.kind}){detail}")

    def _sprig_children(self, config, canvas_id: str, depth: int = 0, seen: frozenset = frozenset()) -> list:
        """(depth, name, is sprig) for what's inside a sprig, nested sprigs opened too (a few levels)."""
        part = self.collection.part_of(config) if config.kind == PART_KIND else None
        if part is None or part.id in seen or depth > 2:
            return []
        layout = self.collection.shape_for(part, canvas_id) if canvas_id else part
        rows = []
        for inner in reversed(layout.items):
            child = self.collection.source(inner.source)
            rows.append((depth, child.name, child.kind == PART_KIND))
            rows += self._sprig_children(child, canvas_id, depth + 1, seen | {part.id})
        return rows[:12]

    def _on_inside_moved(self, source_row: int, target_row: int) -> None:
        """A row was dragged in Inside: reorder the bloom (rows are top-most first, items bottom-most first)."""
        scene = self._scene()
        if scene is None or not (0 <= source_row < len(scene.items)) or not (0 <= target_row < len(scene.items)):
            return
        rows = list(reversed(scene.items))
        rows.insert(target_row, rows.pop(source_row))
        scene.items = list(reversed(rows))
        self._changed()
        self._refresh_sources(keep_row=target_row)

    def _source_label(self, source_id: str) -> str:
        config = self.collection.source(source_id)
        label = f"{config.name}   [{PART}]" if config.kind == PART_KIND else f"{config.name}   [{config.kind}]"
        if any(f.enabled for f in config.filters):
            label += "   ✦ filters"
        hidden = len(self.collection.hidden_here(config)) if config.kind == PART_KIND else 0
        if hidden:
            label += f"   · {hidden} hidden here"
        tag = short_status(self.source_statuses.get(source_id, "ok"))
        return f"{label}   ({tag})" if tag else label

    def _refresh_source_labels(self) -> None:
        scene = self._scene()
        if scene is None or self.source_list.count() != len(scene.items):
            return
        self._updating = True
        for row, item in enumerate(reversed(scene.items)):
            text = self._source_label(item.source)
            if self.source_list.item(row).text() != text:
                self.source_list.item(row).setText(text)
                self._inside_roles(self.source_list.item(row), item.source, scene.canvas)
        self._updating = False
        item = self._item()
        if item is not None:
            self.prop_status.setText(self._status_text(item.source))

    def _status_text(self, source_id: str) -> str:
        """Short: '1080p120 → 720p30'. The full status is the label's tooltip."""
        status = self.source_statuses.get(source_id, "")
        self.prop_status.setToolTip(status)
        return status_summary(status, self.source_sizes.get(source_id))

    def _refresh_properties(self) -> None:
        item = self._item()
        enabled = item is not None
        self._updating = True
        for box in self.spin.values():
            box.setEnabled(enabled)
        self.prop_name.setEnabled(enabled)
        self.prop_extra.setEnabled(enabled)
        self.prop_fit.setEnabled(enabled)
        self.prop_filters.setEnabled(enabled)
        self.prop_extra.setVisible(enabled)
        for holder in self.prop_sections:
            holder.setVisible(enabled)
        self.prop_empty.setVisible(not enabled and not self.learned)  # after a few sessions: just a quiet column
        self.prop_name.setVisible(enabled)
        self.prop_locked.setEnabled(enabled)
        for canvas_id, state in self._shape_states(item).items():
            label = self.prop_shapes[canvas_id]
            label.setText(state)
            label.setStyleSheet(f"color: {theme.ACCENT if state == 'Shown' else theme.MUTED};")
        if item is not None:
            self.prop_locked.setChecked(item.locked)
            config = self.collection.source(item.source)
            self.prop_name.setText(config.name)
            self._show_spin_values(item)
            self.prop_uncrop.setVisible(any(item.crop))
            self.prop_extra.setText(
                {
                    "color": "Colour",
                    "image": "Image",
                    "display_capture": "Monitor",
                    "window_capture": "Window",
                    "game_capture": "Game",
                    "camera": "Camera",
                    "text": "Text",
                    "browser": "Page",
                    "chat": "Chat box",
                }.get(config.kind, "Settings")
            )
            self.prop_status.setText(self._status_text(item.source))
            problem = plain_problem(self.source_statuses.get(item.source, ""), config.kind)
            self.prop_problem.setVisible(problem is not None)
            if problem is not None:
                self.prop_problem_text.setText(problem[0])
                self._problem_fix = problem[1]
                self.prop_fix.setVisible(bool(problem[1]))
                self.prop_fix.setText({"pick": "Pick it again", "retry": "Try again"}.get(problem[1], "Fix"))
            self.prop_vcam.setVisible(self._vcam_tip_due(config))
            part = self.collection.part_of(config) if config.kind == PART_KIND else None
            self.prop_sprig.setVisible(part is not None)
            if part is not None:
                here = self._scene()
                others = [s for s in self.collection.part_uses(part.id) if here is None or s.id != here.id]
                self.prop_used_in.setText(
                    f"Also in {self._bloom_links(others)}" if others else f"Only in this {SHOW} so far.")
        else:
            self.prop_sprig.hide()
            self.prop_problem.hide()
            self.prop_vcam.hide()
            self.prop_name.clear()
            self.prop_extra.setText("")
            self.prop_status.setText("")
            self.prop_locked.setChecked(False)
        self._updating = False
        self._refresh_previews()

    # Audio -------------------------------------------------------------------------

    AUDIO_KINDS = ("audio_input", "audio_output", "app_audio")

    def _has_sound(self, config) -> bool:
        """Sounds the Sound card lists: mics, desktop audio, and browser pages whose sound comes here."""
        if config.kind in self.AUDIO_KINDS:
            return not self.collection.device_off(config)  # global devices switched off: not listed
        if config.kind == "media":
            return True
        if config.kind == "browser":
            from sprigcast.sources.browser import sound_to_mixer

            return sound_to_mixer(config.settings)
        return False

    def _refresh_audio(self) -> None:
        sounds = [s for s in self.collection.sources if self._has_sound(s)]
        self.live_side.show_sound([self._sound_item(s) for s in sounds])
        if hasattr(self, "sound_strips"):
            ordered = sorted(sounds, key=lambda s: s.name.lower())  # A to Z, like the Sound list on the left
            self.sound_strips.show_sound([self._sound_item(s) for s in ordered], {s.id: s.kind for s in ordered},
                                         {s.id: self._plays_in_tag(s) for s in ordered})
        self.live_side.sound.set_mic_first(self.collection.audio.ducking)

    def _set_mic_first(self, on: bool) -> None:
        """Live's "Mic first" switch: ducking on or off (amounts stay as set in the Ducking window)."""
        self.collection.audio.ducking = on
        self._changed()

    def _sound_item(self, config) -> Sound:
        st = config.settings
        monitor = st.get("monitor", False)
        return Sound(
            id=config.id, name=config.name, muted=bool(st.get("muted", False)),
            group=str(st.get("group") or ("Alerts" if config.kind == "browser"
                                          else auto_group(config.name, config.kind))),
            pinned=bool(st.get("pinned", False)),
            volume_db=float(st.get("volume_db", 0.0)),
            monitor="only" if monitor == "only" else bool(monitor),
            mono=bool(st.get("mono", False)), balance=float(st.get("balance", 0.0) or 0.0),
            delay_ms=int(st.get("delay_ms", 0) or 0), voice=is_voice(st, config.kind),
            filters=[f.name for f in config.filters if f.enabled],
            can_monitor=config.kind != "audio_output",  # desktop audio already plays on your speakers
            tracks=source_tracks(config),
            apps=self._app_sound_text(config) if config.kind == "app_audio" else None,
        )

    @staticmethod
    def _app_sound_text(config) -> str:
        from sprigcast.sources.app_audio import describe, follows_game

        text = describe(config.settings)
        if follows_game(config.settings) and config.settings.get("apps"):
            text += ", and the full-screen game in front"
        return text

    def _add_app_sound(self, key: str) -> None:
        """A type of sound (Games, Music, Voice chat...) on its own row, catching only its apps."""
        from sprigcast.sources.app_audio import TYPES

        same = next((c for c in self.collection.sources
                     if c.kind == "app_audio" and c.settings.get("preset") == key), None)
        if same is not None:
            self.statusBar().showMessage(f"{same.name!r} is already in Sound. Change its apps there.", 6000)
            if self.mode == "build":
                self._open_build_sound(same.id)
                self._refresh_scenes()
            return
        kind = TYPES[key]
        config = SourceConfig(id=self.collection.new_id(f"sound-{key}"), kind="app_audio", name=kind.name,
                              settings={"preset": key, "apps": [], "volume_db": 0.0, "muted": False})
        self.collection.sources.append(config)
        self._changed()
        self._refresh_audio()
        self._refresh_scenes()
        if self.mode == "build":
            self._open_build_sound(config.id)

    def _pick_app_sound_apps(self, source_id: str) -> None:
        """Tick the apps an app sound catches: its usual ones, the game in front, or apps open now."""
        from sprigcast.sources.app_audio import TYPES, follows_game, running_apps

        config = self.collection.source(source_id)
        kind = TYPES.get(str(config.settings.get("preset", "")))
        picked = [str(a) for a in config.settings.get("apps") or []]
        menu = QMenu(self)
        from sprigcast.sources.audio import device_label

        outputs = [d.name for d in list_audio_devices(loopback=True, rescan=False)]
        device = str(config.settings.get("device") or "")
        if device and device not in outputs:
            outputs.append(device)  # not plugged in right now: still shown, ticked
        if outputs:
            menu.addSection("From an output")
            for name in outputs:
                act = menu.addAction(device_label(name))
                act.setCheckable(True)
                act.setChecked(name == device)
                act.triggered.connect(lambda _=False, d=name: self._set_app_sound(source_id, device=d))
            menu.addSection("From apps")
        usual = menu.addAction(f"Usual apps ({kind.hint})" if kind and kind.apps else "Usual apps")
        usual.setCheckable(True)
        usual.setChecked(not picked and not device)
        usual.setEnabled(bool(kind and kind.apps))
        usual.triggered.connect(lambda: self._set_app_sound(source_id, apps=[]))
        game = menu.addAction("The full-screen game in front")
        game.setCheckable(True)
        game.setChecked(follows_game(config.settings))
        game.triggered.connect(lambda on: self._set_app_sound(source_id, auto_game=on))
        menu.addSection("Apps open now")
        lower = {a.lower() for a in picked}
        apps = running_apps() or []
        for app in sorted(set(apps) | set(picked), key=str.lower):
            action = menu.addAction(app[:-4] if app.lower().endswith(".exe") else app)
            action.setCheckable(True)
            action.setChecked(app.lower() in lower)
            action.triggered.connect(lambda on, a=app: self._set_app_sound(
                source_id, apps=[p for p in picked if p.lower() != a.lower()] + ([a] if on else [])))
        if not apps and not picked:
            menu.addAction("Open the app, then come back here").setEnabled(False)
        menu.exec(QCursor.pos())

    def _set_app_sound(self, source_id: str, **changes) -> None:
        config = self.collection.source(source_id)
        if "device" not in changes and ("apps" in changes or "auto_game" in changes):
            changes["device"] = ""  # picking apps takes the sound from apps again, not an output
        settings = {**config.settings, **changes}
        if "apps" in changes and "auto_game" not in config.settings:  # picking apps by hand: just those
            from sprigcast.sources.app_audio import follows_game

            settings["auto_game"] = follows_game(config.settings) and not changes["apps"]
        config.settings = settings
        self._changed()
        self._refresh_audio()
        if self.selected_sound_id == source_id:
            self._open_build_sound(source_id)

    def _on_sound_changed(self, source_id: str, changes: dict) -> None:
        """Live's Sound card changed a sound; volume and left/right apply without rebuilding the card."""
        config = self.collection.source(source_id)
        config.settings = {**config.settings, **changes}
        self._changed()
        if set(changes) & {"muted", "group", "pinned", "monitor", "duck_voice"}:
            self._refresh_audio()

    def _on_audio_changed(self, source_id: str, changes: dict) -> None:
        """A mixer control changed: volume_db, muted, monitor, hidden or duck_voice."""
        config = self.collection.source(source_id)
        config.settings = {**config.settings, **changes}
        self._changed()
        if "hidden" in changes or "duck_voice" in changes or "muted" in changes:
            self._refresh_audio()
        if changes.get("hidden"):
            self.statusBar().showMessage(f"Hid {config.name!r} from the mixer. It still plays.", 5000)

    def _open_ducking(self) -> None:
        dialog = DuckingDialog(self.collection, self)
        if dialog.exec():
            dialog.apply_to(self.collection)
            self._changed()
            self._refresh_audio()

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

    def _add_audio(self, kind: str, device: str | None = None) -> None:
        """Add a mic or desktop sound. Sound belongs to no bloom or shape: it plays everywhere, once."""
        if device is None:
            device = self._pick_audio_device(kind)
        if device is None:
            return
        same = next((c for c in self.collection.sources
                     if c.kind == kind and str(c.settings.get("device", "default")) == device
                     and not self.collection.device_off(c)), None)
        if same is not None:  # already here: one source serves every bloom on both shapes
            self.statusBar().showMessage(f"{same.name!r} is already in Sound: it plays in every bloom, on both "
                                         "shapes, so there's no need to add it again.", 8000)
            if self.mode == "build":
                self._open_build_sound(same.id)
                self._refresh_scenes()
            return
        base = "Desktop audio" if kind == "audio_output" else "Mic"
        name = base if device == "default" else ask_name(self, short_name(device, base), f"{base} ({device})")
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
        self._refresh_scenes()

    def _fill_quick_filter_menu(self, menu: QMenu, source_id: str, kind: str, key: str,
                                choices: tuple[tuple[str, str], ...]) -> None:
        """A tick list for the Shape or Color look filter: pick one, or None to take it off."""
        source = self.collection.source(source_id)
        current = next((f for f in source.filters if f.kind == kind and f.enabled), None)
        value = settings_for(kind, current.settings)[key] if current else None
        none = menu.addAction("Rectangle (none)" if kind == "shape" else "None")
        none.setCheckable(True)
        none.setChecked(value is None)
        none.triggered.connect(lambda _=False: self._set_quick_filter(source_id, kind, key, None))
        menu.addSeparator()
        for data, label in choices:
            if data == "image":
                continue
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == data)
            action.triggered.connect(lambda _=False, d=data: self._set_quick_filter(source_id, kind, key, d))
        menu.addSeparator()
        more = "My own shape, border, soft edge…" if kind == "shape" else "From a .cube file, strength…"
        menu.addAction(more).triggered.connect(
            lambda _=False: self._set_quick_filter(source_id, kind, key, value or choices[0][0], open_filters=True))

    def _set_quick_filter(self, source_id: str, kind: str, key: str, value: str | None,
                          open_filters: bool = False) -> None:
        """Turn a source's Shape / Color look filter on with `value`, or remove it (None)."""
        source = self.collection.source(source_id)
        existing = next((f for f in source.filters if f.kind == kind), None)
        if value is None:
            if existing is not None:
                source.filters.remove(existing)
        else:
            if existing is None:
                info = filter_kind(kind)
                existing = Filter(id=uuid.uuid4().hex[:8], kind=kind, name=info.label, settings=dict(info.defaults))
                source.filters.append(existing)
            existing.enabled = True
            existing.settings = {**settings_for(kind, existing.settings), key: value}
        self._changed()
        if open_filters:
            self._open_filters(source_id)

    def _edit_filters(self) -> None:
        item = self._item()
        if item is None:
            QMessageBox.information(self, "SprigCast", "Select a source first.")
            return
        self._open_filters(item.source)

    def _open_filters(self, source_id: str, media: str | None = None) -> None:
        sounds = sorted(((c.id, c.name) for c in self.collection.sources if self._has_sound(c)),
                        key=lambda pair: pair[1].lower())
        FiltersDialog(self.collection.source(source_id), self._changed, self, media, sounds=sounds).exec()
        if self._has_sound(self.collection.source(source_id)):
            self._refresh_audio()  # the Sound card lists filter names
        self._refresh_sources(keep_row=self.source_list.currentRow())

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
            scene = self._shown_scene(canvas_id)
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
        self._show_spin_values(item)
        self._updating = False

    def _row_action(self, row: int, action: str) -> None:
        """An icon at the end of a Leaves row: lock, settings (gear) or remove (bin)."""
        if action == "lock":
            self._toggle_lock_row(row)
            return
        self.source_list.setCurrentRow(row)
        if action == "settings":
            self._edit_source_settings()
        elif action == "remove":
            self._remove_item()

    def _toggle_lock_row(self, row: int) -> None:
        """The lock at the end of a Leaves row: locked leaves can't be moved or picked on the canvas."""
        scene = self._scene()
        if scene is None or not 0 <= row < len(scene.items):
            return
        item = list(reversed(scene.items))[row]
        item.locked = not item.locked
        self._changed()
        self._refresh_sources(keep_row=row)

    def _on_locked_toggled(self, locked: bool) -> None:
        item = self._item()
        if self._updating or item is None:
            return
        item.locked = locked
        self._changed()
        row = self.source_list.currentItem()
        if row is not None:
            row.setData(ROLE_LOCKED, locked)

    # Scene actions ---------------------------------------------------------------

    def _on_scene_selected(self) -> None:
        if self._updating:
            return
        selected = self.scene_tree.selectedItems()
        if selected and selected[0].data(0, ROLE_ROW) == "sound":
            self._open_build_sound(selected[0].data(0, ROLE_ID))
            return
        if self.selected_sound_id is not None:
            self._close_build_sound(refresh=False)
        scene_id = selected[0].data(0, ROLE_ID) if selected else None
        folder_id = selected[0].data(0, ROLE_FOLDER) if selected else None
        if folder_id:
            self.selected_folder_id = folder_id  # new blooms go in here
        if scene_id:
            scene = self.collection.scene(scene_id)
            self.selected_folder_id = self.collection.main_part(scene).folder if scene.kind == "part" else scene.folder
            if scene.kind == "show":
                self.last_bloom_id = scene.id
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
        canvas_id = self.build_canvas  # the Build tab you're on
        title = f"New {canvas_id} {SHOW}"
        name, ok = QInputDialog.getText(self, title, "Name:")
        if not ok or not name.strip():
            return
        linked = None
        shows = [s for s in self.collection.scenes if s.kind == "show" and s.canvas != canvas_id]
        if canvas_id != self.collection.canvases[0].id and shows:
            link_options = ["(not linked)"] + [s.name for s in shows]
            # Suggest the main-canvas bloom with the same name, if there is one.
            guess = next((i + 1 for i, s in enumerate(shows) if s.name.lower() == name.strip().lower()), 0)
            link, ok = QInputDialog.getItem(self, title, "Switch together with:", link_options, guess, False)
            if not ok:
                return
            linked = next((s.id for s in shows if s.name == link), None)
        folder = self.selected_folder_id
        if folder is not None and all(f.id != folder for f in self.collection.folders_for(canvas_id)):
            folder = None
        scene = Scene(id=self.collection.new_id("scene"), name=name.strip(), canvas=canvas_id, linked_to=linked,
                      folder=folder)
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
        folder = self.selected_folder_id
        if folder is not None and all(f.id != folder for f in self.collection.part_folders()):
            folder = None
        part = Scene(id=self.collection.new_id("part"), name=name, canvas=canvas_id, kind="part", folder=folder)
        self.collection.scenes.append(part)
        if add_to_show and show is not None:
            self._add_part_source(part.id, show.id)
        if show is not None:
            self.last_bloom_id = show.id
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

    def _copy_bloom(self, scene_id: str) -> None:
        """A second bloom with the same things inside, in the same place and folder (sources are shared)."""
        scene = self.collection.scene(scene_id)
        names = {s.name for s in self.collection.scenes}
        name, n = f"{scene.name} copy", 2
        while name in names:
            name, n = f"{scene.name} copy {n}", n + 1
        copy = scene.model_copy(deep=True, update={"id": self.collection.new_id("scene"), "name": name,
                                                   "linked_to": None})
        for item in copy.items:  # a sprig's "just in this bloom" tweaks live on its placement: give the copy its own
            config = self.collection.source(item.source)
            if config.kind == PART_KIND:
                own = config.model_copy(deep=True, update={"id": self.collection.new_id("part-src")})
                self.collection.sources.append(own)
                item.source = own.id
        self.collection.scenes.insert(self.collection.scenes.index(scene) + 1, copy)
        self.selected_scene_id = copy.id
        self.last_bloom_id = copy.id
        self._changed()
        self._refresh_all()

    def _vcam_tip_due(self, config: SourceConfig) -> bool:
        from sprigcast.sources.camera import is_virtual

        return (config.kind == "camera" and is_virtual(str(config.settings.get("name", "")))
                and not QSettings().value(f"tips/vcam_hidden/{config.id}", False, type=bool))

    def _hide_vcam_tip(self) -> None:
        item = self._item()
        if item is not None:
            QSettings().setValue(f"tips/vcam_hidden/{item.source}", True)
        self.prop_vcam.hide()

    def _use_real_camera(self) -> None:
        """Swap the virtual camera for the physical one behind it (same size and frame rate where it can)."""
        from sprigcast.sources.camera import real_camera

        item = self._item()
        if item is None:
            return
        config = self.collection.source(item.source)
        device = real_camera(list_cameras(), str(config.settings.get("name", "")))
        if device is None:
            self._edit_source_settings()  # nothing obvious: let them pick
            return
        wanted = {k: config.settings[k] for k in ("width", "height", "fps", "mirror", "pause_off_screen")
                  if k in config.settings}
        dialog = CameraDialog([device], {"name": device.name, "path": device.path, **wanted}, self,
                              max_fps=self._camera_max_fps())
        settings = dialog.settings()
        dialog.deleteLater()
        config.settings = {**config.settings, **settings, "name": device.name, "path": device.path}
        self._changed()
        self.statusBar().showMessage(f"Now using {device.name}.", 4000)
        self._refresh_properties()

    def _fix_problem(self) -> None:
        """The inspector's fix button: pick the device/window/file again, or start the source again."""
        item = self._item()
        if item is None:
            return
        config = self.collection.source(item.source)
        if getattr(self, "_problem_fix", "") == "retry":
            if config.kind == "browser" and self.browser_host:
                self.browser_host.reload(config.id)
            else:  # any settings change makes the engine start the source afresh
                config.settings = {**config.settings, "retry": int(config.settings.get("retry", 0)) + 1}
                self._changed()
            self.source_statuses.pop(config.id, None)
            self.statusBar().showMessage(f"Starting {config.name!r} again…", 4000)
            self._refresh_properties()
        else:
            self._edit_source_settings()

    def _bloom_links(self, scenes) -> str:
        """Bloom names as links (click one to edit it), for rich-text labels."""
        import html

        return ", ".join(f'<a href="{s.id}" style="color: {theme.ACCENT}; text-decoration: none;">'
                         f"{html.escape(s.name)}</a>" for s in scenes)

    def _jump_to_bloom(self, scene_id: str) -> None:
        """A bloom link: leave any sprig and edit that bloom (on its own shape)."""
        if scene_id not in {s.id for s in self.collection.scenes if s.kind == "show"}:
            return
        canvas_id = self.collection.scene(scene_id).canvas
        if canvas_id != self.build_canvas and canvas_id in self.previews:
            self._swap_shape(canvas_id)
        self._pick_bloom(scene_id)

    def _open_build_sound(self, source_id: str) -> None:
        """A sound picked in the library: its controls take the right column (Inside comes back with a bloom)."""
        config = next((c for c in self.collection.sources if c.id == source_id), None)
        if config is None:
            return
        self.selected_sound_id = source_id
        self.sound_strips.set_selected(source_id)
        blooms = self.collection.sound_blooms(config)
        self.build_tune.show_plays_in(self._names([self.collection.scene(b) for b in blooms]) if blooms
                                      else f"Every {SHOW}")
        self.build_tune.show_sound(self._sound_item(config))
        self.build_stage.right_stack.setCurrentIndex(1)

    def _plays_in_popup(self) -> None:
        if self.selected_sound_id is None:
            return
        menu = QMenu(self)
        self._fill_plays_in_menu(menu, self.selected_sound_id)
        menu.exec(QCursor.pos())

    def _close_build_sound(self, refresh: bool = True) -> None:
        self.selected_sound_id = None
        self.sound_strips.set_selected(None)
        self.build_stage.right_stack.setCurrentIndex(0)
        if refresh:
            self._refresh_scenes()

    def _build_tune_changed(self, source_id: str, changes: dict) -> None:
        self._on_sound_changed(source_id, changes)
        self._refresh_audio()  # Live's Sound card shows the same sound
        if "muted" in changes:
            self._refresh_scenes()

    def _sprig_dropped(self, _canvas_id: str, part_id: str) -> None:
        """A sprig dragged from the library onto the canvas or into Inside: put it in what you're editing."""
        if self.mode != "build" or part_id not in {p.id for p in self.collection.parts()}:
            return
        self._add_part_source(part_id)

    # Live states ---------------------------------------------------------------------

    def _edit_states(self, part_id: str) -> None:
        part = self.collection.main_part(self.collection.scene(part_id))
        dialog = StatesDialog(self.collection, part, self)
        if dialog.exec():
            dialog.apply_to(part)
            self._changed()
            self._refresh_scenes()
            self.live_panel.show_scenes(self.collection)

    def _set_sprig_state(self, part_id: str, name: str | None) -> None:
        """From Live (or a hotkey): switch a sprig's look everywhere it's used. The engine fades it."""
        self.collection.set_state(part_id, name)
        self._changed()
        self.live_panel.show_scenes(self.collection)

    # Per-placement tweaks ------------------------------------------------------------

    def _fill_tweaks_menu(self, menu: QMenu, placement_id: str) -> None:
        """Tick what this placement shows. Unticked items are hidden here only; the sprig itself is unchanged."""
        placement = self.collection.source(placement_id)
        part = self.collection.part_of(placement)
        if part is None:
            return
        menu.setToolTipsVisible(True)
        hidden = self.collection.hidden_here(placement)
        seen: set[str] = set()
        for shape in self.collection.shapes(part):
            for inner in reversed(shape.items):  # top-most first, like the Sources list
                if inner.source in seen:
                    continue
                seen.add(inner.source)
                action = menu.addAction(literal(self.collection.source(inner.source).name))
                action.setCheckable(True)
                action.setChecked(inner.source not in hidden)
                action.setToolTip(f"Untick to hide it in this {SHOW} only. Other {SHOWS} using the {PART} still show it.")
                action.toggled.connect(lambda on, sid=inner.source: self._set_hidden_here(placement_id, sid, not on))
        if not seen:
            menu.addAction(f"The {PART} is empty").setEnabled(False)
        menu.addSeparator()
        reset = menu.addAction("Show everything (undo tweaks)")
        reset.setEnabled(bool(hidden))
        reset.triggered.connect(lambda: self._reset_tweaks(placement_id))

    def _set_hidden_here(self, placement_id: str, inner_id: str, hide: bool) -> None:
        placement = self.collection.source(placement_id)
        hidden = self.collection.hidden_here(placement)
        hidden = hidden | {inner_id} if hide else hidden - {inner_id}
        settings = {k: v for k, v in placement.settings.items() if k != "hide"}
        if hidden:
            settings["hide"] = sorted(hidden)
        placement.settings = settings
        self._changed()
        self._refresh_source_labels()

    def _reset_tweaks(self, placement_id: str) -> None:
        placement = self.collection.source(placement_id)
        placement.settings = {k: v for k, v in placement.settings.items() if k != "hide"}
        self._changed()
        self._refresh_source_labels()

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
        self.part_path.setText(f"{scene.canvas.capitalize()} {canvas.width}×{canvas.height}")
        names = self._bloom_links(uses)
        self.part_uses_note.setText(
            f"This {PART} is in {len(uses)} {SHOW if len(uses) == 1 else SHOWS}: {names}. "
            "What you change here changes in all of them." if uses else
            f"This {PART} isn't in any {SHOW} yet. Pick {a(SHOW)}, then + Add and choose it.")
        back = getattr(self, "last_bloom_id", None)
        back_scene = next((s for s in self.collection.scenes if s.id == back and s.kind == "show"), None)
        self.part_back.setText(f"← {back_scene.name}" if back_scene else "← Back")
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
        show = self._shown_scene(canvas_id)
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
        show = self._shown_scene(canvas_id)
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
        """Back to the bloom you came from (or the one on air)."""
        canvas_id = self.collection.canvases[0].id
        back = getattr(self, "last_bloom_id", None)
        if back not in {s.id for s in self.collection.scenes if s.kind == "show"}:
            back = self.collection.active_scenes.get(canvas_id)
        self.selected_scene_id = back
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
        if scene.kind == "show" and config.kind != PART_KIND:  # blooms hold sprigs only: it goes into the bloom's sprig, opened to edit
            scene = self._open_bloom_sprig(scene)
        self.collection.sources.append(config)
        scene.items.append(SceneItem(source=config.id))
        self._changed()
        self._refresh_sources(keep_row=0)

    def _open_bloom_sprig(self, show: Scene) -> Scene:
        """The sprig this bloom shows (made, named after it, if it has none yet), selected for editing."""
        part = self.collection.bloom_sprig(show)
        if part is None:
            part = Scene(id=self.collection.new_id("part"), name=show.name, canvas=show.canvas, kind="part")
            self.collection.scenes.append(part)
            placement = SourceConfig(id=self.collection.new_id("part-src"), kind=PART_KIND, name=show.name,
                                     settings={"scene": part.id})
            self.collection.sources.append(placement)
            show.items.append(SceneItem(source=placement.id))
        shape = self.collection.shape_for(part, show.canvas)
        self.last_bloom_id = show.id
        self.selected_scene_id = shape.id
        self._refresh_scenes()
        self._show_part_editor()
        return shape

    def _add_color(self) -> None:
        scene = self._scene()
        if scene is None:
            return
        color = QColorDialog.getColor(QColor(theme.ACCENT), self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if not color.isValid():
            return
        canvas = self.collection.canvas(scene.canvas)
        hex_color = f"#{color.red():02x}{color.green():02x}{color.blue():02x}{color.alpha():02x}"
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

    def _add_chat(self) -> None:
        """Your mixed chat drawn on the canvas (sources/chat.py). Sized to fit the canvas's shape."""
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        from sprigcast.sources.chat import DEFAULTS

        canvas = self.collection.canvas(scene.canvas)
        settings = dict(DEFAULTS)
        if canvas.height > canvas.width:  # vertical: a wide, short strip near the bottom, bigger words for phones
            settings.update(width=round(canvas.width * 0.8), height=round(canvas.height * 0.3),
                            size=round(canvas.width / 27))
        else:
            settings.update(width=round(canvas.width * 0.25), height=round(canvas.height * 0.6),
                            size=round(canvas.height / 40))
        self._add_item(SourceConfig(id=self.collection.new_id("chat"), kind="chat", name="Chat", settings=settings))
        item = scene.items[-1]
        item.x = round(canvas.width * (0.1 if canvas.height > canvas.width else 0.72))
        item.y = round(canvas.height * (0.55 if canvas.height > canvas.width else 0.3))
        self._changed()
        self._refresh_properties()
        if not self.chat_panel.reading():
            self.statusBar().showMessage("Add your channels in the Chat panel (⋯ → Channels…) to fill it.", 8000)

    def _interact(self, source_id: str) -> None:
        """Open a browser source's page on screen so you can click and type in it."""
        source = next((s for s in self.collection.sources if s.id == source_id), None)
        if source is None or self.browser_host is None:
            return
        self.browser_host.interact(source_id, source.settings, source.name, self)

    def _add_browser(self) -> None:
        if self.browser_host is None:
            QMessageBox.information(self, "SprigCast", "Browser sources need Qt WebEngine, which isn't installed.")
            return
        self._add_from_class("browser", "Browser")
        config = self.collection.sources[-1] if self.collection.sources else None
        if config is not None and config.kind == "browser":
            from sprigcast.sources.browser import page_name

            config.name = page_name(config.settings)
            self._changed()
            self._refresh_sources(keep_row=self.source_list.currentRow())

    def _add_from_class(self, kind: str, name: str) -> None:
        """Add a source whose settings form is described by its class's `defaults` (text and plugins)."""
        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        cls = source_class(kind)
        defaults = dict(getattr(cls, "defaults", {}) or {})
        fills = getattr(cls, "fills_canvas", False)  # like a slide show: starts the size of the canvas, at 0, 0
        if fills and {"width", "height"} <= set(defaults):
            canvas = self.collection.canvas(self._scene().canvas)
            defaults.update(width=canvas.width, height=canvas.height)
        if kind == "browser":
            from sprigcast.app.web_page_dialog import WebPageDialog

            dialog = WebPageDialog("New web page", defaults, self, primary="Add")
        else:
            dialog = SettingsDialog(f"New {name}", defaults, defaults, self)
        if not dialog.exec():
            return
        settings = dialog.values()
        scene = self._scene()
        config = SourceConfig(id=self.collection.new_id(kind), kind=kind, name=_name_from(settings, name),
                              settings=settings)
        size = getattr(cls, "own_sprig", None)  # overlays like Snap: a sprig of their own, filling the canvas
        if size:
            if scene.kind != "show":  # editing a sprig: it goes on top of the bloom you came from
                bloom = next((s for s in self.collection.scenes
                              if s.id == getattr(self, "last_bloom_id", None) and s.kind == "show"), None)
                scene = bloom or scene
            self.collection.sources.append(config)
            self.collection.own_sprig(scene, config.id, tuple(size), config.name)
            self._changed()
            self._refresh_all()
            return
        self._add_item(config)
        scene = self._scene() or scene  # a bloom sends it into its sprig: place it there
        item = next((i for i in scene.items if i.source == config.id), None)
        if item is None:
            return
        canvas = self.collection.canvas(scene.canvas)
        if fills:
            w, h = int(settings.get("width") or canvas.width), int(settings.get("height") or canvas.height)
            item.scale = min(canvas.width / max(1, w), canvas.height / max(1, h))
            item.scale_y = None
            item.x = (canvas.width - w * item.scale) / 2
            item.y = (canvas.height - h * item.scale) / 2
            self._changed()
            self._refresh_properties()
            return
        # Start each new one a little lower so several don't pile up in the corner.
        stacked = sum(1 for i in scene.items if i is not item and self.collection.source(i.source).kind == kind)
        item.x = round(canvas.width * 0.05)
        item.y = round(canvas.height * 0.05) + (stacked * 100) % max(1, canvas.height // 2)
        self._changed()
        self._refresh_properties()

    def _fill_more_menu(self) -> None:
        """List source kinds from plugins (any registered class with a `label` that has no button of its own)."""
        entries = sorted(
            (getattr(source_class(kind), "label", None), kind)
            for kind in known_kinds()
            if kind not in BUILT_IN_KINDS and getattr(source_class(kind), "label", None)
        )
        if self.more_menu.actions() and getattr(self.more_menu, "_entries", None) == entries:
            return  # rebuilding a menu as it opens swaps the entries under the pointer: the first click was lost
        self.more_menu._entries = entries
        self.more_menu.clear()
        for label, kind in entries:
            action = self.more_menu.addAction(label)
            # After the menu has closed: a window opened from inside a closing menu could swallow the click.
            action.triggered.connect(
                lambda _=False, k=kind, n=label: QTimer.singleShot(0, lambda: self._add_from_class(k, n)))
        if not entries:
            empty = self.more_menu.addAction("No plugin sources installed")
            empty.setEnabled(False)
        self.more_menu.addSeparator()
        self.more_menu.addAction("Plugin store…").triggered.connect(lambda: self._open_settings("Plugins"))
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
            SourceConfig(id=self.collection.new_id("image"), kind="image",
                         name=ask_name(self, short_name(Path(path).stem.replace("_", " "), "Image"), Path(path).stem),
                         settings={"path": path})
        )

    def _add_media(self, sound: bool = False) -> None:
        """A video goes into the selected bloom like a picture; a sound file goes into Sound (every bloom)."""
        title = "Choose a sound file" if sound else "Choose a video or sound file"
        path, _ = QFileDialog.getOpenFileName(self, title, "", media_file_filter())
        if not path:
            return
        stem = Path(path).stem
        config = SourceConfig(id=self.collection.new_id("media"), kind="media",
                              name=ask_name(self, short_name(stem.replace("_", " "), "Media"), stem),
                              settings={"path": path, "volume_db": 0.0, "muted": False, "loop": False})
        if is_sound_file(path):
            self.collection.sources.append(config)
            self._changed()
            self._say(f"{config.name!r} is in Sound. Right-click it to play.")
        else:
            self._add_item(config)
        self._refresh_audio()
        self._refresh_scenes()

    def media_source(self, source_id: str):
        """The playing media source (from the engine), or None."""
        source = self.engine._sources.get(source_id)
        return source if getattr(source, "kind", "") == "media" else None

    def media_control(self, source_id: str, what: str, seconds: float | None = None) -> dict | None:
        """play, pause, toggle, restart, stop or seek a video or sound file. Returns where it is now."""
        source = self.media_source(source_id)
        if source is None:
            return None
        if what == "seek":
            source.seek(float(seconds or 0.0))
        else:
            {"play": source.play, "pause": source.pause, "toggle": source.toggle, "restart": source.restart,
             "stop": source.stop_playing}[what]()
        if getattr(self, "control", None) is not None:
            self.control.soon()
        return source.info()

    @property
    def api(self):
        """The SprigCast API, for plugins (the same one apps use)."""
        return self.control.native

    def _fill_plugin_menu(self, menu: QMenu, source_id: str) -> None:
        """Right-click entries plugins add (Barrel roll, Hide it, Snap...)."""
        from sprigcast import plugin_hooks

        entries: list[tuple[str, object]] = []
        plugin_hooks.fill_item_menu(self.api, self.collection.source(source_id), lambda label, fn: entries.append((label, fn)))
        if entries:
            menu.addSeparator()
        for label, fn in entries:
            menu.addAction(label).triggered.connect(lambda _=False, f=fn: f())

    def _fill_media_menu(self, menu: QMenu, source_id: str) -> None:
        source = self.media_source(source_id)
        playing = source is not None and source.state == "playing"
        for label, what in (("Pause" if playing else "Play", "toggle"), ("Start over", "restart"), ("Stop", "stop")):
            action = menu.addAction(label)
            action.setEnabled(source is not None)
            action.triggered.connect(lambda _=False, w=what: self.media_control(source_id, w))
        config = self.collection.source(source_id)
        loop = menu.addAction("Loop")
        loop.setCheckable(True)
        loop.setChecked(bool(config.settings.get("loop")))
        loop.toggled.connect(lambda on: self._set_media_loop(source_id, on))

    def _set_media_loop(self, source_id: str, on: bool) -> None:
        config = self.collection.source(source_id)
        config.settings = {**config.settings, "loop": on}
        self._changed()

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

    def _add_display(self, monitor=None) -> None:
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        monitor = monitor or self._pick_monitor()
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

    def _add_mirror(self, source_id: str) -> None:
        """The same leaf again in this bloom (like OBS's Source Clone, without a second copy running)."""
        scene = self._scene()
        if scene is None:
            return
        scene.items.append(SceneItem(source=source_id))
        self._changed()
        self._refresh_sources(keep_row=0)

    def _add_game(self, window=None) -> None:
        """Game capture: the full-screen game in front, or one game's window, with the game's own sound."""
        from sprigcast.core.capture_sound import APP, set_capture_sound

        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        if window is None:
            settings = {"mode": "any", "cursor": False}
            name = "Game"
        else:
            settings = {"mode": "window", "title": window.title, "process": window.process, "cursor": False}
            name = window_name(window.title, window.process)
        config = SourceConfig(id=self.collection.new_id("game"), kind="game_capture", name=name, settings=settings)
        self._add_item(config)
        set_capture_sound(self.collection, config, APP)  # like OBS's Capture audio: the game's own sound
        self._changed()
        self._refresh_audio()

    def _game_grab(self, source_id: str) -> None:
        """A game capture's hotkey: take the window in front (and its sound, if it brings its own)."""
        from sprigcast.core.capture_sound import follow_capture
        from sprigcast.sources.win32_enum import foreground_window

        config = next((c for c in self.collection.sources if c.id == source_id and c.kind == "game_capture"), None)
        window = foreground_window()
        if config is None or window is None:
            return
        config.settings = {**config.settings, "title": window.title, "process": window.process}
        follow_capture(self.collection, config)
        self._changed()
        self._refresh_audio()
        self.statusBar().showMessage(f"{config.name} now shows {window_name(window.title, window.process)}.", 5000)

    def _add_window(self, window=None) -> None:
        if self._scene() is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        window = window or self._pick_window()
        if window is None:
            return
        name = ask_name(self, window_name(window.title, window.process), window.title)
        self._add_item(
            SourceConfig(
                id=self.collection.new_id("window"),
                kind="window_capture",
                name=name,
                settings={"title": window.title, "process": window.process, "cursor": True},
            )
        )

    def _camera_max_fps(self) -> int:
        rates = [int(round(c.fps)) for c in self.collection.canvases if c.enabled]
        return max(rates) if rates else 60

    def _camera_dialog(self, current: dict | None = None, source_id: str | None = None) -> dict | None:
        cameras = list_cameras()
        if not cameras:
            QMessageBox.information(self, "SprigCast", "No cameras found. Is one plugged in and not in use by another app?")
            return None
        source = self.engine._sources.get(source_id) if source_id else None
        modes = getattr(source, "modes_seen", None) or None
        dialog = CameraDialog(cameras, current, self, max_fps=self._camera_max_fps(), modes=modes)
        return dialog.settings() if dialog.exec() else None

    def _add_camera(self, device=None) -> None:
        """Add a camera: pick one and its mode, or (from + Add's list) that camera at its usual mode."""
        scene = self._scene()
        if scene is None:
            QMessageBox.information(self, "SprigCast", "Select a scene first.")
            return
        if device is not None:
            dialog = CameraDialog([device], {"name": device.name, "path": device.path}, self,
                                  max_fps=self._camera_max_fps())
            settings = dialog.settings()  # the mode it would suggest, without asking
            dialog.deleteLater()
            name = short_name(settings["name"], "Camera")
        else:
            settings = self._camera_dialog()
            if settings is None:
                return
            name = ask_name(self, short_name(settings["name"], "Camera"), settings["name"])
        self._add_item(SourceConfig(id=self.collection.new_id("camera"), kind="camera", name=name, settings=settings))
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
        if item.bounded:  # the box fills the canvas; the picture fits in it its own way
            item.bounds_size, item.rotation = (float(canvas.width), float(canvas.height)), 0.0
            item.x = item.y = 0.0
            self._changed()
            self._refresh_properties()
            return
        item.scale = min(canvas.width / w, canvas.height / h)
        item.scale_y = None
        item.rotation = 0.0
        item.x = (canvas.width - w * item.scale) / 2
        item.y = (canvas.height - h * item.scale) / 2
        self._changed()
        self._refresh_properties()

    def _reset_crop(self) -> None:
        item = self._item()
        if item is None:
            return
        item.crop = (0, 0, 0, 0)
        self._changed()
        self._refresh_properties()

    def _edit_item(self, change) -> None:
        """Apply `change(item, canvas size, source size)` to the selected item, then save and redraw."""
        scene, item = self._scene(), self._item()
        if scene is None or item is None:
            return
        size = self.source_sizes.get(item.source)
        if not size or not size[0] or not size[1]:
            self.statusBar().showMessage("That source has no picture yet. Try again in a second.", 4000)
            return
        canvas = self.collection.canvas(scene.canvas)
        change(item, (canvas.width, canvas.height), size)
        self._changed()
        self._refresh_properties()

    def _stretch_item(self) -> None:
        def change(item, canvas, size):
            cw, ch = item.cropped_size(size)
            item.rotation, item.x, item.y, item.bounds = 0.0, 0.0, 0.0, "off"
            item.scale = canvas[0] / cw
            item.scale_y = None if abs(canvas[1] / ch - item.scale) < 1e-6 else canvas[1] / ch
        self._edit_item(change)

    def _fill_item(self) -> None:
        """Cover the whole canvas, keeping the shape: what sticks out is cropped evenly off both sides
        (a 4:3 or 5:3 camera on a 16:9 canvas: no empty strips, nothing squashed)."""
        def change(item, canvas, size):
            item.rotation, item.x, item.y, item.scale_y, item.bounds = 0.0, 0.0, 0.0, None, "off"
            item.crop, item.scale = cover(size, canvas)
        self._edit_item(change)

    def _center_item(self) -> None:
        def change(item, canvas, size):
            w, h = item.drawn_size(size)
            item.x, item.y = round((canvas[0] - w) / 2), round((canvas[1] - h) / 2)
        self._edit_item(change)

    def _reset_item_size(self) -> None:
        def change(item, canvas, size):
            item.scale, item.scale_y, item.rotation, item.crop, item.bounds = 1.0, None, 0.0, (0, 0, 0, 0), "off"
        self._edit_item(change)

    def _to_edge(self, front: bool) -> None:
        scene, index = self._scene(), self._item_index()
        if scene is None or index is None:
            return
        item = scene.items.pop(index)
        if front:
            scene.items.append(item)
        else:
            scene.items.insert(0, item)
        self._changed()
        self._refresh_sources(keep_row=0 if front else len(scene.items) - 1)

    def _toggle_item_visible(self) -> None:
        item = self._item()
        if item is None:
            return
        item.visible = not item.visible
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())

    def _build_item_actions(self) -> None:
        """Shortcuts for the selected thing in Build. They show in its right-click menu and work while
        the Build screen has focus (typing in a box still types)."""
        self.item_actions: dict[str, QAction] = {}
        table = (
            ("fit", "Fit to canvas", "Ctrl+F", self._fit_item),
            ("fill", "Fill the canvas (crop the edges)", "Ctrl+Alt+F", self._fill_item),
            ("stretch", "Stretch to fill the canvas", "Ctrl+Shift+F", self._stretch_item),
            ("center", "Centre on the canvas", "Ctrl+E", self._center_item),
            ("reset", "Reset size, crop and rotation", "Ctrl+R", self._reset_item_size),
            ("uncrop", "Remove crop", "Ctrl+Shift+R", self._reset_crop),
            ("front", "Bring to front", "Ctrl+Shift+Up", lambda: self._to_edge(True)),
            ("up", "Bring forward", "Ctrl+Up", lambda: self._move_item(+1)),
            ("down", "Send backward", "Ctrl+Down", lambda: self._move_item(-1)),
            ("back", "Send to back", "Ctrl+Shift+Down", lambda: self._to_edge(False)),
            ("hide", "Show / hide", "Ctrl+H", self._toggle_item_visible),
            ("lock", "Lock", "Ctrl+L", lambda: self.prop_locked.setChecked(not self.prop_locked.isChecked())),
            ("settings", "Settings…", "Ctrl+Return", self._edit_source_settings),
            ("filters", "Filters…", "Ctrl+Shift+E", self._edit_filters),
            ("rename", "Rename…", "F2", self._rename_item),
            ("remove", "Remove", "Delete", self._remove_item),
        )
        for key, text, shortcut, slot in table:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.setShortcutVisibleInContextMenu(True)
            action.triggered.connect(lambda _=False, f=slot: f())
            self.build_stage.addAction(action)
            self.item_actions[key] = action

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

    def _show_spin_values(self, item: SceneItem) -> None:
        """Fill Place (X and Y are the pin point), Bounds and Crop for the selected item."""
        size = self.source_sizes.get(item.source)
        known = bool(size and size[0] and size[1])
        w, h = item.drawn_size(size) if known else (0.0, 0.0)
        x, y = item.pin(size) if known else (item.x, item.y)
        values = {"x": x, "y": y, "w": w, "h": h, "rotation": item.rotation}
        for key, box in self.spin.items():
            box.blockSignals(True)
            box.setValue(float(values[key]))
            box.blockSignals(False)
        for key in ("w", "h"):
            self.spin[key].setEnabled(bool(w and h))
        self.pin_grid.set_current(item.align)
        self.bounds_grid.set_current(item.bounds_align)
        self.bounds_mode.blockSignals(True)
        self.bounds_mode.setCurrentIndex(max(0, self.bounds_mode.findData(item.bounds if item.bounded else "off")))
        self.bounds_mode.blockSignals(False)
        self.bounds_grid.setVisible(item.bounded and item.bounds != "stretch")
        for side, box in self.crop_spin.items():
            box.blockSignals(True)
            if known:
                box.setMaximum(max(0, (size[0] if side in (0, 2) else size[1]) - 2))
            box.setValue(float(item.crop[side]))
            box.blockSignals(False)

    def _item_and_size(self):
        """The selected item and its source's size, or (None, None) while either is missing."""
        item = self._item()
        size = self.source_sizes.get(item.source) if item is not None else None
        if item is None or not size or not size[0] or not size[1]:
            return None, None
        return item, size

    def _after_place_edit(self, item: SceneItem) -> None:
        self._changed()
        self._updating = True
        self._show_spin_values(item)
        self._updating = False
        self._refresh_previews()

    def _on_transform_edited(self, key: str, value: float) -> None:
        if self._updating:
            return
        item = self._item()
        if item is None:
            return
        size = self.source_sizes.get(item.source)
        known = bool(size and size[0] and size[1])
        if key in ("x", "y"):
            if not known:
                setattr(item, key, value)
            else:
                x, y = item.pin(size)
                item.set_pin(size, value if key == "x" else x, value if key == "y" else y)
            self._changed()
            self._refresh_previews()
            return
        if key in ("w", "h"):
            if not known or value < 1:
                return
            pin = item.pin(size)
            if item.bounded:
                bw, bh = item.bounds_size
                if key == "w":
                    bh = bh * value / bw if self.keep_shape.isChecked() else bh
                    bw = value
                else:
                    bw = bw * value / bh if self.keep_shape.isChecked() else bw
                    bh = value
                item.bounds_size = (max(2.0, bw), max(2.0, bh))
            else:
                cw, ch = item.cropped_size(size)
                if not cw or not ch:
                    return
                sx, sy = item.scale, item.sy
                if key == "w":
                    new_sx = max(0.01, value / cw)
                    sy = sy * new_sx / sx if self.keep_shape.isChecked() else sy
                    sx = new_sx
                else:
                    new_sy = max(0.01, value / ch)
                    sx = sx * new_sy / sy if self.keep_shape.isChecked() else sx
                    sy = new_sy
                item.scale, item.scale_y = sx, (None if abs(sx - sy) < 1e-6 else sy)
            item.set_pin(size, *pin)  # it grows and shrinks around its pin point
            self._after_place_edit(item)
            return
        setattr(item, key, value)
        self._changed()
        self._refresh_previews()

    def _on_pin_picked(self, field: str, align: str) -> None:
        """A new pin point (stays put on the canvas) or a new spot for the picture in its box."""
        item, size = self._item_and_size()
        if item is None:
            return
        if field == "align":
            item.set_align(size, align)
        else:
            item.bounds_align = align
        self._after_place_edit(item)

    def _on_bounds_mode(self) -> None:
        if self._updating:
            return
        item, size = self._item_and_size()
        if item is None:
            return
        mode = self.bounds_mode.currentData()
        pin = item.pin(size)
        if mode != "off" and not item.bounded:
            item.bounds_size = item.drawn_size(size)  # the box starts where it is now
        elif mode == "off" and item.bounded:
            sx, sy = item.picture_scale(size)  # keep the picture's size
            item.scale, item.scale_y = sx, (None if abs(sx - sy) < 1e-6 else sy)
        item.bounds = mode
        item.set_pin(size, *pin)
        self._after_place_edit(item)

    def _on_crop_edited(self, side: int, value: int) -> None:
        if self._updating:
            return
        item, size = self._item_and_size()
        if item is None:
            return
        crop = list(item.crop)
        crop[side] = value
        limit = size[0] if side in (0, 2) else size[1]
        other = 2 if side == 0 else 0 if side == 2 else 3 if side == 1 else 1
        crop[side] = min(value, max(0, limit - crop[other] - 2))
        pin = item.pin(size)
        item.crop = tuple(crop)
        item.set_pin(size, *pin)
        self._after_place_edit(item)
        self.prop_uncrop.setVisible(any(item.crop))

    def _set_keep_shape(self, on: bool) -> None:
        QSettings().setValue("edit/keep_aspect", on)
        for preview in [*self.previews.values(), self.part_preview]:
            preview.keep_aspect = on

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
                "color": f"#{color.red():02x}{color.green():02x}{color.blue():02x}{color.alpha():02x}",
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
            settings = self._camera_dialog(config.settings, config.id)
            if settings is None:
                return
            old = str(config.settings.get("name", ""))
            config.settings = settings
            if config.name in (old, short_name(old, "Camera")):  # a name you chose stays
                config.name = short_name(settings["name"], "Camera")
        elif config.kind == "game_capture":
            from sprigcast.app.game_dialog import GameCaptureDialog
            from sprigcast.core.capture_sound import APP, capture_sound, follow_capture, set_capture_sound

            dialog = GameCaptureDialog(config.settings, list_windows(), capture_sound(self.collection, config.id)
                                       is not None, self, list_windows=list_windows)
            if not dialog.exec():
                return
            changes, sound = dialog.values()
            config.settings = {**config.settings, **changes}
            if sound != (capture_sound(self.collection, config.id) is not None):
                set_capture_sound(self.collection, config, APP if sound else None)
            follow_capture(self.collection, config)
            self._refresh_audio()
        elif config.kind == "window_capture":
            window = self._pick_window()
            if window is None:
                return
            old = window_name(str(config.settings.get("title", "")), str(config.settings.get("process", "")))
            old_title = str(config.settings.get("title", ""))
            config.settings = {**config.settings, "title": window.title, "process": window.process}
            if config.name in (old, old_title, old_title[:37] + "…"):  # a name you chose stays
                config.name = window_name(window.title, window.process)
            from sprigcast.core.capture_sound import follow_capture

            follow_capture(self.collection, config)  # its own app sound follows the new window
        else:
            cls = source_class(config.kind)
            defaults = getattr(cls, "defaults", None) if cls is not None else None
            if not defaults:
                QMessageBox.information(self, "SprigCast", f"{config.kind!r} sources have no settings to edit.")
                return
            if config.kind == "browser":
                from sprigcast.app.web_page_dialog import WebPageDialog

                host = self.browser_host
                dialog = WebPageDialog(config.name, config.settings, self,
                                       on_reload=(lambda: host.reload(config.id)) if host else None)
            elif config.kind == "media":
                from sprigcast.app.media_dialog import MediaDialog

                dialog = MediaDialog(config.name, config.settings, self)
            else:
                dialog = SettingsDialog(config.name, defaults, config.settings, self)
            if not dialog.exec():
                return
            old_auto = _name_from(config.settings, config.name)
            config.settings = {**config.settings, **dialog.values()}  # volume, mute... aren't in the form
            if config.name == old_auto:  # the name followed the text, so keep it following
                config.name = _name_from(config.settings, config.name)
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())

    # Recording -----------------------------------------------------------------

    def _default_recordings_folder(self) -> str:
        videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
        return str(Path(videos or Path.home()) / "SprigCast")

    def _cheer_live(self) -> None:
        """A few seconds of "You're live!" over the Live screen, naming where it goes."""
        from sprigcast.app.empty import LiveCheer

        if getattr(self, "live_cheer", None) is None:
            self.live_cheer = LiveCheer(self.live_stage)
        names = [st.name for _, st in self.streaming.streamers] if self.streaming is not None else []
        self.live_cheer.cheer(", ".join(dict.fromkeys(names)))

    def _cheer_goodbye(self, seconds: int) -> None:
        """A few seconds of "Nice stream!" with the goodnight sloth when a stream ends."""
        from sprigcast.app.empty import LiveCheer

        if getattr(self, "live_cheer", None) is None:
            self.live_cheer = LiveCheer(self.live_stage)
        length = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}" if seconds >= 3600 \
            else f"{seconds // 60}:{seconds % 60:02d}"
        self.live_cheer.goodbye(f"Live for {length}")

    def _say(self, text: str) -> None:
        """A short note in the status bar, drawn now (the next step may keep the UI busy a moment)."""
        self.statusBar().showMessage(text, 6000)
        self.statusBar().repaint()

    def _recordings_folder(self) -> Path:
        return Path(self.collection.recording.folder or self._default_recordings_folder())

    def _screenshot(self, shapes: list[str] | None = None, width: int | None = None, save: bool = True,
                    as_data: bool = False) -> list[dict]:
        """A PNG of what's on air in each shape (or just shapes), in the recordings folder's Screenshots."""
        from sprigcast.app import screenshot

        ids = [c.id for c in self.collection.canvases if c.enabled and (not shapes or c.id in shapes)]
        folder = self._recordings_folder() / "Screenshots" if save else None
        images: dict = {}
        shots = screenshot.take(self.engine, ids, folder, width, as_data, images)
        if save:
            self._say(f"Screenshot saved to {folder}" if shots else "Couldn't take a screenshot: nothing is drawing.")
            self._tidy_saves()
            if shots:  # plugins such as Snap show it off
                from sprigcast import plugin_hooks

                plugin_hooks.screenshot_taken(self.api, [{**s, "image": images.get(s["shape"])} for s in shots])
        return shots

    def _tidy_saves(self) -> None:
        """Settings → Recording → Space: keep only the newest screenshots and clips, if asked."""
        from sprigcast.output import storage

        rec, root = self.collection.recording, self._recordings_folder()
        storage.keep_newest(root / "Screenshots", rec.keep_screenshots, storage.SCREENSHOT_TYPES)
        storage.keep_newest(root / "Clips", rec.keep_clips, storage.CLIP_TYPES)

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
        from sprigcast.output import storage

        # Room on the drive for every file this recording writes (each canvas, twice with source record).
        kbps = (settings.bitrate_kbps + settings.encoder.audio_kbps) * len(canvases) * (2 if settings.source_record else 1)
        self._space_watch = storage.SpaceWatch(self._recordings_folder(), kbps)
        space = self._space_watch.check()
        if space and (self._space_watch.left or 0) < 1:
            answer = QMessageBox.question(self, "SprigCast", f"{space}\n\nRecord anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._say("Starting the recording…")
        self._set_encoder_tracks()
        try:
            self.recording = SharedRecording.start(
                self.encoders, canvases, self._recordings_folder(), settings.bitrate_kbps, settings.container,
                encoder=settings.encoder, short_side=settings.short_side,
            )
        except Exception as exc:
            self.recording = None
            QMessageBox.critical(self, "SprigCast", f"Could not start recording:\n{exc}")
            return
        if settings.source_record:
            self._start_source_record(canvases)
        log.info("recording started: %s, %d kbps, %s, size %s, encoder %s preset %s",
                 ", ".join(c.id for c in canvases), settings.bitrate_kbps, settings.container,
                 f"{settings.short_side}p" if settings.short_side else "canvas", settings.encoder.rate_control,
                 settings.encoder.preset)
        self.live_side.set_recording("● REC 0:00:00")
        self._refresh_recording_status()
        if space:
            log.warning("recording: %s", space)
            self.statusBar().showMessage(space, 20000)

    # Replay buffer ---------------------------------------------------------------------

    def _start_source_record(self, canvases: list) -> None:
        """Source record: next to the recording, files without the sources picked in Settings."""
        from sprigcast.output.source_record import SourceRecording, WithSourceRecord

        settings = self.collection.recording
        known = {s.id for s in self.collection.sources}
        hidden = [i for i in settings.source_record_hide if i in known]
        try:
            extra = SourceRecording.start(
                self.engine, canvases, self._recordings_folder(), hidden, settings.bitrate_kbps,
                settings.container, encoder=settings.encoder, short_side=settings.short_side,
            )
        except Exception as exc:
            log.exception("source record failed to start")
            self.statusBar().showMessage(f"Recording, but source record could not start: {exc}", 10000)
            return
        self.recording = WithSourceRecord(self.recording, extra)
        log.info("source record started without %d sources", len(hidden))

    def _clip_key(self) -> tuple:
        """What clips are started with; a change while they're ready starts them again."""
        rec = self.collection.recording
        return (rec.replay_seconds, rec.clip_shapes, tuple(rec.audio_tracks), tuple(self.collection.audio.track_names))

    def _set_encoder_tracks(self) -> None:
        """New encoders carry the sound tracks recordings keep (Settings → Audio), named as set there."""
        from sprigcast.core.scene import AUDIO_TRACKS

        self.encoders.recording_tracks = self.collection.recording.track_ids()
        audio = self.collection.audio
        self.encoders.track_titles = {t: audio.track_name(i) for i, t in enumerate(AUDIO_TRACKS, 1)}

    def _clip_canvases(self) -> list:
        """The canvases a clip saves: Settings → Recording → Clips picks horizontal, vertical or both."""
        shapes = self.collection.recording.clip_shapes
        enabled = [c for c in self.collection.canvases if c.enabled]
        if shapes == "both":
            return enabled
        wanted = [c for c in enabled if (c.width >= c.height) == (shapes == "horizontal")]
        return wanted or enabled[:1]

    def _restart_clips_if_changed(self, before: tuple) -> None:
        """Clip length or shapes changed in Settings while clips are ready: start again with the new ones."""
        settings = self.collection.recording
        if self.replay is not None and before != self._clip_key():
            self._toggle_replay(False)
            self._toggle_replay(True)

    def _toggle_replay(self, on: bool) -> None:
        if on and self.replay is None:
            settings = self.collection.recording
            canvases = self._clip_canvases()
            if not canvases:
                QMessageBox.information(self, "SprigCast", "Turn on a canvas first (Settings → Video).")
                return
            self._set_encoder_tracks()
            try:
                self.replay = SharedReplay.start(  # clips go in Clips/<day> (output/replay.py replay_path)
                    self.encoders, canvases, self._recordings_folder() / "Clips", settings.replay_seconds,
                    settings.bitrate_kbps, settings.container, encoder=settings.encoder,
                    short_side=settings.short_side,
                )
            except Exception as exc:
                QMessageBox.critical(self, "SprigCast", f"Could not get clips ready:\n{exc}")
                return
            self.statusBar().showMessage(f"Clips ready: Save clip keeps the last {settings.replay_seconds} s.", 4000)
        elif not on and self.replay is not None:
            self.replay.stop()
            self.replay = None
            self._rest_soon()
        self.live_side.set_clip(self.replay is not None, self.collection.recording.replay_seconds)

    def _save_replay(self) -> list:
        """Save a clip of the last seconds; returns the files (none when clips are off or it failed)."""
        if self.replay is None:
            self.statusBar().showMessage("Press Clip first to get clips ready.", 4000)
            return []
        try:
            paths = self.replay.save()
        except Exception as exc:
            self.statusBar().showMessage(f"Couldn't save the replay: {exc}", 6000)
            return []
        names = ", ".join(p.name for p in paths)
        self.statusBar().showMessage(f"Saved clip: {names}", 8000)
        self._tidy_saves()
        self.live_side.clip_saved(len(paths))
        self.control.replay_saved(paths)
        return paths

    def _stop_recording(self) -> None:
        session, self.recording = self.recording, None
        if session is None:
            return
        self._say("Finishing files…")
        session.stop()
        self.live_side.set_recording("")
        errors = session.errors
        names = ", ".join(p.name for p in session.paths)
        from sprigcast.core.pacing import memory_line

        log.info("recording stopped after %.0f s: %s; %d frames dropped%s; memory: %s", session.elapsed,
                 names or "no files", session.dropped, f"; errors: {errors}" if errors else "", memory_line())
        self._rest_soon()
        if errors:
            self.live_panel.set_recording("")
            QMessageBox.warning(self, "SprigCast", "Recording stopped with an error:\n" + "\n".join(errors))
        else:
            self._say(f"Saved {names}")
            self.live_panel.set_recording("")
            from sprigcast.app.settings_window import remux_choice

            remux, delete = remux_choice()
            mkvs = [p for p in session.paths if p.suffix.lower() == ".mkv"]
            if remux and mkvs:
                from sprigcast.output.naming import remux_later

                remux_later(mkvs, delete)  # the result is logged; the files appear next to the MKVs

    def _refresh_recording_status(self) -> None:
        session = self.recording
        if session.errors:
            self._stop_recording()
            return
        seconds = int(session.elapsed)
        clock = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        codec = codec_short(session.recorders[0][1].codec) if session.recorders else codec_short(pick_codec())
        dropped = session.dropped
        text = f"● REC {clock}  ·  {codec}"
        if dropped:
            text += f"  ·  {dropped} frames dropped (encoder too slow)"
        watch = getattr(self, "_space_watch", None)
        if watch is not None:
            if seconds % 15 == 0 and seconds != getattr(self, "_space_checked", -1):  # a disk check every 15 s
                self._space_checked = seconds
                space = watch.check()
                if space:
                    log.warning("recording: %s", space)
                    self.statusBar().showMessage(space, 20000)
            from sprigcast.output.storage import rec_note

            note = rec_note(watch.left)
            if note:
                text += f"  ·  {note}"
        self.live_panel.set_recording(text)
        self.live_side.set_recording(text)

    # Streaming -----------------------------------------------------------------

    def _refresh_stream_boxes(self) -> None:
        if self.streaming is None:
            self._live_destinations()

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
                                      output.bandwidth_test and service.bandwidth_test, output_id=output.id,
                                      auto_bitrate=output.auto_bitrate)
                if output.enhanced and output.service == "twitch":
                    target = self._enhanced_target(output, target)
                    if target is None:
                        return  # the user chose not to go live
                targets.append(target)
        if problems:
            QMessageBox.warning(self, "SprigCast", "Not ready to go live:\n\n" + "\n".join(problems))
            return
        if not self._doctor_says_go():
            return
        self._say("Connecting…")
        try:
            from sprigcast.app.settings_window import stream_delay

            self.streaming = StreamSession.start(self.engine, targets,
                                                 resolve_codec(self.collection.stream_encoder, streaming=True),
                                                 self.collection.stream_encoder, delay_s=stream_delay())
        except Exception as exc:
            self.streaming = None
            QMessageBox.critical(self, "SprigCast", f"Could not go live:\n{exc}")
            return
        self.live_side.set_live(True)
        from sprigcast.app import transitions_panel

        transitions_panel.set_quiet(True)  # its picture plays only when pointed at while live
        self._cheer_live()
        # Chats still waiting for your stream to start look again once the platforms have noticed it.
        for seconds in (15, 45):
            QTimer.singleShot(seconds * 1000, self.chat_panel.hub.check_now)
        for preview in self.previews.values():
            preview.ring = theme.LIVE
            preview.update()
        from sprigcast.app.logbook import StreamReport

        self._stream_report = StreamReport(self.streaming, self.collection)
        from sprigcast.app.health import SessionHealth

        self._health = SessionHealth(self.streaming)
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
        self._say("Asking Twitch for the Enhanced Broadcasting settings…")
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
            return target if answer == QMessageBox.StandardButton.Yes else None
        if config.warning:
            answer = QMessageBox.question(self, "SprigCast", f"Twitch says:\n\n{config.warning}\n\nGo live anyway?")
            if answer != QMessageBox.StandardButton.Yes:
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

        self._say("Getting your stream key from Twitch…")
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
        self._say("Ending stream…")
        session.stop()
        delayed = max((getattr(st, "delay_s", 0.0) for _, st in session.streamers), default=0.0)
        if delayed > 0:
            self.statusBar().showMessage(f"Stream delay: the last {delayed:g} s still go out to viewers.",
                                         int(delayed * 1000) + 3000)
        from sprigcast.app import transitions_panel

        transitions_panel.set_quiet(False)
        self.live_side.set_live(False)
        for preview in self.previews.values():
            preview.ring = None
            preview.update()
        self._refresh_stream_boxes()
        self.live_panel.set_clock("Offline", False)
        self._set_header_clock("Offline", False)
        self._live_destinations()  # "Offline · N ready to go live"
        seconds = int(session.elapsed)
        lines = [f"Offline. Last stream {seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"]
        lines += [st.summary() for _, st in session.streamers]
        doctor.save_last_streams(QSettings(), session.streamers)
        from sprigcast.app import optimize

        streamers = [st for _, st in session.streamers]
        proven = optimize.proven_kbps(streamers, self.collection.stream_encoder.audio_kbps, session.elapsed)
        optimize.remember_proven(QSettings(), proven)
        if proven is not None:
            log.info("upload: this stream %s", f"proved {proven} kbps fits" if proven else "had network trouble")
        self.statusBar().showMessage(lines[0], 10000)
        if seconds >= 60:  # not for a quick test that never really went out
            self._cheer_goodbye(seconds)
        for line in lines[1:]:
            log.info("stream summary: %s", line)
        from sprigcast.core.pacing import memory_line

        log.info("stream stopped; memory: %s", memory_line())
        self._rest_soon()
        report, self._stream_report = getattr(self, "_stream_report", None), None
        if report is not None:
            report.finish(lines)
        health, self._health = getattr(self, "_health", None), None
        self.live_stage.card.set_notice("", "")
        if health is not None and seconds >= 10:  # not for a start that never got going
            health.finish(QSettings())
        self.live_side.set_health_link(bool(self._health_records()))

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
        self._live_destinations({k: v[0] for k, v in states.items()},
                                {k: v[1].split(" · ")[0] for k, v in states.items()})
        report = getattr(self, "_stream_report", None)
        if report is not None:
            try:
                report.tick(self._last_stats)
            except Exception:
                log.exception("stream report")
        health = getattr(self, "_health", None)
        if health is not None:
            from sprigcast.app.health import notice

            try:
                health.tick()
            except Exception:
                log.exception("stream health")
            self.live_stage.card.set_notice(*reversed(notice(session.streamers)))
        self.live_panel.set_clock(clock, True)
        import re

        sending = [int(m.group(1)) for _level, text in states.values() if (m := re.match(r"(\d+) kbps", text))]
        line = clock.replace("● LIVE", "● Live")
        if sending:
            # How many places are getting the stream, and the upload they add up to.
            line += f"  ·  {len(sending)} of {len(states)} sending  ·  {sum(sending) / 1000:.1f} Mbps up"
        self._set_header_clock(line + ("" if worst == "good" else "  ·  check Destinations"), True)

    # Hotkeys -----------------------------------------------------------------

    def _apply_hotkeys(self) -> None:
        self.hotkeys.apply(self.collection.hotkeys)
        if self.collection.hotkeys and self.hotkeys.mode == "window":
            self.statusBar().showMessage("Global hotkeys aren't available here; they work while SprigCast is focused.", 10000)

    # Settings window -------------------------------------------------------------

    def _open_settings(self, tab: str = "General", output_id: str | None = None) -> None:
        devices = {
            "audio_input": [d.name for d in list_audio_devices(loopback=False)],
            "audio_output": [d.name for d in list_audio_devices(loopback=True, rescan=False)],  # one rescan
        }
        rec = self.collection.recording
        self._clips_before = self._clip_key()
        window = SettingsWindow(
            self.collection,
            default_recordings_folder=self._default_recordings_folder(),
            encoder=codec_label(pick_codec()),
            audio_devices=devices,
            plugin_report=self.plugin_report,
            open_plugins_folder=self._open_plugins_folder if self.plugins_dir is not None else None,
            plugins_dir=self.plugins_dir,
            streaming=self.streaming is not None,
            recording=self.recording is not None,
            control=self.control,
            check_updates=lambda: self.updater.check(manual=True),
            tab=tab,
            parent=self,
        )
        if output_id == "new":
            window.stream_page.add_output()
        elif output_id:
            window.stream_page.select_output(output_id)
        window.applied.connect(self._after_settings)
        window.plugins_changed.connect(self._fill_more_menu)
        # A removed plugin's sources stop now (and an added one's start), not at the next restart.
        window.plugins_changed.connect(lambda: self.engine.send(ReplaceCollection(self.collection.model_copy(deep=True))))
        window.exec()

    def _sync_camera(self) -> None:
        """Turn SprigCast Camera on or off to match Settings (it's only added while SprigCast runs)."""
        from sprigcast.app.output_page import camera_choice, camera_wanted
        from sprigcast.output import virtualcam

        want = camera_wanted() and virtualcam.supported()[0] and virtualcam.installed()
        if want and self.camera is None:
            camera = virtualcam.VirtualCamera(self.engine, lambda: self.collection.canvases, choice=camera_choice,
                                              scenes=lambda: self.collection.scenes)
            if camera.start():
                self.camera = camera
                self._camera_timer.start()
                self._say("SprigCast Camera is on")
            else:
                self._say(camera.error)
                self._camera_error = camera.error
        elif not want and self.camera is not None:
            self._camera_timer.stop()
            self.camera.stop()
            self.camera = None
        if self.camera is not None:
            self._camera_error = ""
        self.live_side.set_camera(self.camera is not None, getattr(self, "_camera_error", ""))

    def _toggle_camera(self) -> None:
        """Live's Virtual camera button: the same switch as Settings → Output → Virtual camera."""
        from PySide6.QtCore import QSettings

        from sprigcast.app.output_page import CAMERA_ON
        from sprigcast.output import virtualcam

        turn_on = self.camera is None
        self._camera_error = self._guide_camera() if turn_on else ""
        QSettings().setValue(CAMERA_ON, turn_on and not self._camera_error)
        self._sync_camera()
        if turn_on and self.camera is None:  # it didn't start: leave the switch off so the button matches
            QSettings().setValue(CAMERA_ON, False)
            self._camera_error = self._camera_error or "The camera didn't start."
            self.live_side.set_camera(False, self._camera_error)
            self._say(self._camera_error)
            if virtualcam.state.privacy_failed:  # Windows said no when it started: show the switch to flip
                self._offer_camera_privacy(virtualcam.privacy_block() or virtualcam.PRIVACY_HINT)

    CAMERA_SETUP_WAIT_S = 30.0

    def _guide_camera(self) -> str:
        """Turning the virtual camera on: set it up once if needed (one Windows prompt), check that Windows'
        camera privacy lets desktop apps in, and say plainly what to do when something is in the way.
        Returns what's wrong ('' when it can start)."""
        import time

        from sprigcast.output import virtualcam

        ok, why = virtualcam.supported()
        if not ok:
            return why
        if not virtualcam.installed() or virtualcam.needs_update():
            fresh = not virtualcam.installed()
            answer = QMessageBox.question(
                self, "SprigCast Camera",
                ("SprigCast Camera needs setting up once so other apps can see it."
                 if fresh else "SprigCast Camera needs a quick update.")
                + "\nWindows will ask for permission.",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Ok:
                return "The camera isn't set up yet." if fresh else ""
            if not virtualcam.set_up():
                return "Setup didn't start. Try again and allow Windows' prompt."
            if fresh:  # the prompt runs on its own: wait for the camera to appear
                end = time.monotonic() + self.CAMERA_SETUP_WAIT_S
                while not virtualcam.installed() and time.monotonic() < end:
                    QApplication.processEvents()
                    time.sleep(0.1)
                if not virtualcam.installed():
                    return "Setup didn't finish. Try again and allow Windows' prompt."
        blocked = virtualcam.privacy_block()
        if blocked:
            self._offer_camera_privacy(blocked)
            return f"{blocked} Turn on camera access for desktop apps, then try again."
        return ""

    def _offer_camera_privacy(self, why: str) -> None:
        from sprigcast.output import virtualcam

        box = QMessageBox(self)
        box.setWindowTitle("SprigCast Camera")
        box.setText(f"{why}\nIn Windows Settings, turn on Camera access and Let desktop apps access your camera.")
        open_button = box.addButton("Open camera settings", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_button:
            virtualcam.open_privacy_settings()

    def _camera_settings(self) -> None:
        """The gear beside Virtual camera: a small pop-up to pick what it shows."""
        from sprigcast.app.camera_popup import camera_popup
        from sprigcast.output import virtualcam

        ok = virtualcam.supported()[0]
        repair = virtualcam.set_up if ok and (not virtualcam.installed() or virtualcam.needs_update()) else None
        status = getattr(self, "_camera_error", "") if self.camera is None else ""
        camera_popup(self.collection, self, repair, status).exec()

    def _after_settings(self) -> None:
        """Settings were applied: save, and bring every dock up to date."""
        clips = getattr(self, "_clips_before", None)
        if clips is not None:
            self._restart_clips_if_changed(clips)
            rec = self.collection.recording
            self._clips_before = self._clip_key()
        self.live_side.set_clip(self.replay is not None, self.collection.recording.replay_seconds)
        from sprigcast.app.theme import apply_theme

        apply_theme(QApplication.instance())
        from sprigcast.app.settings_window import sleep_policy

        self.engine.sleep_policy = sleep_policy()
        self._sync_canvas_choice()  # Settings → Video may have switched a canvas on or off (and sizes changed)
        self._sync_camera()
        self._changed()
        self._refresh_stream_boxes()
        self._updating = True
        self._updating = False
        self._refresh_audio()
        self._apply_hotkeys()
        from sprigcast.app.control_server import apply_settings

        apply_settings(self.control, self)

    REST_AFTER_MS = 10_000

    def _rest_soon(self) -> None:
        """A recording, the stream or clips just stopped: once nothing is left running, give memory back."""
        QTimer.singleShot(self.REST_AFTER_MS, self._rest_if_idle)

    def _rest_if_idle(self) -> None:
        """Nothing is being recorded, streamed or kept for clips: let go of what the show left behind.

        After a 2-hour soak the app sat at ~415 MB at rest against ~200 MB fresh, flat for an hour: pages
        touched during the show (encoders, the clips buffer, frames in flight) and cached plugin art."""
        if self.recording is not None or self.streaming is not None or self.replay is not None:
            return
        from sprigcast.core import painted, pacing

        painted.forget_art()
        pacing.settle()
        trimmed = pacing.trim_working_set()
        log.info("at rest: let go of what the show held%s; memory: %s",
                 " (and handed unused pages back to Windows)" if trimmed else "", pacing.memory_line())

    def _show_render_health(self, event) -> None:
        """One plain word in the status bar (Smooth / Busy / Struggling); the numbers are in its tooltip."""
        health = getattr(self, "_health", None)
        if health is not None:
            health.render(event)
        target = max((c.fps for c in self.collection.canvases), default=60)
        late = max(0, event.dropped_ticks - getattr(self, "_late_before", 0))  # the engine counts them up
        self._late_before = event.dropped_ticks
        budget = 1000 / target
        if event.fps < target * 0.85 or late > target * 0.05:
            word, colour = "Struggling", theme.BAD
        elif event.fps < target * 0.95 or late > 1 or event.render_ms > budget * 0.6:  # one hitch isn't "busy"
            word, colour = "Busy", theme.WARN
        else:
            word, colour = "Smooth", theme.ACCENT
        self.status.setText(f'<span style="color:{colour}">●</span>'
                            f'<span style="color:{theme.MUTED}">&nbsp;&nbsp;{word}</span>')
        self.stats_strip.setToolTip(f"{event.fps:.0f} of {target} frames a second  ·  each frame takes "
                                    f"{event.render_ms:.1f} ms of {budget:.1f}  ·  {late} late this second\n"
                                    "Click for all the numbers (Ctrl+Shift+S)")

    # Command palette -------------------------------------------------------------

    def _palette_commands(self) -> list[Command]:
        """Everything the palette offers: scenes, hotkey actions, modes, menus and the Add source menu."""
        hotkeys = self.collection.hotkeys
        commands: list[Command] = []
        for canvas in self.collection.canvases:
            for scene in self.collection.scenes_for(canvas.id):
                if scene.kind == "show":
                    commands.append(Command(scene.name if canvas.id in scene.name.lower() else f"{scene.name}  ·  {canvas.id}", "Scene",
                                            lambda sid=scene.id: self._switch_to_scene(sid),
                                            hotkeys.get(SCENE_PREFIX + scene.id, ""),
                                            scene.id == self.collection.active_scenes.get(canvas.id)))
        live = self.streaming is not None
        recording = self.recording is not None
        commands += [
            Command("End stream" if live else "Go live", "Action", self._on_live_clicked, hotkeys.get(TOGGLE_STREAM, "")),
            Command("Stop recording" if recording else "Start recording", "Action", self._on_record_clicked,
                    hotkeys.get(TOGGLE_RECORD, "")),
            Command("Clips ready", "Action", lambda: self._toggle_replay(self.replay is None),
                    checked=self.replay is not None),
            Command("Save replay", "Action", self._save_replay, hotkeys.get(SAVE_REPLAY, "")),
            Command("Stream doctor", "Action", self._open_doctor, "Ctrl+Shift+D"),
            Command("Stream health", "Action", lambda: self._open_health()),
        ]
        for part in self.collection.parts():
            if part.states:
                commands.append(Command(f"{part.name}: as built", "Sprig",
                                        lambda pid=part.id: self._set_sprig_state(pid, None), checked=not part.state))
            for state in part.states:
                commands.append(Command(f"{part.name}: {state.name}", "Sprig",
                                        lambda pid=part.id, n=state.name: self._set_sprig_state(pid, n),
                                        hotkeys.get(f"{STATE_PREFIX}{part.id}:{state.name}", ""),
                                        part.state == state.name))
        for mode, action in self.mode_actions.items():
            commands.append(Command(f"Go to {action.text().strip()}", "Mode", action.trigger,
                                    action.shortcut().toString(), self.mode == mode))
        self._fill_add_source_menu()
        commands += self._menu_commands(self.add_source_menu, "Add")
        for menu_action in self.menuBar().actions():
            group = menu_action.text().replace("&", "")
            group = {"Panels": "Panel"}.get(group, group)
            commands += self._menu_commands(menu_action.menu(), group)
        return [c for c in commands if c.title and "Command palette" not in c.title]

    def _menu_commands(self, menu, group: str, prefix: str = "") -> list[Command]:
        out = []
        if menu is None:
            return out
        for action in menu.actions():
            if action.isSeparator() or not action.isVisible():
                continue
            text = prefix + action.text().replace("&", "").rstrip("…").strip()
            if action.menu() is not None:
                out += self._menu_commands(action.menu(), group, text + ": ")
            elif action.isEnabled():
                out.append(Command(text, group, action.trigger, action.shortcut().toString(),
                                   action.isChecked() if action.isCheckable() else None))
        return out

    def use_light_settings(self, why: str = "") -> bool:
        """Offer the light settings (720p, 30 fps, fast encoder). True if they were applied."""
        from sprigcast.app import lowend

        if self.streaming is not None or self.recording is not None or self.replay is not None:
            QMessageBox.information(self, "Light settings", "Stop streaming, recording and the replay buffer first.")
            return False
        cpu = pick_codec() == "libx264"
        lines, fps = lowend.light_changes(self.collection, pending_fps=lowend.waiting_fps(), cpu_encoding=cpu)
        if not lines:
            QMessageBox.information(self, "Light settings", "You're already on light settings.")
            return False
        text = (why + "\n\n" if why else "") + "Light settings keep streaming smooth on a slower PC:\n\n"
        text += "\n".join(f"•  {line}" for line in lines)
        text += "\n\nYou can change any of it back in Settings."
        box = QMessageBox(QMessageBox.Icon.Question, "Light settings", text, parent=self)
        use = box.addButton("Use light settings", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not use:
            return False
        _, fps = lowend.light_changes(self.collection, apply=True, pending_fps=lowend.waiting_fps(), cpu_encoding=cpu)
        lowend.store_fps(fps)
        self.collection.save(self.scenes_path)
        self.statusBar().showMessage(
            "Light settings are on." + (" Restart SprigCast for 30 fps." if fps else ""), 15000)
        return True

    def open_starter(self) -> bool:
        """Starter kit: the usual blooms from your camera, screen and mic. True if it made them."""
        from PySide6.QtWidgets import QDialog

        from sprigcast.app.starter import StarterDialog, build_starter, is_fresh

        both = sum(c.enabled for c in self.collection.canvases) > 1
        mics = [d.name for d in list_audio_devices(loopback=False)]
        speakers = [d.name for d in list_audio_devices(loopback=True, rescan=False)]
        from sprigcast.sources.app_audio import running_apps

        dialog = StarterDialog(list_cameras(), list_monitors(), mics, is_fresh(self.collection), both, self,
                               speakers=speakers, running_apps=running_apps())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        def camera_settings(device) -> dict:
            probe = CameraDialog([device], {"name": device.name, "path": device.path}, self,
                                 max_fps=self._camera_max_fps())
            settings = probe.settings()
            probe.deleteLater()
            return settings

        made = build_starter(self.collection, dialog.choice(camera_settings))
        self._sync_canvas_choice()  # vertical versions may have switched the vertical canvas on
        if not made:
            return False
        self._exit_in_place()
        self.selected_scene_id = next((sid for sid in made
                                       if self.collection.scene(sid).canvas == self.build_canvas), made[0])
        self.last_bloom_id = self.selected_scene_id
        self._changed()
        self._refresh_all()
        self._show_part_editor()
        self.live_panel.show_scenes(self.collection)
        self.statusBar().showMessage(f"Made {len(made)} blooms. Change anything you like; Ctrl+Z undoes it all.",
                                     8000)
        return True

    def open_optimizer(self, first_run: bool = False) -> bool:
        """The Optimize for this PC wizard: check the PC, suggest settings. True if any were applied."""
        from sprigcast.app import lowend
        from PySide6.QtWidgets import QDialog

        from sprigcast.app.wizard import OptimizeWizard

        if self.streaming is not None or self.recording is not None or self.replay is not None:
            QMessageBox.information(self, "Optimize for this PC", "Stop streaming, recording and the replay buffer first.")
            return False
        wizard = OptimizeWizard(self.collection, lowend.waiting_fps(), first_run=first_run, parent=self)
        accepted = wizard.exec() == QDialog.DialogCode.Accepted
        plan = getattr(wizard, "plan", None)
        height, fps_q = getattr(plan, "quality", (0, 0))
        log.info("optimize: %s; quality %dp%d, %d change(s) ticked",
                 "applied" if accepted and wizard.chosen else "closed without changes",
                 height, fps_q, len(wizard.chosen) if accepted else 0)
        if not accepted or not wizard.chosen:
            return False
        fps = wizard.plan.apply(wizard.chosen)
        for change in wizard.chosen:
            log.info("optimize: applied %s (was %s)", change.label, change.now)
        lowend.store_fps(fps)
        self.collection.save(self.scenes_path)
        self._after_settings()
        self.statusBar().showMessage(
            "Optimized for this PC." + (" Restart SprigCast for the new frame rate." if fps else ""), 15000)
        return True

    def _health_records(self) -> list:
        from sprigcast.app import health

        return health.load(QSettings())

    def _open_health(self):
        """How the last few streams went: drops, reconnects, peak memory and hitches."""
        from sprigcast.app.health_page import health_popup

        pop = health_popup(self._health_records(), self, self._open_logs)
        pop.open()
        return pop

    def _open_logs(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from sprigcast.app.logbook import log_dir

        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def first_run_optimizer(self) -> None:
        """Once: offer the Optimize for this PC wizard (it also covers the old light-settings suggestion)."""
        from sprigcast.app import optimize

        settings = QSettings()
        if settings.value(optimize.DONE, False, type=bool):
            return
        settings.setValue(optimize.DONE, True)
        self.open_optimizer(first_run=True)
        from sprigcast.app.starter import is_fresh

        if is_fresh(self.collection):  # only the example blooms: offer to make real ones
            self.open_starter()

    def _open_palette(self) -> CommandPalette:
        palette = CommandPalette(self._palette_commands(), self)
        palette.open()
        return palette

    def _on_hotkey(self, action: str) -> None:
        from sprigcast.app.hotkeys import MUTE_ALL, SOUND_PREFIX, TOGGLE_CLIPS, TOGGLE_MODE

        if action.startswith(("garden:", "path:")):
            self.gardens.hotkey(action)
        elif action == TOGGLE_CLIPS:
            self._toggle_replay(self.replay is None)
        elif action == TOGGLE_MODE:
            self._set_mode("live" if self.mode == "build" else "build")
        elif action == MUTE_ALL:
            sounds = [c for c in self.collection.sources if c.kind in self.AUDIO_KINDS]
            mute = any(not c.settings.get("muted", False) for c in sounds)
            for config in sounds:
                if bool(config.settings.get("muted", False)) != mute:
                    self._on_audio_changed(config.id, {"muted": mute})
        elif action.startswith(SOUND_PREFIX):
            source_id, _, what = action[len(SOUND_PREFIX):].rpartition(":")
            config = next((c for c in self.collection.sources if c.id == source_id), None)
            if config is not None:
                muted = bool(config.settings.get("muted", False))
                want = {"mute": True, "unmute": False}.get(what, not muted)
                if want != muted:
                    self._on_audio_changed(source_id, {"muted": want})
        elif action == SAVE_REPLAY:
            self._save_replay()
        elif action == SCREENSHOT:
            self._screenshot()
        elif action.startswith("plugin:"):
            from sprigcast import plugin_hooks

            plugin_hooks.run_hotkey(self.api, action, self._say)
        elif action.startswith(GAME_PREFIX):
            self._game_grab(action[len(GAME_PREFIX):])
        elif action.startswith(MEDIA_PREFIX):
            source_id, _, what = action[len(MEDIA_PREFIX):].rpartition(":")
            self.media_control(source_id, what)
        elif action == TOGGLE_STREAM:
            self._toggle_streaming()
        elif action == TOGGLE_RECORD:
            self._toggle_recording()
        elif action.startswith(STATE_PREFIX):
            part_id, _, name = action[len(STATE_PREFIX):].partition(":")
            part = next((p for p in self.collection.parts() if p.id == part_id), None)
            if part is not None and (not name or any(s.name == name for s in part.states)):
                self._set_sprig_state(part_id, name or None)
        elif action.startswith(SCENE_PREFIX):
            scene_id = action[len(SCENE_PREFIX):]
            if any(s.id == scene_id for s in self.collection.scenes):
                self.collection.set_active(scene_id)
                self._changed()
                self._refresh_scenes()
                self._refresh_previews()

    # Stats -----------------------------------------------------------------------

    def _has_key(self, ref) -> bool:
        try:
            return bool(ref and get_key(ref))
        except Exception:
            return False

    def _checkup(self) -> doctor.Checkup:
        """What the stream doctor looks at, gathered now."""
        stats = self._last_stats
        return doctor.Checkup(
            collection=self.collection,
            has_key=self._has_key,
            codec=pick_codec(),
            render_fps=stats.fps if stats else None,
            render_ms=stats.render_ms if stats else None,
            gpu_pct=doctor.gpu_load(),
            loudest=self.peak_watch.loudest(),
            statuses=dict(self.source_statuses),
            last_streams=doctor.load_last_streams(QSettings()),
            free_gb=doctor.free_space_gb(self._recordings_folder()),
            recording_planned=bool(self.collection.recording.canvases),
        )

    def _doctor_says_go(self) -> bool:
        """Quick check when going live: only serious problems ask first."""
        checkup = self._checkup()
        checkup.gpu_pct = None  # nvidia-smi can take a moment; the full doctor checks it
        bad = [f for f in doctor.examine(checkup) if f.level == "bad"]
        if not bad:
            return True
        box = QMessageBox(QMessageBox.Icon.Warning, "Stream doctor", "\n\n".join(f.text for f in bad), parent=self)
        go = box.addButton("Go live anyway", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Open doctor", QMessageBox.ButtonRole.HelpRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is go:
            return True
        if box.buttonRole(box.clickedButton()) == QMessageBox.ButtonRole.HelpRole:
            self._open_doctor()
        return False

    def _open_doctor(self) -> None:
        doctor.DoctorDialog(self._checkup, self).exec()

    def _refresh_stats_panel(self) -> None:
        outputs = []
        if self.streaming is not None:
            outputs += [(f"Stream: {st.name}", st) for _, st in self.streaming.streamers]
        if self.recording is not None:
            outputs += [(f"Recording: {canvas_id}", rec) for canvas_id, rec in self.recording.recorders]
        self.stats_panel.refresh(self._last_stats, outputs, self._recordings_folder())
