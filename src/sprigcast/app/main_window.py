"""Main window: the Live and Build screens, their panels (app/panels.py), menus and the stats strip."""

from __future__ import annotations

import html
import logging
import os
import queue
import re
import time
import sys
import threading
import types
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QSettings, QSize, QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QCursor,
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

from sprigcast.app import message as msgbox
from sprigcast.app.modal import modal
from sprigcast.app.open_folder import open_folder
from sprigcast.app.labels import literal
from sprigcast.app import card as popups
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
    PAUSE_RECORD,
    CHAPTER_MARK,
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
from sprigcast.app.library import ROLE_UNUSED
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
from sprigcast.app.terms import COLLECTION, PART, PARTS, SHOW, SHOWS, a, cap
from sprigcast import plugin_hooks
from sprigcast.audio.mixer import planned_tracks
from sprigcast.core.events import (
    AudioLevels,
    EngineError,
    ReplaceCollection,
    SourceInfo,
    Stats,
    SwitchBlooms,
)
from sprigcast.core import awake
from sprigcast.core.capture_sound import CAPTURE_KINDS
from sprigcast.core.edit import cover
from sprigcast.core.scene import (
    FOLDER_COLOURS,
    Output,
    PART_KIND,
    PORTRAIT_TOP,
    Scene,
    SceneCollection,
    SceneItem,
    SourceConfig,
)
from sprigcast.core.source import known_kinds, source_class
from sprigcast.output.keys import get_key, set_key
from sprigcast.output.recorder import FLOW_STALL_S, codec_label, codec_short, pick_codec, resolve_codec
from sprigcast.output.shared import EncoderPool, SharedRecording, SharedReplay
from sprigcast.output.stream import SERVICES, StreamSession, StreamTarget
from sprigcast.render.engine import Engine
from sprigcast.core.device_makers import label as camera_label
from sprigcast.sources.browser import DESKTOP_SOUND
from sprigcast.sources.audio import device_label as sound_label
from sprigcast.sources.audio import list_audio_devices
from sprigcast.sources.camera import is_capture_card, list_cameras
from sprigcast.sources.media import file_filter as media_file_filter, is_sound_file
from sprigcast.sources.win32_enum import list_monitors, list_windows

REACH_EVERY_MS = 300_000  # offline on Live: how often each place's server is tried (output/preflight.py)
LEARNED_AFTER = 5  # sessions before Build's hints stop showing
CARD_ENCODER_FULL, CARD_ENCODER_FULL_S = 85.0, 15.0  # graphics-card encoder this busy this long: warn once
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
from sprigcast.app.sound_card import GROUP_COLOURS, GROUPS, Sound, auto_group, type_of, use_types
from sprigcast.app.names import (ask_name, plain_problem, short_name, short_status, sound_name, sound_name_is_ours,
                                 status_summary, window_name)

log = logging.getLogger(__name__)


# Kinds that have their own button or dock; everything else with a `label` goes under + → Plugins.
BUILT_IN_KINDS = {"display_capture", "window_capture", "camera", "color", "image", "text", "audio_input", "audio_output", "game_capture",
                  "app_audio", "scene", "browser", "chat", "media"}


def _name_from(settings: dict, fallback: str) -> str:
    """A text source is named after its first line (or the file it reads), so the sources list is readable."""
    if "text" in settings and settings.get("read_file", True) and str(settings.get("file", "") or "").strip():
        return Path(str(settings["file"]).strip()).stem or fallback
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


class _Repotted(QObject):
    """Carries the repot result from its thread back to the window."""

    done = Signal(list)


class _ReportSaved(QObject):
    """Carries Export report's outcome (None, or the OSError) from its worker back to the window."""

    done = Signal(object)



class _ResizeSplitter(QSplitter):
    """A splitter that calls on_resize() when its size changes (and on nothing else)."""

    on_resize = None

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        super().resizeEvent(event)
        if self.on_resize is not None:
            self.on_resize()


class GearWatch(QObject):
    """The Settings gear opens Settings when it's pressed, not when the click is let go: on the PC about every
    other click did nothing (2026-11-20: the press landed, the button never saw its release, so no click). The
    press is taken here, so the button itself never half-presses. Keyboard (Space) still clicks it."""

    def __init__(self, button, opener=None):
        super().__init__(button)
        self.opener = opener
        button.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt naming)
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            log.info("settings: gear pressed (%s)", "enabled" if obj.isEnabled() else "disabled")
            if self.opener is not None and obj.isEnabled() and event.button() == Qt.MouseButton.LeftButton:
                QTimer.singleShot(0, self.opener)  # after the press is done with: Settings runs its own loop
                return True
        elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick) and self.opener is not None \
                and event.button() == Qt.MouseButton.LeftButton:
            return True  # the press opened it already
        return False


# + Add's entries (user, 2026-11-20: professional names; one click adds the leaf, double-click picks what it shows).
BLANK_KINDS = {
    "Application Audio": "app_audio", "Audio File": "media:sound", "Audio Input": "audio_input",
    "Audio Output": "audio_output", "Browser": "browser", "Chat": "chat", "Color": "color",
    "Display Capture": "display_capture", "Game Capture": "game_capture", "Image": "image", "Text": "text",
    "Video Capture": "camera", "Video File": "media:video", "Window Capture": "window_capture",
}
def fit_window(rect: tuple[int, int, int, int], free: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """A window's inside (x, y, width, height) made to fit a screen's free area with its frame and title bar:
    no bigger than the area, and wholly on it. Windows refuses a window whose frame is larger than that."""
    side, top, bottom = 8, 32, 8  # the frame Windows draws around the inside
    x, y, width, height = rect
    fx, fy, fw, fh = free
    width = max(200, min(width, fw - side * 2))
    height = max(150, min(height, fh - top - bottom))
    x = min(max(x, fx + side), fx + fw - side - width)
    y = min(max(y, fy + top), fy + fh - bottom - height)
    return x, y, width, height


ADD_ICON_KINDS = {"media:video": "media", "media:sound": "sound_file"}  # + Add's chips for the two file entries
BLANK_TIPS = {
    "Application Audio": "The sound of the apps you pick: games, music, voice chat…",
    "Audio File": "Music or a sound clip from your PC: a leaf that plays while its bloom is on air, with its strip in Sound",
    "Audio Input": "A mic: plays in every bloom unless you pick some",
    "Audio Output": "Everything a speaker or headset plays",
    "Browser": "A web page: alerts, widgets, overlays",
    "Display Capture": "A whole screen",
    "Game Capture": "A game: the full-screen one in front, or a game's window. Its sound comes with it",
    "Mirror": "Show a leaf from another bloom here too. It's the same leaf: change one, both change",
    "Video Capture": "A webcam, capture card or virtual camera",
    "Video File": "A video, with its own sound",
    "Window Capture": "One app's window",
}


def close_reason(user: bool, app=None) -> str:
    """Why the window is closing, for the log: a restart, an installer or Windows signing out, or the window's
    own close (×, Alt+F4, the tray's Quit). StreamBridge 2026-11-20 couldn't tell a deliberate close from a crash."""
    if app is not None and app.property("sprigcast_restart"):
        return "SprigCast restarts (Settings' Restart now)"
    if app is not None and app.property("sprigcast_session_end"):
        return "Windows asked it to close (an installer or update, or signing out / shutting down)"
    if user:
        return "the window was closed (×, Alt+F4 or Quit)"
    return "SprigCast closed itself (Quit, Clear all my data, or an app's request)"


def _show_sprigcast_to_captures(window) -> None:
    """A screen or window capture's Show SprigCast reaches SprigCast's open windows as soon as it's set (and when
    it's taken off or its capture removed, the Privacy setting is back). Checked on every send to the engine."""
    from sprigcast.app import privacy

    if privacy.set_shown_by_capture(privacy.shows_sprigcast(window.collection)) and privacy.supported() \
            and privacy.hidden():
        privacy.apply_to(window)
        window._privacy_to_open_windows()


class MainWindow(QMainWindow):
    _record_finished = Signal(object, bool)  # a recording's files are closed (from the thread that waited)
    _clip_written = Signal(list, list)  # a clip's files are written (files, problems), from the thread that wrote

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
        self._report_future = None  # Export report, while it is being made on a worker
        self.plugin_report = None  # set by main.run() after loading plugins
        self.selected_scene_id: str | None = collection.active_scenes.get(collection.canvases[0].id)
        self.last_bloom_id: str | None = self.selected_scene_id  # where a sprig's ← Back goes
        self.selected_sound_id: str | None = None  # a sound picked in Build's library
        self._undo: list[str] = []  # earlier collections as JSON, oldest first (Edit → Undo, Ctrl+Z)
        self._redo: list[str] = []
        self._undo_current: str | None = collection.to_text()
        self._undo_at = 0.0
        # Hints and tips show for your first few sessions, then get out of the way.
        sessions = QSettings().value("build/sessions", 0, type=int) + 1
        QSettings().setValue("build/sessions", sessions)
        self.learned = sessions > LEARNED_AFTER
        # A part being edited in place inside a show: canvas id, show id, placement source id, layout id.
        self.in_place: tuple[str, str, str, str] | None = None
        self._updating = False  # True while we fill widgets, so their signals do not echo back
        self.source_sizes: dict[str, tuple[int, int]] = {}
        self._start_sizes: dict[str, str] = {}  # new source id -> its layout's id, until it's sized (_place_new_sources)
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
        splitter = _ResizeSplitter(Qt.Orientation.Horizontal)
        for canvas in collection.canvases:
            preview = Preview(canvas.id, canvas.id.capitalize(), (canvas.width, canvas.height))
            preview.output_size = lambda c=canvas.id: self.collection.canvas(c).output_size()
            preview.zoom_locked = QSettings().value("build/zoom_locked", False, type=bool)
            preview.show_zoom_bar = False  # Build's zoom is in the top bar
            preview.zoom_changed.connect(self._zoom_moved)
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
        self.part_preview.zoom_locked = QSettings().value("build/zoom_locked", False, type=bool)
        self.part_preview.show_zoom_bar = False
        self.part_preview.zoom_changed.connect(self._zoom_moved)
        self.part_preview.selected.connect(self._on_preview_selected)
        self.part_preview.edited.connect(self._on_preview_edited)
        self.part_preview.sprig_dropped.connect(self._sprig_dropped)
        self.part_preview.menu_requested.connect(self._on_preview_menu)
        # The sprig's canvas gets the whole card: no header, no note (user, 2026-11-20: "the canvas is crowded").
        # The library opens and closes sprigs; Esc goes back to the bloom you came from.
        self.part_preview.show_title = False
        self.part_preview.back_requested.connect(self._close_part)
        part_panel = QWidget()
        part_layout = QVBoxLayout(part_panel)
        part_layout.setContentsMargins(0, 0, 0, 0)
        part_layout.addWidget(self.part_preview, 1)
        self.splitter = splitter
        splitter.on_resize = self._splitter_resized  # Live re-shares the stage when it changes size
        # Build's shape tabs: one shape at a time, each with its own blooms (they sit at the top of the library).
        self.canvas_tabs = QTabBar()
        self.canvas_tabs.setObjectName("shapeTabs")
        self.canvas_tabs.setDrawBase(False)
        self.canvas_tabs.setExpanding(False)
        self.canvas_tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.canvas_tabs.setStyleSheet(
            "#shapeTabs { background: transparent; }"
            f"#shapeTabs::tab {{ padding: 0px 16px; margin: 4px 2px; height: 30px; border: none; border-radius: 15px;"
            f" background: transparent; color: {theme.MUTED}; font-weight: 600; }}"
            f"#shapeTabs::tab:hover {{ color: {theme.TEXT}; }}"
            f"#shapeTabs::tab:selected {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT}; border: none; }}"
        )
        for canvas in collection.canvases:
            mark = "▯" if canvas.height > canvas.width else "▭"
            self.canvas_tabs.addTab(f"{mark}  {canvas.id.capitalize()}   {canvas.width} × {canvas.height}")
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
        self._apply_preview_settings()
        self._set_live_safe_zones(QSettings().value("live/safe_zones", "Off"))
        self.center = QStackedWidget()
        self.center.addWidget(canvas_page)
        self.center.addWidget(part_panel)
        self.center.currentChanged.connect(lambda _i: self._zoom_moved())
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
        self.build_stage.card_slot.addWidget(self.center)
        from sprigcast.app import canvas_window

        self.canvas_window = None  # Build's canvas in a window of its own (the monitor button in the top bar)
        self.canvas_away = canvas_window.placeholder(self._dock_canvas)  # straight back: no toggle in between
        self.build_stage.card_slot.addWidget(self.canvas_away)
        from sprigcast.app.sound_strips import SoundStrips

        self.sound_strips = SoundStrips(self._on_sound_changed, self._open_build_sound, self._sound_strip_menu,
                                        self._edit_sound_type, self._set_sound_name)
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
        self._last_levels: tuple[float, dict] = (0.0, {})  # (when, {sound id: (peak, rms)}): Settings meters
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
        if self.browser_host is not None:
            self.browser_host.state_of = self._page_state
        self.engine = Engine(collection)
        from sprigcast.app.settings_window import apply_naming, sleep_policy

        self.engine.sleep_policy = sleep_policy()
        self._apply_preview_settings()
        apply_naming()  # file-name patterns from Settings → Recording
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
        from sprigcast.app.hotkeys import PushKey

        self.push_key = PushKey(self.engine.push_key)  # push to talk: pynput's thread straight to the mixer
        self._sync_push_key()
        from sprigcast.app.tray import Tray

        self.tray = Tray(self)  # Settings → General → System tray; the icon only while that is on
        self.tray.sync()
        self.camera = None  # SprigCast Camera (Settings → Video → Virtual camera), see _sync_camera
        self._camera_timer = QTimer(self, interval=1000)
        self._camera_timer.timeout.connect(self._poll_camera)
        self._rest_check = QTimer(self, interval=2000)  # rest mode (app/priority.py): step down when nothing goes out
        self._rest_check.timeout.connect(self._check_rest)
        self._quiet_since: float | None = None
        QTimer.singleShot(20_000, self._rest_check.start)  # after start-up, which wants the boost
        QTimer.singleShot(1500, self._sync_camera)

        self.mode = "build"
        self._load_thumbs()
        self._build_mode_bar()
        self._refresh_all()
        from sprigcast.app import safe_mode

        # Before the saved panels are put back. None in safe mode (a web panel may be what crashed).
        custom_docks.apply(self, [] if safe_mode.on() else custom_docks.load())
        self._restore_layout()
        self._sync_canvas_choice()
        self._show_part_editor()
        from sprigcast.app.control_server import start_for

        self.control = start_for(self)  # Streamer.bot, Stream Deck... (Settings → Connections; off by default)
        from sprigcast.app.updates import Updater

        # New versions from GitHub Releases (app/updates.py); never shown or installed while live or recording.
        self.updater = Updater(self, busy=lambda: self.streaming is not None or self.recording is not None)
        self.updater.start()
        QTimer.singleShot(2500, self._plugin_updates)  # say which store plugins updated at this start
        from sprigcast.app import features

        if self.collection.recording.clips_on_start and features.on("clips"):  # once the renderer has frames
            QTimer.singleShot(1500, self, lambda: self.replay is None and self._toggle_replay(True))
        self._clips_used_at = time.monotonic()
        self._clips_idle_paused = False  # clips paused for being unused: back when a stream or recording starts
        self._clips_timer = QTimer(self)
        self._clips_timer.timeout.connect(self._check_clips_idle)
        self._clips_timer.start(10_000)
        self._clips_follow = QTimer(self)
        self._clips_follow.timeout.connect(self._follow_stream_with_clips)
        self._clips_follow.start(1000)

    # Building the UI ------------------------------------------------------

    def _build_scenes_dock(self) -> None:
        self.scene_tree.dropped.connect(lambda: QTimer.singleShot(0, self._scenes_dropped))
        self.scene_tree.delegate.plus_clicked.connect(self._library_plus)
        self.library.search.textChanged.connect(lambda _text: self._refresh_scenes())
        self.scene_tree.itemSelectionChanged.connect(self._on_scene_selected)
        self._open_on_release = False
        self.scene_tree.let_go.connect(self._list_let_go)
        self.scene_tree.drag_ended.connect(self._list_drag_ended)
        # No double-click to put on air: Build never changes what's on air (user, 2026-11-19). Live does that.
        self.scene_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scene_tree.dot_clicked.connect(self._pick_folder_colour)
        self.scene_tree.customContextMenuRequested.connect(self._scene_context_menu)
        self.scene_tree.itemExpanded.connect(lambda item: self._folder_expanded(item, True))
        self.scene_tree.itemCollapsed.connect(lambda item: self._folder_expanded(item, False))
        add_menu = QMenu(self)
        add_menu.addAction(f"New {SHOW}…").triggered.connect(self._add_scene)
        add_menu.addAction("New folder…").triggered.connect(self._add_folder)
        add_menu.addAction(f"New {PART}…").triggered.connect(self._add_part)
        add_menu.addSeparator()
        add_menu.addAction("Starter kit…").triggered.connect(lambda: QTimer.singleShot(0, self, self.open_starter))
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
        self.link_button.setText("Link")
        self.link_button.setIcon(icons.icon("swap", theme.LABEL, 16))
        self.link_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.link_button.setAutoRaise(True)
        self.link_button.setMinimumSize(30, 28)
        self.link_button.setMenu(self.link_menu)
        self.link_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        bar.layout().insertWidget(bar.layout().count() - 1, self.link_button)
        self.panels.shelve(bar)  # the list itself is Build's left rail; its old buttons stay off screen

    def _fill_add_sound_menu(self, menu: QMenu) -> None:
        """+ beside Sound (under the canvas): A to Z."""
        self._fill_app_sound_menu(menu.addMenu("Application Audio"))  # the names + Add uses (user, 2026-11-20)
        menu.addAction("Audio File…").triggered.connect(lambda: self._add_media(sound=True))
        menu.addAction("Audio Input…").triggered.connect(lambda: self._add_audio("audio_input"))
        menu.addAction("Audio Output…").triggered.connect(lambda: self._add_audio("audio_output"))

    def _sound_strip_menu(self, source_id: str | None, at) -> None:
        """Right-click a sound strip (or the + beside Sound)."""
        menu = QMenu(self)
        if source_id is None:
            self._fill_add_sound_menu(menu)
        else:
            self._fill_sound_menu(menu, source_id)
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(at)
        menu.deleteLater()  # a new menu each time: kept, each held a window handle for good

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
            menu.addSeparator()
            # After the menu has closed: a window opened from inside a closing menu could swallow the click.
            menu.addAction("Starter kit…").triggered.connect(lambda: QTimer.singleShot(0, self, self.open_starter))
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(QCursor.pos())
        menu.deleteLater()  # a new menu each time: kept, each held a window handle for good

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
        from sprigcast.app.accounts_page import open_accounts

        open_accounts(self, "twitch")  # Settings → Stream, on the Twitch place; the Chat panel hears the new account

    def open_stream_details(self) -> None:
        """Stream details: title, category and the rest, saved straight to Twitch, YouTube and Kick."""
        from sprigcast.app.stream_details_dialog import StreamDetailsDialog, places_for

        streams = [o for o in self.collection.outputs if o.type == "stream"]  # in the order you set on Live
        modal(StreamDetailsDialog(self, places_for(streams)))

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
        title = QHBoxLayout()
        title.addWidget(heading("Transitions"), 1)
        gear = QPushButton()
        gear.setIcon(icons.icon("gear", theme.MUTED, 16))
        gear.setStyleSheet(theme.LINK_STYLE)
        gear.setCursor(Qt.CursorShape.PointingHandCursor)
        gear.setToolTip("Direction, edge, wipe image, stinger clips and sound")
        gear.clicked.connect(lambda: self.live_transitions.open_settings())
        title.addWidget(gear)
        card.layout().addLayout(title)
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
        place = settings.value("window/place")  # x, y, width, height of the un-maximized window, then maximized
        if place is not None:
            try:
                x, y, width, height, maximized = (int(v) for v in str(place).split(","))
            except ValueError:
                place = None
        if place is not None:
            # Set here, sized to fit its screen. Qt's restoreGeometry asked Windows for a window whose frame was
            # bigger than the screen's free area when it had been maximized ("Unable to set geometry
            # 1918x1006…" at every start, PC test 2026-11-20).
            screen = QGuiApplication.screenAt(QPoint(x + width // 2, y + height // 2)) or QGuiApplication.primaryScreen()
            if screen is not None:
                x, y, width, height = fit_window((x, y, width, height), screen.availableGeometry().getRect())
            self.setGeometry(x, y, width, height)
            if maximized:
                self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        elif (geometry := settings.value("window/geometry")) is not None:  # saved by an older SprigCast
            self.restoreGeometry(geometry)
            maximized = self.isMaximized()
            normal = self.normalGeometry() if maximized else self.geometry()
            screen = QGuiApplication.screenAt(normal.center()) or QGuiApplication.primaryScreen()
            if normal.isValid() and screen is not None:  # the same fit, before the window is first shown
                self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMaximized)
                self.setGeometry(*fit_window(normal.getRect(), screen.availableGeometry().getRect()))
                if maximized:
                    self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        if settings.value("window/mode", "build") == "live":
            self._set_mode("live")
        self.panels.set_mode(self.mode)
        self.panels.restore_popped()

    def _save_layout(self) -> None:
        settings = QSettings()
        settings.setValue("window/geometry", self.saveGeometry())
        normal = self.normalGeometry() if self.isMaximized() or self.isFullScreen() else self.geometry()
        rect = normal.getRect() if normal.isValid() else tuple(str(settings.value("window/place") or "").split(",")[:4])
        if len(rect) == 4:  # maximized with no un-maximized size known: the last one saved
            settings.setValue("window/place", ",".join(str(v) for v in (*rect, int(self.isMaximized()))))

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
        from sprigcast.app.click_menu import ClickMenu

        self.add_source_menu = ClickMenu(self)  # Plugins and the other submenus open on a click too
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
        self._fill_sprig_picks(menu, parts)
        if menu.actions():
            menu.addSeparator()
        menu.addAction(f"New {PART}…").triggered.connect(lambda: self._add_part())

    def _fill_sprig_picks(self, menu: QMenu, parts: list[Scene]) -> None:
        """The sprigs made for the shape you're editing (user, 2026-11-20: Horizontal lists only horizontal
        sprigs, Vertical only vertical ones), loose ones first, then one submenu per folder, A to Z."""
        sprig = icons.icon("sprig", theme.ACCENT)
        folders = {f.id: f for f in self.collection.part_folders()}
        scene = self._scene()
        canvas_id = scene.canvas if scene is not None else self.build_canvas
        parts = sorted((p for p in parts if any(s.canvas == canvas_id for s in self.collection.shapes(p))),
                       key=lambda p: p.name.lower())
        for part in parts:
            if part.folder not in folders:
                menu.addAction(sprig, literal(part.name)).triggered.connect(
                    lambda _=False, pid=part.id: self._add_part_source(pid))
        for folder in sorted(folders.values(), key=lambda f: f.name.lower()):
            inside = [p for p in parts if p.folder == folder.id]
            if not inside:
                continue
            sub = menu.addMenu(_dot(folder.colour, 12), literal(folder.name))
            for part in inside:
                sub.addAction(sprig, literal(part.name)).triggered.connect(
                    lambda _=False, pid=part.id: self._add_part_source(pid))

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
        self._fill_sprig_picks(parts_menu, parts)
        if not parts_menu.actions():
            parts_menu.addAction(f"No {PARTS} yet: + beside {cap(PARTS)} on the left").setEnabled(False)
        menu.addSeparator()

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

        # A to Z (sprigs stay on top: they're SprigCast's own). Each kind opens to New… (adds the leaf and opens
        # its picker straight away) and the leaves of that kind you already have (user, 2026-11-20, from a
        # video of the menu they liked). Sounds list what's in Sound; picking one opens its controls.
        from sprigcast.app.build_stage import kind_icon

        # Sounds are not here (user, 2026-11-20): the + on the Sound card adds them, and a sound's inspector
        # changes its mic, device, apps or file.
        entries = [(label, kind) for label, kind in BLANK_KINDS.items()
                   if kind not in self.AUDIO_KINDS and kind != "media:sound"] + [("Mirror", None)]
        for label, kind in sorted(entries, key=lambda e: e[0].lower()):
            sub = menu.addMenu(label)
            sub.setToolTipsVisible(True)
            sub.menuAction().setToolTip(BLANK_TIPS.get(label, ""))
            if kind is None:
                sub.setIcon(icons.icon("link", theme.TEXT, 16))
                sub.aboutToShow.connect(lambda s=sub: (s.clear(), mirrors(s)))
            else:
                sub.setIcon(kind_icon(ADD_ICON_KINDS.get(kind, kind)))
                sub.aboutToShow.connect(lambda s=sub, k=kind, n=label: (s.clear(), self._fill_add_kind_menu(s, k, n)))
        menu.setToolTipsVisible(True)
        menu.addSeparator()
        plugins = menu.addMenu(icons.icon("plug", theme.TEXT, 16), "Plugins")
        self.more_menu = plugins
        plugins.aboutToShow.connect(self._fill_more_menu)  # store plugins can arrive while SprigCast runs
        self._fill_more_menu()

    def _source_context_menu(self, pos) -> None:
        on_item = self.source_list.itemAt(pos) is not None
        menu = self._source_menu(on_item)
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(self.source_list.viewport().mapToGlobal(pos))
        menu.deleteLater()  # a new menu each right-click: kept, each one held a window handle for good

    def _on_preview_menu(self, canvas_id: str, index: int, where) -> None:
        menu = self._preview_menu(canvas_id, index)
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(where)
        menu.deleteLater()

    def _preview_menu(self, canvas_id: str, index: int) -> QMenu:
        """Right-click in a preview: the same menu as the Sources list for what's under the mouse.
        In Live (nothing to edit) it offers Projector and this canvas's settings."""
        part = canvas_id.startswith("part:")
        if part and index < 0:  # an open sprig's empty space: just Snapping
            menu = QMenu(self)
            self._snap_menu(menu)
            return menu
        if self.mode == "live" and canvas_id in self.previews:
            menu = QMenu(self)
        else:  # a leaf in an open sprig gets the same menu as one in a bloom (PC test, 2026-11-20)
            menu = self._source_menu(index >= 0 and self._item() is not None)
        if index < 0:
            if menu.actions():
                menu.addSeparator()
            if self.mode == "live":
                self._live_canvas_menu(menu, canvas_id)
                return menu
            self._fill_projector_menu(self._pop_out_menu(menu), canvas_id)
            if self.mode != "live":
                self._snap_menu(menu)
                menu.addAction("Canvas size and frame rate…").triggered.connect(lambda: self._open_settings("Video"))
        return menu

    SNAP_DISTANCES = (5, 10, 20, 40)

    def _snap_menu(self, menu: QMenu) -> QMenu:
        """Build's right-click → Snapping (it used to be five rows in Settings → General)."""
        from sprigcast.app.preview import snap_settings

        now = snap_settings()
        snap = menu.addMenu("Snapping")
        snap.setToolTipsVisible(True)

        def tick(label: str, key: str, on: bool, tip: str = "") -> None:
            act = snap.addAction(label)
            act.setCheckable(True)
            act.setChecked(on)
            if tip:
                act.setToolTip(tip)
            act.toggled.connect(lambda checked, k=key: QSettings().setValue(k, checked))

        tick("Snap", "editing/snap", now["enabled"], "Hold Ctrl while dragging to place a source without snapping")
        snap.addSeparator()
        tick("To canvas edges", "editing/snap_edges", now["edges"])
        tick("To other sources", "editing/snap_sources", now["sources"])
        tick("To the center", "editing/snap_center", now["center"])
        snap.addSeparator()
        reach = snap.addMenu("Sensitivity")
        for px in self.SNAP_DISTANCES:
            act = reach.addAction(f"{px} px")
            act.setCheckable(True)
            act.setChecked(abs(now["distance"] - px) < 0.5)
            act.triggered.connect(lambda _=False, v=px: QSettings().setValue("editing/snap_distance", float(v)))
        for act in snap.actions()[1:]:
            act.setEnabled(now["enabled"])
        return snap

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
            menu.addAction("Close projectors").triggered.connect(
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
            lambda: self.open_hotkeys())

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
        from sprigcast.core import workers

        folder = self._recordings_folder() / "Screenshots"

        def take():  # on a worker, the window drawing meanwhile: waiting for the frame and the PNG took it 50-200 ms
            image = screenshot.grab(self.engine, ["view:screenshot"]).get("view:screenshot")
            return None if image is None else screenshot.save(image, folder, scene.name)

        self.engine.set_view("screenshot", scene.canvas, "bloom", scene_id)
        try:
            path = workers.wait_answering(take)
        finally:
            self.engine.set_view("screenshot", None)
        if path is None:
            self._say("Couldn't take a screenshot: nothing is drawing.")
            return
        self._say(f"Screenshot saved to {path}")
        self._tidy_saves()

    def _rename_bloom(self, scene_id: str) -> None:
        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return
        name, ok = popups.ask_text(self, "Rename", f"{cap(SHOW)} name", scene.name, "Rename")
        if ok and name.strip() and name.strip() != scene.name:
            scene.name = name.strip()
            self._changed()
            self._refresh_all()

    def _pop_out_menu(self, menu: QMenu) -> QMenu:
        """ "Projector" (user, 2026-11-20: one professional word, not "Pop out"): show it big on another screen,
        or in its own window."""
        sub = menu.addMenu(icons.icon("monitor", theme.MUTED), "Projector")
        sub.menuAction().setToolTip("Show it on its own: full screen on a monitor, or in a window")
        return sub

    def _projector_menu(self, menu: QMenu, kind: str, ref: str) -> None:
        """Add "Projector" for a bloom, sprig or source."""
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
        QTimer.singleShot(0, self, self._pace_poll)  # once it's shown: a projector wants every picture
        return projector

    def _projector_closed(self, projector) -> None:
        if projector in self.projectors:
            self.projectors.remove(projector)
        if projector.view is not None:
            self.engine.remove_frame_tap(f"view:{projector.view}", projector.tap)
            self.engine.set_view(projector.view, None)
        self._pace_poll()

    def _fill_capture_sound_menu(self, menu: QMenu, capture_id: str) -> None:
        """A capture's own sound, like OBS's Capture audio: none, the app's own, an output such as Elgato Wave
        Link's Game channel, or (a video capture) an input such as a capture card's own sound."""
        from sprigcast.core.capture_sound import APP, INPUT, sound_choice
        from sprigcast.sources.audio import device_label

        current = sound_choice(self.collection, capture_id)
        camera = self.collection.source(capture_id).kind == "camera"
        group = QActionGroup(menu)
        rows: list[tuple[str | None, str]] = [(None, "No sound of its own")]
        if not camera:
            rows.append((APP, "The app's own sound"))
        lists = []
        if camera:  # a capture card's sound is an input of its own
            inputs = [INPUT + d.name for d in list_audio_devices(loopback=False, rescan=False)]
            lists.append(("From an input", inputs))
        lists.append(("From an output", [d.name for d in list_audio_devices(loopback=True, rescan=False)]))
        if current not in (None, APP) and all(current not in names for _, names in lists):
            lists[0 if current.startswith(INPUT) else -1][1].append(current)  # unplugged now: still shown, ticked
        for value, text in rows:
            self._capture_sound_row(menu, group, capture_id, value, text, current)
        for title, names in lists:
            menu.addSeparator()
            note = menu.addAction(title)
            note.setEnabled(False)
            for value in names:
                name = value[len(INPUT):] if value.startswith(INPUT) else value
                self._capture_sound_row(menu, group, capture_id, value, device_label(name), current)

    def _capture_sound_row(self, menu, group, capture_id: str, value, text: str, current) -> None:
        act = menu.addAction(text)
        act.setCheckable(True)
        act.setChecked(value == current)
        group.addAction(act)
        act.triggered.connect(lambda _=False, v=value: self._set_capture_sound(capture_id, v))

    def _pair_capture_card_sound(self, config) -> None:
        """A capture card (Elgato HD60 X...) lists its sound as an input of its own: a new video capture takes it
        along when one fits its name, the way OBS's device audio does. Right-click → Sound changes it."""
        from sprigcast.core.capture_sound import INPUT, capture_sound, paired_input

        if config.id not in {s.id for s in self.collection.sources} or capture_sound(self.collection, config.id):
            return
        inputs = [d.name for d in list_audio_devices(loopback=False, rescan=False)]
        found = paired_input(str(config.settings.get("name") or config.name), inputs)
        if found:
            self._set_capture_sound(config.id, INPUT + found)

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
            if item is not None and self.collection.source(item.source).kind in CAPTURE_KINDS:
                self._fill_capture_sound_menu(menu.addMenu("Sound"), item.source)
            if item is not None and self.collection.source(item.source).kind == "camera":
                self._fill_swap_camera_menu(menu.addMenu("Swap camera"), item.source)
            if item is not None and self.collection.source(item.source).kind == "browser" and self.browser_host:
                menu.addAction("Refresh page").triggered.connect(
                    lambda _=False, sid=item.source: self.browser_host.reload(sid))
                menu.addAction("Interact…").triggered.connect(
                    lambda _=False, sid=item.source: self._interact(sid))
            if item is not None and self.collection.source(item.source).kind == "media":
                self._fill_media_menu(menu, item.source)
            # By how often it's used (user, 2026-11-20): the item's own settings first, then Transform and
            # Order, then looks, then the rarer moves, and Remove last.
            if menu.actions():
                menu.addSeparator()
            acts = self.item_actions
            for key in ("settings", "filters", "rename"):
                menu.addAction(acts[key])
            menu.addSeparator()
            transform = menu.addMenu("Transform")
            for key in ("fit", "fill", "stretch", "center"):
                transform.addAction(acts[key])
            transform.addSeparator()
            for key in ("reset", "uncrop"):
                transform.addAction(acts[key])
            transform.addSeparator()
            hint = transform.addAction("Crop: hold Alt and drag a side")
            hint.setEnabled(False)
            order = menu.addMenu("Order")
            for key in ("front", "up", "down", "back"):
                order.addAction(acts[key])
            acts["hide"].setText("Show" if item is not None and not item.visible else "Hide")
            acts["lock"].setCheckable(True)
            acts["lock"].setChecked(bool(item and item.locked))
            menu.addAction(acts["hide"])
            menu.addAction(acts["lock"])
            visual = item is not None and self.collection.source(item.source).kind not in self.AUDIO_KINDS
            # No filters or effects listed here (Shape, Color look, Pixelate, Black out, Freeze...): Filters…
            # opens them all (user, 2026-11-20). A plugin's own actions on its source (Add a minute...) stay.
            if item is not None:
                self._fill_plugin_menu(menu, item.source)
            menu.addSeparator()
            count = len(self.source_list.selectedItems())
            turn = menu.addAction(f"Group into {a(PART)}…" if count < 2 else f"Group these {count} into {a(PART)}…")
            turn.setToolTip(f"Move the selected sources into a new reusable {PART}, right where they are")
            turn.triggered.connect(self._turn_into_part)
            if visual:
                if self.collection.source(item.source).kind == PART_KIND:
                    part = self.collection.source(item.source).settings.get("scene", "")
                    self._projector_menu(menu, "bloom", part)
                else:
                    self._projector_menu(menu, "source", item.source)
            menu.addSeparator()
            menu.addAction(acts["remove"])
        return menu

    def _scene_context_menu(self, pos) -> None:
        menu = QMenu(self)
        under = self.scene_tree.itemAt(pos)
        folder_id = under.data(0, ROLE_FOLDER) if under is not None else None
        if folder_id:
            self._folder_menu(menu, self.collection.folder(folder_id))
            theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
            menu.exec(self.scene_tree.viewport().mapToGlobal(pos))
            menu.deleteLater()
            return
        if under is not None and under.data(0, ROLE_ROW) == "sound":
            self._fill_sound_menu(menu, under.data(0, ROLE_ID))
            theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
            menu.exec(self.scene_tree.viewport().mapToGlobal(pos))
            menu.deleteLater()
            return
        if under is not None and self._scene() is not None:
            scene = self._scene()
            if scene.kind == "show":
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
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(self.scene_tree.viewport().mapToGlobal(pos))
        menu.deleteLater()

    # Sounds in Build's library --------------------------------------------------------

    def _fill_sound_menu(self, menu: QMenu, source_id: str) -> None:
        """A sound's right-click menu: its own short list, not the bloom one."""
        config = next((c for c in self.collection.sources if c.id == source_id), None)
        if config is None:
            return
        menu.setToolTipsVisible(True)
        # Windows open once the menu has closed: one opened inside the menu's own loop showed Windows' UI Automation
        # an empty pane (PC 2026-11-21: the Filters window had no parts, and + Add filter listed nothing).
        later = lambda f: QTimer.singleShot(0, self, f)  # noqa: E731
        menu.addAction("Rename…").triggered.connect(lambda _=False: later(lambda: self._rename_sound(source_id)))
        muted = bool(config.settings.get("muted", False))
        menu.addAction("Unmute" if muted else "Mute").triggered.connect(
            lambda _=False: self._build_tune_changed(source_id, {"muted": not muted}))
        self._fill_plays_in_menu(menu.addMenu("Plays in"), source_id)
        self._fill_type_menu(menu.addMenu("Type"), source_id)
        menu.addSeparator()
        # Opens the sound's page on the right. Left out while that page is already open (user 2026-11-20: picked
        # then, it seemed to do nothing).
        if not (self.selected_sound_id == source_id and self.build_stage.right_stack.currentIndex() == 1):
            menu.addAction("Volume and sound…").triggered.connect(lambda _=False: self._select_sound(source_id))
        menu.addAction("Filters…").triggered.connect(
            lambda _=False: later(lambda: self._open_filters(source_id, "audio")))
        menu.addAction("Tracks and sync…").triggered.connect(
            lambda _=False: later(lambda: self._open_settings("Audio/Sounds")))
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

    def _fill_type_menu(self, menu: QMenu, source_id: str) -> None:
        """Put a sound in one of your types (the Sound card's groups and Build's Type filter), or a new one."""
        now = self._sound_item(self.collection.source(source_id)).group
        for group in GROUPS:
            action = menu.addAction(group)
            action.setCheckable(True)
            action.setChecked(group == now)
            action.triggered.connect(lambda _=False, g=group: self._build_tune_changed(source_id, {"group": g}))
        menu.addSeparator()
        # After the menu has closed: a window opened from inside a closing menu could swallow the click.
        menu.addAction("New type…").triggered.connect(
            lambda _=False: QTimer.singleShot(0, self, lambda: self._edit_sound_type(None, source_id)))

    def _edit_sound_type(self, name: str | None, ticked: str | None = None) -> None:
        """New type (name None) or Edit <name>: its name, color and the sounds in it. Every sound's type is
        saved by name afterwards, so a renamed or removed type takes its sounds along."""
        from sprigcast.app.sound_type_dialog import SoundTypeDialog

        audio = self.collection.audio
        use_types(audio.sound_types)
        sounds = sorted((s for s in self.collection.sources if self._has_sound(s)), key=lambda s: s.name.lower())
        now = {s.id: self._sound_item(s).group for s in sounds}
        homes = {s.id: "Browser" if s.kind == "browser" else auto_group(s.name, s.kind) for s in sounds}
        dialog = SoundTypeDialog(audio.sound_types, [(s.id, s.name, now[s.id]) for s in sounds], name, self,
                                 homes=homes)
        if ticked in dialog.ticks:
            dialog.ticks[ticked].setChecked(True)
        if not modal(dialog):
            return
        types, moves = dialog.result_types()
        audio.sound_types = types
        use_types(types)
        for source in sounds:
            source.settings = {**source.settings, "group": moves.get(source.id, now[source.id])}
        strips = getattr(self, "sound_strips", None)
        if strips is not None and name and strips.type_shown == name:
            strips._pick_type("" if dialog.removed else dialog.chosen_name())  # follow the rename
        self._changed()
        self._refresh_audio()
        if self.selected_sound_id and self.build_stage.right_stack.currentIndex() == 1:
            self._open_build_sound(self.selected_sound_id)  # its Type pill lists the new types

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
        name, ok = popups.ask_text(self, "Rename sound", "Name", config.name, "Rename")
        if ok:
            self._set_sound_name(source_id, name)

    def _set_sound_name(self, source_id: str, name: str) -> None:
        """A sound's new name (Rename… or double-clicking its strip's name). Only the sound changes: its id stays,
        so apps that use it through the Control API by id keep working."""
        config = self.collection.source(source_id)
        if config is not None and name.strip() and name.strip() != config.name:
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
        self.collection.remove_source_if_unused(source_id, sounds_too=True)
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
        if sprigs:  # user, 2026-11-20: "organize your sprig folders in alphabetic order either a-z or z-a"
            menu.addSeparator()
            menu.addAction("Sort A–Z").triggered.connect(lambda: self._sort_folder(folder.id, False))
            menu.addAction("Sort Z–A").triggered.connect(lambda: self._sort_folder(folder.id, True))
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
        name, ok = popups.ask_text(self, title, "Folder name", primary="Add", placeholder=example)
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
        name, ok = popups.ask_text(self, "Rename folder", "Folder name", folder.name, "Rename")
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
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(at)
        menu.deleteLater()  # a new menu each time: kept, each held a window handle for good

    def _sort_folder(self, folder_id: str, backwards: bool = False) -> None:
        """A folder's contents in name order (A–Z, or Z–A). They keep the places they held among all scenes, so
        other lists stay put. Numbers count as numbers: "Chat 2" comes before "Chat 10"."""
        import re

        def key(scene) -> list:
            return [(0, int(part), "") if part.isdigit() else (1, 0, part)
                    for part in re.split(r"(\d+)", scene.name.casefold()) if part]

        scenes = self.collection.scenes
        slots = [i for i, sc in enumerate(scenes) if sc.folder == folder_id]
        ordered = sorted((scenes[i] for i in slots), key=key, reverse=backwards)
        if [scenes[i].id for i in slots] == [sc.id for sc in ordered]:
            return  # in that order already: nothing to save or undo
        for slot, scene in zip(slots, ordered, strict=True):
            scenes[slot] = scene
        self._changed()
        self._refresh_scenes()

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

    def _reorder_blooms(self, ids: list[str]) -> None:
        """Live's Edit: one list's blooms in a new order. They keep the places they held among all scenes,
        so other lists stay put; what's on air doesn't change, and 1-9 follow the new order."""
        scenes = self.collection.scenes
        slots = sorted(i for i, s in enumerate(scenes) if s.id in ids)
        if len(slots) != len(ids):
            return  # a bloom went meanwhile
        by_id = {s.id: s for s in scenes}
        for slot, scene_id in zip(slots, ids):
            scenes[slot] = by_id[scene_id]
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

        # What the library listed: this shape's blooms and sprigs only (#623). Checked against everything, a
        # sprig or sprig folder of the other shape made every drop look wrong and it was put back (PC, 2026-11-20).
        bloom_groups = self.collection.grouped_scenes(self.build_canvas)
        sprig_groups = self.collection.grouped_parts_on(self.build_canvas)
        shows = [s for _f, group in bloom_groups for s in group]
        parts = [p for _f, group in sprig_groups for p in group]
        show_folders = [f.id for f, _g in bloom_groups if f is not None]
        part_folders = [f.id for f, _g in sprig_groups if f is not None]
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
        name, ok = popups.ask_text(self, "Rename source", "Name", config.name, "Rename")
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
                                   self._close_build_sound, self._change_sound_source,
                                   on_blooms=lambda _sid: self._plays_in_popup())
        # In a scroll area: the titled sections are taller than a short window (a 768 px laptop screen).
        tune_scroll = QScrollArea()
        tune_scroll.setWidgetResizable(True)
        tune_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tune_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tune_scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        tune_holder = QWidget()
        tune_col = QVBoxLayout(tune_holder)
        tune_col.setContentsMargins(0, 0, 6, 0)  # room for the scroll bar
        tune_col.addWidget(self.build_tune)
        tune_col.addStretch(1)
        tune_scroll.setWidget(tune_holder)
        self.build_tune_scroll = tune_scroll
        self.build_stage.sound_slot.addWidget(tune_scroll, 1)
        right = self.build_stage.right
        panel = QWidget()
        panel.setStyleSheet(theme.SOFT_FIELD_STYLE)
        col = QVBoxLayout(panel)
        col.setContentsMargins(6, 0, 16, 8)  # breathing room from the window edge
        col.setSpacing(10)

        self.prop_sections: list[QWidget] = []  # hidden while nothing is selected
        self.prop_section_toggles: dict[str, tuple[QPushButton, QWidget]] = {}

        from sprigcast.app.inspector_fields import Card, NumberField, link_button

        from sprigcast.app.inspector_fields import InspectorTabs

        # One page now: Transform (position, size, turn, bounds, crop). Look, Shapes and Device went (user,
        # 2026-11-20): Filters… holds the looks and shapes, Properties… the device.
        self.inspector_tabs = InspectorTabs(("Transform",))
        self.prop_sections.append(self.inspector_tabs)
        tab_of = {"Place": "Transform", "Bounds": "Transform", "Crop": "Transform"}
        self._prop_spare = QWidget(panel)  # the old tabs' buttons, kept for their shortcuts, never shown
        self._prop_spare.hide()

        def section(title: str) -> QVBoxLayout:
            """A group on the inspector's page: Transform holds the place, Bounds and Crop."""
            tab = tab_of.get(title)
            if tab is None:
                return QVBoxLayout(QWidget(self._prop_spare))
            body = self.inspector_tabs.group(tab, None if title == "Place" else title)
            self.prop_section_toggles[title] = (self.inspector_tabs.buttons[tab], body)
            return body.layout()

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
        col.addWidget(self.inspector_tabs)

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
        chip_style = theme.button_style(16, "8px 14px")
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
        for chip in (self.prop_fit, self.prop_uncrop):
            chips.addWidget(chip)
        chips.addStretch(1)
        look.addLayout(chips)
        # Filters (looks, shapes, keys...) is one button under the name, the way in to every filter.
        top.addWidget(self.prop_filters, 0, Qt.AlignmentFlag.AlignLeft)
        device = section("Device")
        device_row = QHBoxLayout()
        device_row.addWidget(self.prop_extra)
        device_row.addStretch(1)
        device.addLayout(device_row)

        col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setWidget(panel)
        right.addWidget(scroll, 1)

    def _fill_bloom_tools(self) -> None:
        """Above the canvas, on the right: Link (vertical blooms) and Safe zones. No Put on air: what's on air
        is picked on Live, so editing in Build while streaming never changes the show (user, 2026-11-19)."""
        style = theme.SMALL_PILL_STYLE
        for button in (self.link_button, self.safe_zone_button):
            button.setStyleSheet(style)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.build_bar.tools.addWidget(button)

    def _edit_destination(self, output_id: str | None) -> None:
        """Open Settings → Stream on a destination. "new:<service>" (Live's + Add menu) adds one first."""
        if output_id == "details":  # Live's Destinations card: title, category and the rest
            self.open_stream_details()
            return
        added = None
        if output_id and output_id.startswith("new:"):
            added = output_id = self._new_destination(output_id.split(":", 1)[1])
            if output_id is None:
                return  # ready to go: a Twitch account gives the key
        if output_id is None:
            first = next((o.id for o in self.collection.outputs if o.type == "stream"), None)
            output_id = first or "new"
        saved = self._open_settings("Stream", output_id=output_id)
        if added is not None and saved is not None:
            self._settle_new_place(added, saved)

    def _settle_new_place(self, output_id: str, saved: bool) -> None:
        """A place just made by + Add, once Settings has closed on it. Closed without saving: it was never
        wanted, so it goes (it stayed in the list, switched on with no key; PC, 2026-11-20). Saved without
        its server or key: it stays, switched off until it has them."""
        from sprigcast.app.destinations import problem

        output = next((o for o in self.collection.outputs if o.id == output_id), None)
        if output is None:
            return
        if not saved:
            if self.streaming is not None:
                return
            log.info("places: %s (%s) added and Settings closed without saving, removed", output.id, output.service)
            self._place_action(output_id, "remove")
            self._keep_path_file()
            return
        why = problem(output, self.collection, self._has_key)
        if output.enabled and why and why.startswith("Needs"):
            log.info("places: %s (%s) saved switched off: %s", output.id, output.service, why)
            output.enabled = False
            self.collection.save(self.scenes_path)
            self._refresh_stream_boxes()
            self._keep_path_file()

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
        """Live's Edit mode: remove a place, swap its canvas, or flip Twitch's bandwidth test; and the order."""
        output = next((o for o in self.collection.outputs if o.id == output_id), None)
        if output is None:
            return
        if action in ("up", "down") or action.startswith("to:"):  # only the list's order: fine while live too
            self._order_place(output, action)
            return
        if self.streaming is not None:
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

    def _order_place(self, output, action: str) -> None:
        """Move a place up, down or to a spot in the list. Its order is the outputs' order, so Settings,
        Stream details and the control API's places.list all show it the same way."""
        outputs = self.collection.outputs
        slots = [i for i, o in enumerate(outputs) if o.type == "stream"]
        streams = [outputs[i] for i in slots]
        at = streams.index(output)
        if action == "up":
            to = at - 1
        elif action == "down":
            to = at + 1
        else:
            try:
                to = int(action[3:])
            except ValueError:
                return
        to = max(0, min(len(streams) - 1, to))
        if to == at:
            return
        streams.insert(to, streams.pop(at))
        for i, o in zip(slots, streams):
            outputs[i] = o
        self.collection.save(self.scenes_path)
        self._live_destinations()  # now, live or not: only the order changed

    def _build_live_stage(self) -> None:
        """Live mode's own screen (app/live_stage.py): no docks, the stage, bloom tiles and a side column."""
        self.live_panel = LivePanel(self._switch_to_scene, self._set_sprig_state, on_edit=self._edit_in_build)
        self.live_panel.on_peek = self._peek_bloom
        self.live_panel.on_project = self._live_bloom_menu
        self.live_panel.on_reorder = self._reorder_blooms
        self._build_transition_menu()
        self.live_side = LiveSide(self._on_live_clicked, self._on_record_clicked, self._on_clip_clicked,
                                  self._edit_destination, self._on_stream_toggled, self._place_action,
                                  lambda: self._open_settings(), self._on_sound_changed,
                                  lambda sid: self._open_filters(sid, "audio"), self._set_mic_first,
                                  self._toggle_camera, self._camera_settings)
        self.live_side.on_pause = lambda: self.pause_recording(None)
        self.live_side.sound.on_settings = lambda: self._open_settings("Audio/Sounds")
        self.live_side.on_health = lambda: self._open_health()
        self._show_last_stream()
        self.live_side.on_screenshot = lambda: self._screenshot(background=True)
        self.live_side.on_view = self._set_live_view
        self.live_side.on_unreachable = self._open_doctor
        # Offline on Live: each place's server is tried now and then (output/preflight.py; no key is used), so a
        # place that won't connect is named beside Go live before it's pressed. Never while live or hidden.
        self._unreachable: dict[str, str] = {}  # output id -> why its server wouldn't connect
        self._unreachable_lines: list[str] = []
        self._reach_job = None
        self._reach_key: tuple = ()
        self._reach_timer = QTimer(self)
        self._reach_timer.setInterval(REACH_EVERY_MS)
        self._reach_timer.timeout.connect(self._check_reach)
        self._reach_timer.start()
        self._reach_poll = QTimer(self)
        self._reach_poll.setInterval(100)
        self._reach_poll.timeout.connect(self._reach_done)
        self._apply_features()  # Settings → General → Features: hide what's switched off
        self.live_side.garden.clicked.connect(lambda: self._open_garden_check())
        self.live_stage = LiveStage(self.live_side)
        self.live_tiles = QScrollArea()
        self.live_tiles.setWidgetResizable(True)
        self.live_tiles.setFrameShape(QFrame.Shape.NoFrame)
        self.live_tiles.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.live_tiles.setStyleSheet("QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.live_tiles.setWidget(self.live_panel)
        self.live_stage.tiles_slot.addWidget(self.live_tiles, 1)
        self.live_panel.transition_chip.hide()  # the Transitions card beside the blooms shows it now
        self._build_live_transitions()
        self.modes.addWidget(self.live_stage)  # page 1
        self._thumbs_at = 0.0
        self._thumb_seen: dict[str, str | None] = {}  # Build: the bloom each preview showed at the last look
        self.live_side.set_clip(False, self.collection.recording.replay_seconds)

    def _splitter_resized(self) -> None:
        # (A resizeEvent, not an event filter on the window: the filter saw every event of the splitter and the
        # tiles' viewport through Python, 69 ms of a Garden check's open on the PC while live, 2026-11-18.)
        if self.mode == "live":
            QTimer.singleShot(0, self, self._share_stage)

    def _fit_tiles(self) -> None:
        """Nothing to fit any more (kept for older callers)."""
        # The blooms now sit in their own card of fixed height under the canvas and scroll inside it.

    def _pop_out_canvas(self) -> None:
        """Build's canvas goes to a window of its own; the library, Leaves and the inspector stay here."""
        if self.canvas_window is not None:
            return
        from sprigcast.app.canvas_window import CanvasWindow

        shortcuts = [a for a in self.findChildren(QAction) if not a.shortcut().isEmpty()]
        self.build_stage.card_slot.removeWidget(self.center)
        self.canvas_window = CanvasWindow(self.center, shortcuts)
        self.canvas_window.closed.connect(self._dock_canvas)
        self.canvas_away.show()
        if self.mode == "build":
            self.canvas_window.show()
        self._show_canvas_out(True)
        log.info("build canvas popped out")

    def _dock_canvas(self) -> None:
        """The canvas comes back to its card (closing its window does this too)."""
        window, self.canvas_window = self.canvas_window, None
        if window is None:
            return
        window.card.removeWidget(self.center)
        self.build_stage.card_slot.insertWidget(0, self.center)
        self.center.show()
        self.canvas_away.hide()
        window.take_back()
        window.deleteLater()
        self._show_canvas_out(False)
        log.info("build canvas back in its card")

    def _show_canvas_out(self, out: bool) -> None:
        """The monitor button shows where the canvas is, without asking for another move."""
        blocked = self.pop_canvas_button.blockSignals(True)
        self.pop_canvas_button.setChecked(out)
        self.pop_canvas_button.blockSignals(blocked)

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
            QTimer.singleShot(0, self, self._share_stage)
        else:
            if self.splitter.parent() is not self.canvas_page:
                self.canvas_page.layout().addWidget(self.splitter, 1)
                self.splitter.show()
            for preview in self.previews.values():
                preview.live_look = False
                preview.align = "center"
                preview.show_title = preview.twin or self.in_place is not None
        self.modes.setCurrentIndex(1 if self.mode == "live" else 0)
        if self.canvas_window is not None:  # Live borrows the canvases for its stage: the window waits for Build
            self.canvas_window.setVisible(self.mode == "build")
        self._sync_engine_staged()
        for preview in self.previews.values():
            preview.update()

    def _share_stage(self) -> None:
        """Give each canvas stage width in proportion to its shape, so a 16:9 and a 9:16 both fill the height."""
        shown = [c for c in self.collection.canvases if c.enabled and self.canvas_columns[c.id].isVisibleTo(self.splitter)]
        if len(shown) < 2:
            for preview in self.previews.values():
                preview.align = "center"  # one canvas: in the middle of the stage
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
        sending = self._sending_ids() if self.streaming is not None else None
        levels = dict(levels or {})
        for output in streams:
            service = SERVICES.get(output.service or "custom", SERVICES["custom"])
            issue = problem(output, self.collection, probably_has_key) if output.enabled else None
            if not issue and output.enabled and sending is None and output.id in getattr(self, "_unreachable", {}):
                issue = "Won't connect"
                levels.setdefault(output.id, "warning")
            note = (notes or {}).get(output.id) or issue or ("Ready" if output.enabled else "Off")
            name = service.label
            if sum(1 for o in streams if o.service == output.service) > 1:  # two Twitch rows: say which shape
                name += f" · {output.canvas.capitalize()}"
            on = output.enabled if sending is None else output.id in sending  # live: on means sending now
            places.append(Destination(output.id, name, on, note, output.canvas,
                                      output.bandwidth_test if service.bandwidth_test else None,
                                      output.service or "custom"))
        self.live_side.show_destinations(places, levels)
        key = tuple((o.id, o.enabled, o.service, o.server, o.canvas) for o in streams)
        if key != getattr(self, "_reach_key", key) and self.streaming is None:  # a place added, switched or moved: try it soon
            self._reach_key = key
            QTimer.singleShot(1500, self, self._check_reach)
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

    PEEK_AFTER_MS = 250  # pointed at this long (not just passed over) before its pages open
    PEEK_KEEP_MS = 4000  # and kept open this long after the pointer leaves

    def _peek_bloom(self, scene_id: str | None) -> None:
        """A bloom pointed at in Live: its sources (web pages, cameras) open ahead, so a click switches to pages
        already loaded. Its linked twins too, since they switch with it."""
        timer = getattr(self, "_peek_timer", None)
        if timer is None:
            timer = self._peek_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._set_peek(self._peek_next))
        self._peek_next = scene_id
        timer.start(self.PEEK_AFTER_MS if scene_id is not None else self.PEEK_KEEP_MS)

    def _set_peek(self, scene_id: str | None) -> None:
        ids = set()
        if scene_id is not None:
            ids.add(scene_id)
            ids |= {s.id for s in self.collection.scenes if s.linked_to == scene_id}
            scene = next((s for s in self.collection.scenes if s.id == scene_id), None)
            if scene is not None and scene.linked_to:
                ids.add(scene.linked_to)
        engine = getattr(self, "engine", None)
        if engine is not None:
            engine.peek = frozenset(ids)  # a fresh object: the render thread reads it without a lock

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
        self.brand_name = name
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
        # Build only: the shape you're editing, its tools, and the zoom. Live only: the clock, Garden check,
        # Record and Go live (Build is for making things; the show runs in Live).
        shapes = QFrame()
        shapes.setObjectName("shapePill")
        shapes.setStyleSheet(f"#shapePill {{ background: {theme.PANEL}; border-radius: 19px; }}")
        shapes.setFixedHeight(38)  # tabs 30 high + 4 each side: the highlight sits evenly inside the pill
        shapes_row = QHBoxLayout(shapes)
        shapes_row.setContentsMargins(2, 0, 2, 0)
        shapes_row.addWidget(self.canvas_tabs)
        self._build_only = [bar.addWidget(shapes)]
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        tools = QWidget()
        tools_row = QHBoxLayout(tools)
        tools_row.setContentsMargins(0, 0, 0, 0)
        tools_row.setSpacing(8)
        for button in (self.link_button, self.safe_zone_button):
            tools_row.addWidget(button)
        from sprigcast.app.zoom_control import ZoomControl

        self.zoom_control = ZoomControl(self._build_preview,
                                        lambda: [p for p in [self._build_preview(), *self.previews.values(),
                                                             self.part_preview] if p is not None])
        tools_row.addWidget(self.zoom_control)
        self._build_only.append(bar.addWidget(tools))
        self.mode_clock = QLabel("")
        self.mode_clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._live_only = [bar.addWidget(self.mode_clock)]
        self._live_only.append(bar.addWidget(self.live_side.header_box))
        divider = QFrame()
        divider.setFixedSize(1, 24)
        divider.setStyleSheet(f"background: {theme.EDGE_STRONG};")
        bar.addWidget(divider)
        self.screenshot_button = QToolButton()
        self.screenshot_button.setIcon(icons.icon("shutter", theme.LABEL, 18))
        self.screenshot_button.setIconSize(QSize(18, 18))
        self.screenshot_button.setFixedSize(38, 38)
        self.screenshot_button.setToolTip("Screenshot every canvas, to your Screenshots folder")
        self.screenshot_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screenshot_button.clicked.connect(lambda: self._screenshot(background=True))
        self._build_only.append(bar.addWidget(self.screenshot_button))  # Live has Screenshot under its canvases
        self.pop_canvas_button = QToolButton()
        self.pop_canvas_button.setIcon(icons.icon("monitor", theme.LABEL, 18))
        self.pop_canvas_button.setIconSize(QSize(18, 18))
        self.pop_canvas_button.setFixedSize(38, 38)
        self.pop_canvas_button.setCheckable(True)
        self.pop_canvas_button.setToolTip("Edit the canvas in its own window")
        self.pop_canvas_button.setStyleSheet(theme.button_style(19, "0", "QToolButton"))
        self.pop_canvas_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pop_canvas_button.clicked.connect(
            lambda: self._dock_canvas() if self.canvas_window is not None else self._pop_out_canvas())
        self._build_only.append(bar.addWidget(self.pop_canvas_button))
        self.palette_button = QToolButton()
        self.palette_button.setIcon(icons.icon("search", theme.LABEL, 18))
        self.palette_button.setIconSize(QSize(18, 18))
        self.palette_button.setFixedSize(38, 38)
        self.palette_button.setToolTip("Search commands, blooms and settings (Ctrl+K)")
        self.palette_button.setStyleSheet(
            theme.button_style(19, "0", "QToolButton"))
        self.palette_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.palette_button.clicked.connect(lambda: self._open_palette())
        self.screenshot_button.setStyleSheet(self.palette_button.styleSheet())
        bar.addWidget(self.palette_button)
        self.settings_button = QToolButton()
        self.settings_button.setIcon(icons.icon("gear", theme.LABEL, 18))
        self.settings_button.setIconSize(QSize(18, 18))
        self.settings_button.setFixedSize(38, 38)
        self.settings_button.setToolTip("Settings (Ctrl+,)")
        self.settings_button.setStyleSheet(self.palette_button.styleSheet())
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.clicked.connect(self._gear_clicked)
        self._gear_watch = GearWatch(self.settings_button, self._gear_clicked)  # logs presses that reach it (PC test: clicks did nothing)
        bar.addWidget(self.settings_button)
        self._set_header_clock("Offline", False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.mode_bar = bar
        self._show_mode_bar(self.mode)

    def _show_mode_bar(self, mode: str) -> None:
        for action in getattr(self, "_build_only", []):
            action.setVisible(mode == "build")
        for action in getattr(self, "_live_only", []):
            action.setVisible(mode == "live")

    def _zoom_moved(self) -> None:
        control = getattr(self, "zoom_control", None)
        if control is not None:
            control.refresh()

    def _build_preview(self):
        """The canvas Build is showing now: the open sprig's, or the shape's."""
        if self.center.currentIndex() == 1:
            return self.part_preview
        return self.previews.get(self.build_canvas)

    def _set_header_clock(self, text: str, live: bool) -> None:
        if not live:
            self._offline_clock = text
            if getattr(self, "_side_parts", None):
                return  # recording: the clock stays (see _side_status)
        self.mode_clock.setTextFormat(Qt.TextFormat.PlainText)
        self.mode_clock.setText(text)
        font = self.mode_clock.font()
        font.setPixelSize(16 if live else 13)
        font.setWeight(font.Weight.DemiBold if live else font.Weight.Normal)
        self.mode_clock.setFont(font)
        self.mode_clock.setStyleSheet(f"color: {theme.LIVE_TEXT if live else theme.MUTED}; padding-left: 6px;")
        self._hold_clock_width()
        QTimer.singleShot(0, self, self._fit_header)

    def start_in_live(self) -> None:
        """SprigCast opens on Live (Destinations, Go live) every time, whatever screen it closed on: after an
        update it opened on Build with no Destinations (soak, 2026-11-19)."""
        self._set_mode("live")

    def _set_mode(self, mode: str) -> None:
        if mode == self.mode:
            self.mode_actions[mode].setChecked(True)
            return
        self.mode = mode
        if mode == "live":
            QTimer.singleShot(1500, self, self._check_reach)
        self.mode_actions[mode].setChecked(True)
        self.panels.set_mode(mode)
        editable = mode == "build"
        self._exit_in_place()
        self._place_canvases()
        self._show_part_editor()
        # Live shows what's on air, Build the bloom picked there: the "Empty bloom" card of a bloom picked in
        # Build stayed over the on-air picture in Live (2026-11-20).
        self._refresh_previews()
        for preview in self.previews.values():
            preview.editable = editable
            preview.update()
        self._apply_canvas_view()
        self._show_mode_bar(mode)
        QSettings().setValue("window/mode", mode)

    # Build mode's canvas tabs ---------------------------------------------------------

    def _live_view(self) -> str:
        """Which canvases Live's stage shows: "both", or one canvas id (only one that is switched on)."""
        enabled = [c.id for c in self.collection.canvases if c.enabled]
        view = str(QSettings().value("live/canvas_view", "both") or "both")
        if len(enabled) < 2:
            return enabled[0] if enabled else "both"
        return view if view == "both" or view in enabled else "both"

    def _set_live_view(self, view: str) -> None:
        QSettings().setValue("live/canvas_view", view)
        self._apply_canvas_view()
        if self.mode == "live":
            QTimer.singleShot(0, self, self._share_stage)
        self.live_panel.show_scenes(self.collection)

    def _apply_canvas_view(self) -> None:
        """Build shows only the canvas of the current tab; Live shows every canvas side by side."""
        build = self.mode == "build"
        self.canvas_tabs_row.setVisible(False)
        # The shape tabs only show when both shapes are on (nothing to switch between otherwise).
        self.canvas_tabs.setVisible(sum(c.enabled for c in self.collection.canvases) > 1)
        view = self._live_view()
        self.live_side.set_view(view, sum(c.enabled for c in self.collection.canvases) > 1)
        self.live_stage.set_blooms_wide(view == "both")
        self.live_panel.set_view(view)
        for canvas in self.collection.canvases:
            column = self.canvas_columns[canvas.id]
            # Build shows just the shape on the tab you're on; the other one isn't drawn (or rendered) at all.
            # Live shows both, or just the one picked under the stage.
            column.setVisible(canvas.enabled and (canvas.id == self.build_canvas if build else
                                                  view in ("both", canvas.id)))
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
        # Now, so the swap doesn't show a frame of the old sizes, and again once the columns have their new sizes.
        # Live too: switching a canvas off (Settings, the canvas menu) left the other one leaning to the side
        # where its twin had been, with the twin's space empty (PC, 2026-11-22).
        self._share_stage()
        QTimer.singleShot(0, self, self._share_stage)
        primary = self.collection.canvases[0].id
        self.link_button.setVisible(self.build_canvas != primary)
        canvas = self.collection.canvas(self.build_canvas)
        self.safe_zone_button.setVisible(canvas.height > canvas.width)  # only vertical canvases get zones
        self._zoom_moved()

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
                msgbox.information(self, "SprigCast", f"The {canvas.id} canvas is recording or live. "
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
        self._apply_canvas_view()

    def _privacy_to_open_windows(self) -> None:
        """Hide from screen capture reaches the windows already open (Settings itself, on Apply), not only the
        next one opened. Pop-out projectors are never hidden: they're there to be captured."""
        from sprigcast.app import privacy
        from sprigcast.app.projector import Projector

        for window in QApplication.topLevelWidgets():
            if (window is self or isinstance(window, Projector) or not window.isVisible()
                    or not window.testAttribute(Qt.WidgetAttribute.WA_WState_Created)):
                continue
            if window.windowType() in (Qt.WindowType.Dialog, Qt.WindowType.Window, Qt.WindowType.Tool):
                privacy.apply_to(window)

    def _apply_preview_settings(self) -> None:
        """Settings → Preview: "Show what's outside the canvas" on Build's canvases."""
        from sprigcast.app.preview import show_overflow_setting

        on = show_overflow_setting()
        for preview in self.previews.values():
            if preview.show_overflow != on:
                preview.show_overflow = on
                preview.update()

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
        """File · Edit · Panels · Gardens · Paths · Tools · Help. Only what has no button on screen: Live and
        Build are the header tabs, the canvas choice is the Both/Horizontal/Vertical pills, Pop out is on the
        canvas right-click and each panel's ↗, Settings is the gear, the palette is the search button, the
        selected thing's actions are its right-click menu, switching gardens and paths is the header chip,
        Stats is the line along the bottom, Optimize and Check for updates are in Settings → General and the
        Starter kit is on the + beside Blooms (their keys and the palette still reach them)."""
        bar = self.menuBar()

        file_menu = bar.addMenu("&File")
        file_menu.addAction("Open recordings folder").triggered.connect(self._open_recordings_folder)
        file_menu.addAction("Repot as MP4…").triggered.connect(lambda: self._ask_repot())
        file_menu.addSeparator()
        file_menu.addAction("Export settings…").triggered.connect(lambda: self.export_settings())
        file_menu.addAction("Import settings…").triggered.connect(lambda: self.import_settings())
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # Edit: Undo and Redo. The selected thing's actions are its right-click menu (their keys work in Build).
        edit_menu = bar.addMenu("&Edit")
        undo = edit_menu.addAction("Undo")
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        undo.triggered.connect(self.undo)
        redo = edit_menu.addAction("Redo")
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        redo.triggered.connect(self.redo)

        # Keys for things that are buttons on screen, so they need no menu.
        # (Ctrl+1 and Ctrl+2 are on the Live and Build tabs' own actions.)
        for keys, slot in ((["Ctrl+K", "Ctrl+Shift+P"], lambda: self._open_palette()),
                           (["Ctrl+,"], lambda: self._open_settings())):
            action = QAction(self)
            action.setShortcuts([QKeySequence(k) for k in keys])
            action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            action.triggered.connect(lambda _=False, f=slot: f())
            self.addAction(action)

        self.docks_menu = bar.addMenu("&Panels")
        self.stats_action = QAction("Stats", self)  # the line along the bottom; the key still opens it
        self.stats_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.stats_action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        self.stats_action.triggered.connect(lambda: self.stats_strip.toggle())
        self.addAction(self.stats_action)
        self.reset_layout_action = QAction("Reset panels", self)
        self.reset_layout_action.triggered.connect(lambda: self.panels.reset())
        self.custom_docks_action = QAction("Web panels…", self)
        self.custom_docks_action.setToolTip("A web page as a panel: activity feed, Streamer.bot, TikFinity…")
        self.custom_docks_action.triggered.connect(lambda: self._edit_custom_docks())
        self.docks_menu.aboutToShow.connect(self._fill_docks_menu)
        self._fill_docks_menu()

        self.gardens.build_menus(bar)

        tools_menu = bar.addMenu("&Tools")
        tools_menu.addAction("Get started…").triggered.connect(lambda: self.open_getting_started())
        doctor_action = tools_menu.addAction("Stream doctor…")
        doctor_action.setShortcut(QKeySequence("Ctrl+Shift+D"))
        doctor_action.triggered.connect(lambda: self._open_doctor())
        tools_menu.addSeparator()
        tools_menu.addAction("Hotkeys…").triggered.connect(lambda: self.open_hotkeys())
        tools_menu.addAction("Plugin store…").triggered.connect(lambda: self.open_plugin_store())
        self._tools_menu = tools_menu
        self._plugin_tool_actions: list = []
        tools_menu.aboutToShow.connect(self._fill_plugin_tools)

        help_menu = bar.addMenu("&Help")
        help_menu.addAction("Keyboard shortcuts").triggered.connect(lambda: self._show_shortcuts())
        help_menu.addSeparator()
        help_menu.addAction("Report a problem…").triggered.connect(lambda: self.report_problem())
        help_menu.addAction("Export report").triggered.connect(lambda: self.export_report())
        help_menu.addAction("Open logs folder").triggered.connect(lambda: self._open_logs())
        help_menu.addSeparator()
        help_menu.addAction("About SprigCast").triggered.connect(lambda: self._show_about())
        help_menu.addAction("Support SprigCast").triggered.connect(lambda: self._open_tip_page())

    def _fill_plugin_tools(self) -> None:
        """Tools menu entries that added plugins bring (register_tool), under Plugin store. Left as they are
        when the plugins' tools haven't changed, so screen readers keep the items they were told about."""
        from sprigcast.app.menu_refresh import announce

        tools = list(plugin_hooks.tools())
        if getattr(self, "_plugin_tools_shown", None) == tools:
            return
        self._plugin_tools_shown = tools
        for action in self._plugin_tool_actions:
            self._tools_menu.removeAction(action)
            action.deleteLater()
        self._plugin_tool_actions = []
        for label, fn in tools:
            action = self._tools_menu.addAction(label)
            action.triggered.connect(lambda _=False, fn=fn: self._run_plugin_tool(fn))
            self._plugin_tool_actions.append(action)
        announce(self._tools_menu)

    def _run_plugin_tool(self, fn) -> None:
        try:
            fn(self.api)
        except Exception:
            log.exception("a plugin's Tools entry failed")
            self.statusBar().showMessage("That plugin hit a problem (it's in the log).", 6000)

    def export_report(self, target: Path | None = None, reveal: bool = True) -> Path | None:
        """File → Export report: one .zip with the recent logs, stream reports, crash notes, this PC and the
        settings that matter, private things hidden (app/tester_report.py), saved to the desktop and shown in
        its folder. Made on a worker: building it on the window's thread, after a save box that took a while to
        come up on its own, froze the window 310 ms on the PC (2026-11-18 pop-up sweep). Returns where it goes;
        it's there once self._report_future is done."""
        from sprigcast.app import tester_report
        from sprigcast.core import workers

        if self._report_future is not None and not self._report_future.done():
            self.statusBar().showMessage("Still saving the report…", 4000)
            return None
        if target is None:
            target = tester_report.default_folder() / tester_report.default_name()
        if target.suffix.lower() != ".zip":
            target = target.with_name(target.name + ".zip")
        # The worker reads a copy: the window may change the collection while the report is made.
        collection = self.collection.model_copy(deep=True)
        plugins_dir = self.plugins_dir
        self.statusBar().showMessage("Saving the report…")

        def make():
            try:
                tester_report.build(target, collection=collection, settings=QSettings(), plugins_dir=plugins_dir)
            except OSError as exc:
                return exc
            if reveal and sys.platform == "win32":
                import subprocess

                subprocess.Popen(["explorer", "/select,", str(target)])
            return None

        saved = _ReportSaved(self)
        saved.done.connect(lambda failed: self._report_saved(target, failed, reveal, saved),
                           Qt.ConnectionType.QueuedConnection)  # emitted on the worker
        self._report_future = workers.run(lambda: saved.done.emit(make()), name="export report")
        return target

    def _report_saved(self, target: Path, failed: OSError | None, reveal: bool, saved) -> None:
        saved.deleteLater()
        if failed is not None:
            log.warning("report: couldn't save %s: %s", target.name, failed)
            self.statusBar().showMessage(f"Couldn't save the report: {failed.strerror or failed}", 8000)
            return
        log.info("report exported")
        self.statusBar().showMessage(f"Report saved: {target.name}", 8000)
        if reveal and sys.platform != "win32":
            open_folder(target.parent)

    def report_problem(self):
        """Help → Report a problem: what happened, then send it with the log (app/problem_report.py)."""
        from sprigcast.app import problem_report

        return problem_report.ask(self)

    def _ask_repot(self) -> None:
        """File → Repot as MP4: pick recordings (MKV, FLV, MOV, TS) and copy each into an MP4 beside it."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Repot as MP4", str(self._recordings_folder()), "Videos (*.mkv *.flv *.mov *.ts);;All files (*)")
        files = [Path(p) for p in paths if Path(p).suffix.lower() != ".mp4"]
        if files:
            self.repot(files)

    def repot(self, files: list[Path], delete_original: bool = False):
        """Remux each file into an MP4 next to it on a background thread (no re-encoding, so it takes seconds
        and keeps the quality), then say how it went in the status bar."""
        from sprigcast.output.naming import remux_later

        word = "file" if len(files) == 1 else "files"
        self.statusBar().showMessage(f"Repotting {len(files)} {word} as MP4…")
        reporter = _Repotted(self)
        reporter.done.connect(lambda made: self._repotted(files, made, reporter),
                              Qt.ConnectionType.QueuedConnection)  # emitted on the repot thread
        return remux_later(files, delete_original, done=reporter.done.emit)

    def _repotted(self, files: list[Path], made: list, reporter) -> None:
        reporter.deleteLater()
        missed = len(files) - len(made)
        if not missed:
            text = f"Repotted {made[0].name}" if len(made) == 1 else f"Repotted {len(made)} files as MP4"
        else:
            text = f"Couldn't repot {missed} of {len(files)} (details in the log)"
        self.statusBar().showMessage(text, 10000)

    def _open_tip_page(self) -> None:
        """Help → Support SprigCast: what the project is and why a tip helps, then Close or the tip page."""
        from sprigcast.app.support import support_dialog

        dialog = support_dialog(self)
        modal(dialog)

    def _show_garden_file(self) -> None:
        """Explorer, open on this garden's file and with it selected."""
        self._reveal(Path(self.scenes_path))

    def _reveal(self, path: Path) -> None:
        """Explorer, open on the file's folder with the file selected."""
        if sys.platform == "win32" and path.exists():
            import subprocess

            subprocess.Popen(["explorer", "/select,", str(path)])
            return
        open_folder(path.parent)

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
        lines += [("Ctrl+1", "Live"), ("Ctrl+2", "Build"), ("Ctrl+K", "Search"), ("Ctrl+,", "Settings"),
                  ("Ctrl + wheel", "Zoom the canvas"), ("Space + drag, middle drag", "Pan the canvas"),
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
        box = msgbox.MessageCard("Keyboard shortcuts", f"<table cellspacing='6'>{rows}</table>", self, rich=True)
        box.add_button("Done", primary=True)
        modal(box)

    def _show_about(self) -> None:
        from sprigcast.app.about import show_about

        show_about(self)

    def _fill_docks_menu(self) -> None:
        """Each panel this screen offers (ticked when shown), then Web panels… and Reset panels."""
        self.panels.fill_menu(self.docks_menu, [self.custom_docks_action, self.reset_layout_action])

    def _edit_custom_docks(self) -> None:
        if not custom_docks.web_engine_available():
            msgbox.warning(self, "SprigCast", "Web panels need Qt WebEngine, which isn't installed.")
            return
        dialog = custom_docks.CustomDocksDialog(custom_docks.load(), self)
        if modal(dialog):
            custom_docks.apply(self, dialog.result_docks())

    # Engine link ------------------------------------------------------------

    def _refresh_meter(self) -> None:
        self.last_resources = self.meter.sample()
        self._watch_encoders()
        self.meter_label.setText(describe(self.last_resources))
        self.live_side.stats.setText(describe(self.last_resources))
        warning = ram_warning(self.last_resources)
        self._refresh_garden(bool(warning))
        self.meter_label.setStyleSheet(f"color: {theme.WARN}" if warning else "")
        self.meter_label.setToolTip(warning or self._meter_tip)
        if warning and not self._warned_ram:
            log.warning(warning)
            self._warn_note(warning, 12000)
        self._warned_ram = bool(warning)
        self._check_busy_pc()

    def _check_busy_pc(self) -> None:
        """When the whole PC stays busy, name the other apps using it (looked up off the UI thread)."""
        from sprigcast.app import busy_pc

        note = getattr(self, "_busy_note", None)
        if note:
            self._busy_note = None
            log.warning(note)
            self._warn_note(note, 12000)
        try:
            import psutil

            cpu = psutil.cpu_percent(None)  # whole PC since the last call (every couple of seconds)
        except Exception:
            cpu = None
        gpu = self.meter.gpu_pct
        watch = getattr(self, "_busy_watch", None)
        if watch is None:
            watch = self._busy_watch = busy_pc.BusyWatch()
        if watch.feed(cpu, gpu):
            def look() -> None:
                self._busy_note = busy_pc.busy_message(busy_pc.heavy_apps(), cpu, gpu)

            from sprigcast.core import workers

            workers.run(look, name="busy-pc")
        spike = getattr(self, "_spike_watch", None)
        if spike is None:
            spike = self._spike_watch = busy_pc.spike_watch()
        if spike.feed(cpu, None):
            on_air = self.streaming is not None or self.recording is not None

            def name_them() -> None:
                apps = busy_pc.heavy_apps(with_use=True)
                log.info(busy_pc.spike_line(apps, cpu))
                # On air, a job of Windows' own that can wait is said at once, not after a busy minute.
                jobs = busy_pc.background_jobs(apps) if on_air else []
                if jobs and time.monotonic() - getattr(self, "_job_said", float("-inf")) >= busy_pc.WARN_EVERY_S:
                    self._job_said = time.monotonic()
                    self._busy_note = f"Your PC is busy (CPU {cpu or 0:.0f}%). " + busy_pc.job_message(*jobs[0])

            from sprigcast.core import workers

            workers.run(name_them, name="busy-pc-log")

    def _preview_mode(self) -> str:
        """Settings → General → This PC → Preview while live: "smooth" unless on air with Light or Off."""
        if self.streaming is None and self.recording is None:
            return "smooth"
        choice = getattr(self, "_preview_live", None)
        if choice is None:
            from sprigcast.app.settings_window import preview_choice

            choice = self._preview_live = preview_choice()
        return choice

    def _watched_previews(self) -> frozenset[str]:
        """Previews on screen right now; the engine skips copying back the rest (or everything when minimised)."""
        projected = {p.canvas_id for p in self.projectors if p.view is None}
        if self.isMinimized() or not self.isVisible():
            return frozenset(projected)  # a projector keeps showing while the main window is down
        keys = projected | {cid for cid, p in self.previews.items() if p.isVisible() and p.width() > 1}
        if self.mode == "build":  # a canvas showing the bloom being edited doesn't show its on-air picture:
            edited = {cid for cid in self.engine.staged if self.previews[cid].isVisible()}  # that one isn't copied
            keys = (keys - (edited - projected)) | {f"next:{cid}" for cid in edited}
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
        tray = getattr(self, "tray", None)
        if tray is not None and tray.icon is not None:
            tray.show_state(self.streaming is not None, self.recording is not None)
        for projector in self.projectors:
            if projector.view is not None:
                projector.take_pending()
        mode = self._preview_mode()
        fps = 15 if mode == "light" else 30
        if getattr(self.engine, "preview_fps", fps) != fps:
            self.engine.preview_fps = fps
        off = mode == "off"
        for preview in self.previews.values():
            if preview.paused != off:
                preview.paused = off
                preview.update()
        watched = self._watched_previews()
        if off:  # Preview while live: Off. Projectors still draw (they're what you asked to see).
            watched = frozenset(k for k in watched if k not in self.previews and not k.startswith("next:"))
        if watched != self.engine.previews:
            self.engine.previews = watched
        # Measured four times a second, not on every poll: it asks Qt for each preview's size and screen scale
        # while holding Python's lock, which the render thread was seen waiting for (PC soak 2026-11-19).
        measured = time.monotonic()
        if measured - getattr(self, "_sizes_measured", 0.0) >= 0.25:
            self._sizes_measured = measured
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
                heard = self.__dict__.setdefault("_heard_at", {})
                now = time.monotonic()
                self._last_levels = (now, event.levels)  # Settings → Audio → Devices shows them
                for source_id, (_peak, rms) in event.levels.items():
                    if rms > -50:  # above the hiss: something was heard
                        heard[source_id] = now
                if theme.bursting():
                    pass  # a pop-up opening: the meters skip a few beats (moving them was 69 ms of its build)
                elif self.mode == "live":
                    self.live_side.set_levels(event.levels)
                else:
                    self.sound_strips.set_levels(event.levels)
                self.peak_watch.feed(event.levels)
                self.control.meters(event.levels)  # only when a connected app asked for meters
                if plugin_hooks._LEVEL_WATCHERS:  # plugins following sound levels (register_level_watcher)
                    plugin_hooks.levels_heard(self.api, event.levels)
            elif isinstance(event, SourceInfo):
                self.source_sizes, self.source_statuses = event.sizes, event.statuses
                desk = frozenset(sid for sid, st in event.statuses.items() if st == DESKTOP_SOUND)
                if desk != getattr(self, "_desktop_sounds", frozenset()):
                    self._desktop_sounds = desk  # a page's sound went to desktop audio: the Sound card says so
                    self._refresh_audio_soon()
                if self._start_sizes:
                    self._place_new_sources(event.sizes)
                moved, touched = self.collection.keep_cards_put(event.sizes)  # a clock card's box grew or shrank
                if moved:
                    self._changed()  # its places moved so the card stays put
                elif touched:
                    self._settings_changed(touched)  # only the width it's laid out for: the light way
                for preview in [*self.previews.values(), self.part_preview]:
                    preview.sizes, preview.statuses = self.source_sizes, self.source_statuses
                self._refresh_source_labels()
                selected = self._item()
                if selected is not None and self.spin["w"].value() == 0 and selected.source in self.source_sizes:
                    self._updating = True
                    self._show_spin_values(selected)  # its size just became known
                    self._updating = False
            elif isinstance(event, EngineError):
                self.statusBar().showMessage(event.message.split("\n")[0], 10000)
                if event.graphics or "compositor" in event.message:
                    msgbox.critical(self, "SprigCast", event.message)
        if self.recording is not None:
            self._refresh_recording_status()
        if self.streaming is not None:
            self._refresh_stream_status()
        awake.hold(self.recording is not None or self.streaming is not None)
        self._grab_thumbs()

    def _send_whole(self) -> None:
        self._engine_dump = None  # the next send is whole too: the engine's copy isn't the last one compared
        self.engine.send(ReplaceCollection(self.collection.snapshot()))

    def _send_to_engine(self) -> None:
        """The collection to the engine: only the blooms on air when nothing else changed since the last send (a
        switch: the engine skips comparing every source in the switch frame), else a whole copy."""
        _show_sprigcast_to_captures(self)
        dump = self.collection.model_dump()
        last = getattr(self, "_engine_dump", None)
        if last is not None and dump["active_scenes"] != last["active_scenes"]:
            from sprigcast.core import pacing

            pacing.bloom_switched()  # no Python clean-up in the switch's frames
        if last is not None and dump["active_scenes"] != last["active_scenes"] \
                and {**dump, "active_scenes": None} == {**last, "active_scenes": None}:
            self.engine.send(SwitchBlooms(dict(dump["active_scenes"])))
        else:
            self.engine.send(ReplaceCollection(type(self.collection).model_validate(dump)))
        self._engine_dump = dump

    def _changed(self, save: bool = True) -> None:
        """Hand a copy of the collection to the engine after every edit, and save it unless mid-drag."""
        if not save:  # mid-drag: at most one copy per frame, the newest (copying on every mouse move lagged)
            timer = getattr(self, "_drag_send", None)
            if timer is None:
                timer = self._drag_send = QTimer(self)
                timer.setSingleShot(True)
                timer.setInterval(16)
                timer.timeout.connect(self._send_whole)
            if not timer.isActive():
                timer.start()
            return
        timer = getattr(self, "_drag_send", None)
        if timer is not None:
            timer.stop()  # this full send carries the drag's end
        self._save_soon()
        self._remember_for_undo()
        self._send_to_engine()
        self._sync_push_key()
        if getattr(self, "_audio_on_air", None) != self.collection.active_scenes:
            self._audio_shown = None  # another bloom on air: Live's Sound card lists its sounds
            self._refresh_audio_soon()
        self._chat_sources_changed()
        control = getattr(self, "control", None)
        if control is not None:
            control.soon()  # apps connected to the control server hear what changed

    def _settings_changed(self, sources) -> None:
        """Only these sources' settings changed, as a plugin keeps its own state (a stream timer started from a
        hotkey, a death counted): the engine gets just those settings, the garden is saved soon, apps hear of it.
        No whole copy of the garden for the engine and none for undo (it isn't an edit): on the PC (2026-11-18)
        that froze the window 544 ms on a timer hotkey and the stream skipped ~440 frames."""
        from sprigcast.core.events import UpdateSourceSettings

        self.engine.send(UpdateSourceSettings({s.id: dict(s.settings) for s in sources}))
        self._save_soon()
        control = getattr(self, "control", None)
        if control is not None:
            control.soon()

    SAVE_AFTER_MS = 2000  # the garden file is written this long after the last change

    def _save_soon(self) -> None:
        """Save the garden once changes settle, on a worker. Saving (and backing up) on every change was
        most of the window's pauses in the 2026-11-17 heavy run: a bloom switch every 15 s, a plugin hotkey
        every 5 s, each one wrote the whole garden on the window's thread."""
        timer = getattr(self, "_save_timer", None)
        if timer is None:
            timer = self._save_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(self.SAVE_AFTER_MS)
            timer.timeout.connect(lambda: (self.collection.save_later(self.scenes_path), self._keep_path_file()))
        timer.start()

    def _keep_path_file(self) -> None:
        """The path's own file follows the garden's copy as soon as places, recording or encoder settings
        change (PC 2026-11-19: two places added at 21:49, the path file still as written at 21:26). Only
        written when it differs from what this run last wrote: a few lines of text, compared in memory."""
        from sprigcast.core.gardens import PathSettings

        try:
            shelf = self.gardens.shelf
            text = PathSettings.of(self.collection).model_dump_json(indent=2)
            key = (str(shelf.path_file()), text)
            if key == getattr(self, "_path_file_written", None):
                return
            shelf.sync_path(self.collection)
            self._path_file_written = key
        except Exception as exc:  # noqa: BLE001 (the garden holds the newest copy either way)
            log.warning("could not save the path: %s", exc)

    def _save_pending(self) -> None:
        """A save still waiting (_save_soon): write it now, here (closing, or before switching gardens)."""
        timer = getattr(self, "_save_timer", None)
        if timer is not None and timer.isActive():
            timer.stop()
            self.collection.save(self.scenes_path)
            self._keep_path_file()

    # Undo and redo ------------------------------------------------------------------------

    UNDO_STEPS = 50
    UNDO_MERGE_S = 0.8  # changes this close together (a slider, nudging with arrows) are one step

    def _remember_for_undo(self, now_json: str | None = None) -> None:
        now_json = now_json if now_json is not None else self.collection.to_text()
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
        self._send_whole()
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

    def request_close(self) -> None:
        """Close as if you'd clicked the window's ×: the tray and "You're live" questions still apply. Used when
        the × was clicked while a pop-up was open (Windows doesn't pass that click on: modal.py)."""
        self._user_close = True
        try:
            self.close()
        finally:
            self._user_close = False

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        tray = getattr(self, "tray", None)
        user = event.spontaneous() or getattr(self, "_user_close", False)
        if user and tray is not None and tray.closing():
            event.ignore()  # Close to the tray: hidden, and everything carries on
            self._pace_poll()
            return
        if user and not self._may_close():
            event.ignore()
            return
        log.info("closing: %s", close_reason(user, QApplication.instance()))  # who ended this run, in the log
        if tray is not None:
            tray.remove()
        self.hide()  # gone at once; what follows only tidies up
        from sprigcast.core import pacing

        pacing.ui_rest()  # a hidden window isn't one that doesn't answer
        pacing.exit_guard()  # whatever gets stuck from here on, SprigCast ends (after saving, below)
        awake.hold(False)
        from sprigcast.app import projector as pop_outs

        pop_outs.save_open(self.projectors)  # Reopen them next time (General → Pop-out views)
        for projector in list(self.projectors):
            projector.close()
        self.panels.close_windows()
        self._dock_canvas()
        self.chat_panel.shutdown()
        forgetting = getattr(self, "_forgetting", False)  # Clear all my data: save nothing on the way out
        if forgetting:
            timer = getattr(self, "_save_timer", None)
            if timer is not None:
                timer.stop()
        else:
            self._save_pending()
            self._save_layout()
            self._save_thumbs()
            self.gardens.close()
        self.hotkeys.stop()
        self.push_key.stop()
        self.meter.stop()
        self.control.stop()
        if self.camera is not None:
            self._camera_timer.stop()
            self.camera.stop()
            self.camera = None
        self.engine.set_sound_out(None)
        if self.streaming is not None:
            self._stop_streaming()
        if self.recording is not None:
            self._stop_recording(wait=True)
        self._wait_for_stopped_recording()
        if self.replay is not None:
            self.replay.stop(wait=True)
            self.replay = None
        self.timer.stop()
        self.engine.stop(wait=False)  # the render thread lets go of the card on its own time (main waits)
        if self.browser_host is not None:
            self.browser_host.shutdown()
        from sprigcast.app import plugin_helper

        plugin_helper.shutdown()  # your plugins' sources end with SprigCast
        if not forgetting:
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
            row.setData(0, LIB_TAG, "on Live" if folder.live else "")  # no count: open it to see
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
                child.setData(0, LIB_TAG, "⇄" if self._link_label(scene) else "")  # the other shape's name: tooltip
                child.setData(0, ROLE_COLOUR, folder.colour if folder is not None else None)
                child.setData(0, ROLE_ON_AIR, scene.id == active)
                child.setData(0, ROLE_THUMB, thumbs.get(scene.id))
                child.setFlags(BLOOM_FLAGS)
                child.setToolTip(0, f"{scene.name}: click to edit it" + ("  ·  on air" if scene.id == active else "")
                                 + (f"  ·  switches with{self._link_label(scene).replace('⇄', '')}"
                                    if self._link_label(scene) else ""))
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
        users = self.collection.part_users()
        for folder, parts in self.collection.grouped_parts_on(canvas_id):  # this shape's sprigs only
            holder = sprigs if folder is None else folder_row(sprigs, folder, len(parts))
            for part in parts:
                uses = len(users.get(part.id, ()))
                shapes = " + ".join(s.canvas for s in self.collection.shapes(part))
                child = QTreeWidgetItem([f"{part.name}   ({shapes} · used in {uses})"])
                child.setIcon(0, icons.icon("sprig", theme.ACCENT))
                child.setData(0, ROLE_ID, part.id)
                child.setData(0, ROLE_ROW, "sprig")
                kind = self.collection.sprig_kind(part, canvas_id, look=self._look_kind)
                child.setData(0, ROLE_SOURCE_KIND, kind)  # what it shows: camera, chat, game...
                child.setData(0, ROLE_COLOUR, self._sprig_colour(part, canvas_id) if kind == "color" else None)
                child.setData(0, LIB_NAME, part.name)
                child.setData(0, LIB_TAG, "")  # no words: an unused sprig is dimmed instead
                child.setData(0, ROLE_UNUSED, not uses)
                child.setFlags(BLOOM_FLAGS)
                child.setToolTip(0, f"{cap(a(PART))} ({shapes}), " + (f"in {uses} {SHOW if uses == 1 else SHOWS}" if uses
                                    else f"not in any {SHOW} yet") + ". Click to "
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

    def _pick_bloom(self, scene_id: str) -> None:
        """A bloom pill: edit that bloom. It never goes on air from here: Live picks what's on air."""
        self.selected_scene_id = scene_id
        self.selected_folder_id = self.collection.scene(scene_id).folder
        self.last_bloom_id = scene_id
        self._show_sound_where()
        if self.in_place is not None:
            self._exit_in_place()
        self._refresh_scenes()
        self._refresh_sources()
        self._show_part_editor()

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
        self._refresh_sprig_icons()
        self._refresh_properties()

    def _refresh_sprig_icons(self) -> None:
        """The Sprigs list's icons say what each sprig holds: kept right as leaves come and go (user's PC,
        2026-11-21: a leaf was added and the sprig still showed the empty +). Only rows that changed are touched."""
        tree = getattr(self, "scene_tree", None)
        if tree is None:
            return
        canvas_id = self.build_canvas
        was, self._updating = self._updating, True
        try:
            stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
            while stack:
                row = stack.pop()
                stack.extend(row.child(i) for i in range(row.childCount()))
                if row.data(0, ROLE_ROW) != "sprig":
                    continue
                part = next((s for s in self.collection.scenes if s.id == row.data(0, ROLE_ID)), None)
                if part is None:
                    continue
                kind = self.collection.sprig_kind(part, canvas_id, look=self._look_kind)
                colour = self._sprig_colour(part, canvas_id) if kind == "color" else None
                if row.data(0, ROLE_SOURCE_KIND) != kind:
                    row.setData(0, ROLE_SOURCE_KIND, kind)
                if row.data(0, ROLE_COLOUR) != colour:
                    row.setData(0, ROLE_COLOUR, colour)
        finally:
            self._updating = was

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
        kind, swatch = self._kind_and_swatch(config, canvas_id)
        row.setData(ROLE_KIND, kind)  # a sprig: what it shows (camera, chat...), so it reads at a glance
        row.setData(ROLE_SWATCH, swatch)
        row.setData(ROLE_CHILDREN, [])  # a sprig is one row, its icon and name: what's inside shows when it's open
        detail = "" if status in ("ok", "") else f"\n{status}"
        if hidden:
            detail += f"\n{hidden} hidden here"
        if "filters" in tags:
            detail += "\nHas filters"
        what = PART if config.kind == PART_KIND else KIND_NAMES.get(self._look_kind(config), config.kind)
        row.setToolTip(f"{config.name}  ({what}){detail}")

    @staticmethod
    def _look_kind(config) -> str:
        """The kind a row's icon shows: a camera that is a capture card (Elgato HD60 X...) shows as one."""
        if config.kind == "camera" and is_capture_card(str(config.settings.get("name") or config.name)):
            return "capture_card"
        return config.kind

    def _kind_and_swatch(self, config, canvas_id: str | None) -> tuple[str, str | None]:
        """The kind a row's icon shows, and a colour source's colour. A sprig shows what it holds."""
        kind = self._look_kind(config)
        part = self.collection.part_of(config) if kind == PART_KIND else None
        if part is not None:
            kind = self.collection.sprig_kind(part, canvas_id, look=self._look_kind)
            return kind, self._sprig_colour(part, canvas_id) if kind == "color" else None
        return kind, config.settings.get("color") if kind == "color" else None

    def _sprig_colour(self, part, canvas_id: str | None) -> str | None:
        layout = self.collection.shape_for(part, canvas_id) if canvas_id else part
        for item in layout.items:
            source = next((c for c in self.collection.sources if c.id == item.source), None)
            if source is not None and source.kind == "color":
                return source.settings.get("color")
        return None

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
        if item is not None:
            self.prop_locked.setChecked(item.locked)
            config = self.collection.source(item.source)
            self.prop_name.setText(config.name)
            self._show_spin_values(item)
            self.prop_uncrop.setVisible(any(item.crop))
            self.prop_extra.setText(
                {
                    "color": "Color",
                    "image": "Image",
                    "display_capture": "Monitor",
                    "window_capture": "Window",
                    "game_capture": "Game",
                    "camera": "Camera",
                    "text": "Text",
                    "browser": "Page",
                    "chat": "Chat box",
                }.get(config.kind, "Source") + " settings"
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

    def _refresh_audio_soon(self) -> None:
        """A sound changed: refresh the Sound card and strips once things settle (100 ms). Muting a group, a
        fader drag or a filter edit used to redo both for every change, holding the window up to 2 s."""
        timer = getattr(self, "_audio_refresh", None)
        if timer is None:
            timer = self._audio_refresh = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(100)
            timer.timeout.connect(self._refresh_audio)
        timer.start()

    def _refresh_audio(self) -> None:
        timer = getattr(self, "_audio_refresh", None)
        if timer is not None:
            timer.stop()  # this refresh covers any that were waiting
        use_types(self.collection.audio.sound_types)  # the garden's sound types, before each sound is put in one
        sounds = [s for s in self.collection.sources if self._has_sound(s)]
        items = [self._sound_item(s) for s in sounds]
        ordered = sorted(sounds, key=lambda s: s.name.lower())  # A to Z, like the Sound list on the left
        kinds = {s.id: s.kind for s in ordered}
        tags = {s.id: self._plays_in_tag(s) for s in ordered}
        shown = (items, kinds, tags, self.collection.audio.ducking, hasattr(self, "sound_strips"),
                 tuple(GROUP_COLOURS.items()))
        if shown == getattr(self, "_audio_shown", None):
            return  # nothing changed (Save in Settings redid every strip, holding SprigCast up ~0.7 s while live)
        self._audio_shown = shown
        # Live lists the sounds of the blooms on air: one placed only in other blooms, or picked to play in
        # other blooms only, waits for them (user 2026-11-20: Starting Soon's music showed on Ending Soon).
        # "Always show" keeps one listed anyway.
        away = self.collection.sounds_off_here() | self.collection.sounds_off_air()
        self._audio_on_air = dict(self.collection.active_scenes)
        self.live_side.show_sound([i for i in items if i.id not in away or i.pinned])
        if hasattr(self, "sound_strips"):
            by_id = {item.id: item for item in items}
            self.sound_strips.show_sound([by_id[s.id] for s in ordered], kinds, tags)
            self._show_sound_where()
        self.live_side.sound.set_mic_first(self.collection.audio.ducking)

    def _set_mic_first(self, on: bool) -> None:
        """Live's "Mic first" switch: ducking on or off (amounts stay as set in the Ducking window)."""
        self.collection.audio.ducking = on
        self._changed()

    def _show_sound_where(self) -> None:
        """The Sound card's "Plays in <bloom>": the bloom you last picked in Build."""
        if not hasattr(self, "sound_strips"):
            return
        bloom = next((s for s in self.collection.scenes if s.id == getattr(self, "last_bloom_id", None)), None)
        self.sound_strips.set_where(bloom.name if bloom else "")
        self.sound_strips.set_here(self.collection.sounds_in(bloom.id) if bloom else None)

    def _sound_item(self, config) -> Sound:
        st = config.settings
        monitor = st.get("monitor", False)
        return Sound(
            id=config.id, name=config.name, muted=bool(st.get("muted", False)),
            group=type_of(str(st.get("group") or ("Browser" if config.kind == "browser"
                                                  else auto_group(config.name, config.kind)))),
            pinned=bool(st.get("pinned", False)),
            volume_db=float(st.get("volume_db", 0.0)),
            monitor="only" if monitor == "only" else bool(monitor),
            mono=bool(st.get("mono", False)), balance=float(st.get("balance", 0.0) or 0.0),
            delay_ms=int(st.get("delay_ms", 0) or 0), voice=is_voice(st, config.kind),
            filters=[f.name for f in config.filters if f.enabled],
            can_monitor=config.kind != "audio_output",  # desktop audio already plays on your speakers
            tracks=planned_tracks(config),  # Just me shows where it goes once it is back
            apps=self._sound_source_text(config),
            desktop=config.kind == "browser" and self.source_statuses.get(config.id) == DESKTOP_SOUND,
        )

    def _sound_source_text(self, config) -> str | None:
        """The inspector's Source card: the apps, device or file a sound comes from (None: nothing to change)."""
        if config.kind == "app_audio":
            return self._app_sound_text(config)
        if config.kind in ("audio_input", "audio_output"):
            device = str(config.settings.get("device", "default") or "default")
            return "Default (follows Windows)" if device == "default" else sound_label(device)
        if config.kind == "media":
            path = str(config.settings.get("path", "") or "")
            return Path(path).name if path else "No file yet"
        return None

    def _change_sound_source(self, source_id: str) -> None:
        """The inspector's Source → Change: another mic or output, other apps, another file (user, 2026-11-20:
        "so I can change the mic to a different"). The sound keeps its volume, filters and where it plays."""
        config = next((c for c in self.collection.sources if c.id == source_id), None)
        if config is None:
            return
        if config.kind == "app_audio":
            self._pick_app_sound_apps(source_id)
            return
        if config.kind in ("audio_input", "audio_output"):
            old = str(config.settings.get("device", "default") or "default")
            device = self._pick_audio_device(config.kind, old, config.name, "Save")
            if device is None or device == old:
                return
            if sound_name_is_ours(config.name, config.kind, old):  # a name you chose stays
                config.name = sound_name(config.kind, device)
            config.settings = {**config.settings, "device": device}
        elif config.kind == "media":
            from sprigcast.app.media_dialog import MediaDialog

            only = config.settings.get("only") or ("sound" if is_sound_file(str(config.settings.get("path", ""))) else None)
            dialog = MediaDialog(config.name, config.settings, self, only=only)
            if not modal(dialog):
                return
            old_stem = Path(str(config.settings.get("path", "") or "")).stem
            config.settings = {**config.settings, **dialog.values()}
            new_stem = Path(str(config.settings.get("path", "") or "")).stem
            if new_stem and config.name in (old_stem, old_stem.replace("_", " ")):  # named after its file: follows it
                config.name = new_stem.replace("_", " ")
        else:
            return
        self._changed()
        self._refresh_audio()
        self._refresh_scenes()
        if self.selected_sound_id == source_id:
            self._open_build_sound(source_id)

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
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(QCursor.pos())
        menu.deleteLater()  # a new menu each time: kept, each held a window handle for good

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
            self._refresh_audio_soon()

    def _on_audio_changed(self, source_id: str, changes: dict) -> None:
        """A mixer control changed: volume_db, muted, monitor, hidden or duck_voice."""
        config = self.collection.source(source_id)
        config.settings = {**config.settings, **changes}
        self._changed()
        if "hidden" in changes or "duck_voice" in changes or "muted" in changes:
            self._refresh_audio_soon()
        if changes.get("hidden"):
            self.statusBar().showMessage(f"Hid {config.name!r} from the mixer. It still plays.", 5000)

    def _open_ducking(self, parent: QWidget | None = None) -> None:
        # Owned by a window, never a widget inside one (Settings → Audio's page): owned by the page it was missing
        # for Windows' UI Automation (PC 2026-11-20).
        dialog = DuckingDialog(self.collection, parent.window() if parent is not None else self)
        if modal(dialog):
            dialog.apply_to(self.collection)
            self._changed()
            self._refresh_audio()

    def _pick_audio_device(self, kind: str, current: str = "default", title: str = "",
                           primary: str = "OK") -> str | None:
        devices = list_audio_devices(loopback=(kind == "audio_output"))
        if not devices:
            msgbox.information(self, "SprigCast", "Audio capture is only available on Windows.")
            return None
        names = ["default"] + [d.name for d in devices]
        if current not in names:  # not plugged in right now: still listed, and picked
            names.append(current)
        labels = ["Default (follows Windows)"] + [sound_label(n) for n in names[1:]]
        start = names.index(current) if current in names else 0
        title = title or ("Desktop audio" if kind == "audio_output" else "Microphone")
        at = popups.ask_choice(self, title, "Device", labels, start, primary)
        return None if at is None else names[at]

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

    def _edit_filters(self) -> None:
        item = self._item()
        if item is None:
            msgbox.information(self, "SprigCast", "Select a source first.")
            return
        self._open_filters(item.source)

    def _open_filters(self, source_id: str, media: str | None = None) -> None:
        sounds = sorted(((c.id, c.name) for c in self.collection.sources if self._has_sound(c)),
                        key=lambda pair: pair[1].lower())
        from sprigcast.app.filters_dialog import EngineGrab

        engine = getattr(self, "engine", None)
        grab = None if engine is None else EngineGrab(engine, self.collection)
        hear = None
        if engine is not None and getattr(engine, "mixer", None) is not None:
            from sprigcast.audio.preview import Preview

            device = self.collection.audio.monitor_device or None  # Listen through, as the sound card's headphones

            def hear(sid: str, filters: list) -> Preview:
                return Preview(sid, filters, device=device)
        modal(FiltersDialog(self.collection.source(source_id), self._changed, self, media, sounds=sounds,
                            collection=self.collection, grab=grab, hear=hear))
        if self._has_sound(self.collection.source(source_id)):
            self._refresh_audio_soon()  # the Sound card lists filter names
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
        if self.scene_tree.holding:  # a press may still turn into a drag: open the row on release
            self._open_on_release = True
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
                self._show_sound_where()
            if self.in_place is not None:
                self.in_place = None
                self.engine.editing_part = None
                self._refresh_previews()
            self.selected_scene_id = scene_id
            self._refresh_sources()
            self._show_part_editor()

    def _list_let_go(self) -> None:
        """A click on the list ended without a drag: now open the row it picked."""
        if self._open_on_release:
            self._open_on_release = False
            self._on_scene_selected()

    def _list_drag_ended(self) -> None:
        """A drag from the list is over: the row stays shut and the list points back at what's being edited."""
        if self._open_on_release:
            self._open_on_release = False
            QTimer.singleShot(0, self._refresh_scenes)  # after the drop has been read back from the rows

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
        name, ok = popups.ask_text(self, title, "Name", primary="Add")
        if not ok or not name.strip():
            return
        linked = None
        shows = [s for s in self.collection.scenes if s.kind == "show" and s.canvas != canvas_id]
        if canvas_id != self.collection.canvases[0].id and shows:
            link_options = ["(not linked)"] + [s.name for s in shows]
            # Suggest the main-canvas bloom with the same name, if there is one.
            guess = next((i + 1 for i, s in enumerate(shows) if s.name.lower() == name.strip().lower()), 0)
            at = popups.ask_choice(self, title, "Switch together with", link_options, guess, "Add")
            if at is None:
                return
            linked = shows[at - 1].id if at else None
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
            msgbox.information(self, "SprigCast", "Select a scene first.")
            return
        if self.collection.contains(part, scene.id):
            msgbox.information(self, "SprigCast", f"{part.name!r} already contains this scene, so it can't go inside it.")
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

        from sprigcast.core.device_makers import is_effects

        name = str(config.settings.get("name", ""))
        return (config.kind == "camera" and is_virtual(name) and not is_effects(name)  # effects are the point
                and not QSettings().value(f"tips/vcam_hidden/{config.id}", False, type=bool))

    def _hide_vcam_tip(self) -> None:
        item = self._item()
        if item is not None:
            QSettings().setValue(f"tips/vcam_hidden/{item.source}", True)
        self.prop_vcam.hide()

    def _fill_swap_camera_menu(self, menu: QMenu, source_id: str) -> None:
        """Every other camera (NVIDIA Broadcast, Streamfog, Camera Hub...): one click swaps the device and
        keeps the placement, filters and size."""
        config = self.collection.source(source_id)
        here = str(config.settings.get("path", "")), str(config.settings.get("name", ""))
        others = [c for c in list_cameras() if not ((c.path and c.path == here[0]) or c.name == here[1])]
        for cam in others:
            menu.addAction(literal(camera_label(cam.name))).triggered.connect(
                lambda _=False, c=cam, sid=source_id: self._swap_camera(sid, c))
        if not others:
            menu.addAction("No other camera").setEnabled(False)

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
        self._swap_camera(config.id, device)

    def _swap_camera(self, source_id: str, device) -> None:
        """Point a camera source at another device, keeping its size and frame rate where that device can."""
        config = self.collection.source(source_id)
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
        if self.selected_sound_id != source_id and hasattr(self, "build_tune_scroll"):
            self.build_tune_scroll.verticalScrollBar().setValue(0)  # another sound: from its top
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
        theme.ui_burst()  # (as modal() does: a menu coming up builds its window)
        menu.exec(QCursor.pos())
        menu.deleteLater()  # a new menu each time: kept, each held a window handle for good

    def _close_build_sound(self, refresh: bool = True) -> None:
        self.selected_sound_id = None
        self.sound_strips.set_selected(None)
        self.build_stage.right_stack.setCurrentIndex(0)
        if refresh:
            self._refresh_scenes()

    def _build_tune_changed(self, source_id: str, changes: dict) -> None:
        self._on_sound_changed(source_id, changes)
        self._refresh_audio_soon()  # Live's Sound card shows the same sound
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
        if modal(dialog):
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
        name, ok = popups.ask_text(
            self, f"Turn into {a(PART)}", f"{cap(PART)} name", first if len(rows) == 1 else "", "Make")
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
        if self.part_preview.canvas_id != f"part:{scene.id}":
            self.part_preview._image = None
        self.part_preview.canvas_id = f"part:{scene.id}"
        self.part_preview.title = scene.name
        self.part_preview.aspect = (canvas.width, canvas.height)
        self.part_preview.output_size = lambda c=canvas.id: self.collection.canvas(c).output_size()
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
        if msgbox.question(self, "Remove layout", question) != QMessageBox.StandardButton.Yes:
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
        name, ok = popups.ask_text(self, "Rename scene", "Scene name", scene.name, "Rename")
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
            msgbox.information(self, "SprigCast", "Each canvas needs at least one scene.")
            return
        question = f"Remove {scene.name!r}?"
        going = self.collection.would_remove([scene])
        if going:
            question += f"\n\nThese go with it (nothing else uses them): {', '.join(going)}."
        if msgbox.question(self, "Remove scene", question) != QMessageBox.StandardButton.Yes:
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
        going = self.collection.would_remove(self.collection.shapes(part))
        if going:
            question += f"\n\nThese go with it (nothing else uses them): {', '.join(going)}."
        if msgbox.question(self, f"Remove {PART}", question) != QMessageBox.StandardButton.Yes:
            return
        self.collection.remove_sprig(part)
        self.selected_scene_id = self.collection.active_scenes.get(self.collection.canvases[0].id)
        self._changed()
        self._refresh_all()
        self._show_part_editor()

    # Source actions --------------------------------------------------------------

    def _add_item(self, config: SourceConfig) -> None:
        scene = self._scene()
        if scene is None:
            msgbox.information(self, "SprigCast", "Select a scene first.")
            return
        if scene.kind == "show" and config.kind != PART_KIND:  # blooms hold sprigs only: it goes into the bloom's sprig, opened to edit
            scene = self._open_bloom_sprig(scene)
        self.collection.sources.append(config)
        scene.items.append(SceneItem(source=config.id))
        if config.kind not in (*self.AUDIO_KINDS, "text"):
            self._start_sizes[config.id] = scene.id  # 1280 x 720 once its picture's size is known
        self._changed()
        self._refresh_sources(keep_row=0)

    START_BOX = (1280, 720)  # every new source but text starts this size on a 1080p canvas (user, 2026-11-19)

    def _place_new_sources(self, sizes: dict) -> None:
        """New sources whose size just became known: fitted into START_BOX (scaled with the canvas, kept
        inside it, or inside the part of the canvas a small sprig covers), centred. Text keeps its own size."""
        for source_id, scene_id in list(self._start_sizes.items()):
            size = sizes.get(source_id)
            if not size or not size[0] or not size[1]:
                continue
            del self._start_sizes[source_id]
            scene = next((s for s in self.collection.scenes if s.id == scene_id), None)
            item = next((i for i in scene.items if i.source == source_id), None) if scene is not None else None
            if item is None:
                continue
            canvas = self.collection.canvas(scene.canvas)
            k, vx, vy, left, top, width, height = self.collection.view_region(scene)
            share = min(canvas.width, canvas.height) / 1080
            box_w, box_h = min(self.START_BOX[0] * share, width), min(self.START_BOX[1] * share, height)
            w, h = item.cropped_size(size)
            if not w or not h:
                continue
            fit = min(box_w / w, box_h / h)
            item.scale, item.scale_y, item.rotation = fit / k, None, 0.0
            item.x = round((left + (width - w * fit) / 2 - vx) / k, 2)
            item.y = round((top + (height - h * fit) / 2 - vy) / k, 2)
            self._changed()
            if self._item() is item:
                self._refresh_properties()

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

    def _existing_of_kind(self, kind: str) -> list:
        """+ Add's list for a kind: the leaves (or sounds) of that kind already in the garden, A to Z."""
        here = {i.source for i in scene.items} if (scene := self._scene()) is not None else set()
        placed = {i.source for s in self.collection.scenes for i in s.items}
        if kind in self.AUDIO_KINDS:
            found = [c for c in self.collection.sources if c.kind == kind and c.settings.get("capture_of") is None]
        elif kind in ("media:sound", "media:video"):  # placed files of that kind
            sound = kind == "media:sound"
            found = [c for c in self.collection.sources if c.kind == "media" and c.id in placed and c.id not in here
                     and (c.settings.get("only") == "sound" if not c.settings.get("path")
                          else is_sound_file(str(c.settings["path"]))) == sound]
        else:
            found = [c for c in self.collection.sources if c.kind == kind and c.id in placed and c.id not in here]
        return sorted(found, key=lambda c: c.name.lower())

    def _fill_add_kind_menu(self, sub: QMenu, kind: str, label: str) -> None:
        """One kind in + Add: New…, then what you already have of it (a leaf goes in as a mirror: the same
        leaf again, nothing new running; a sound opens its controls in Sound)."""
        sub.addAction(icons.icon("plus", theme.TEXT, 16), "New…").triggered.connect(
            lambda _=False: self._add_new(kind, label))
        found = self._existing_of_kind(kind)
        if not found:
            return
        sub.addSeparator()
        sound = kind in self.AUDIO_KINDS
        for config in found:
            act = sub.addAction(literal(config.name))
            if sound:
                act.triggered.connect(lambda _=False, sid=config.id: QTimer.singleShot(0, lambda: self._show_sound(sid)))
            else:
                act.triggered.connect(lambda _=False, sid=config.id: self._add_mirror(sid))

    def _show_sound(self, source_id: str) -> None:
        if self.mode == "build":
            self._open_build_sound(source_id)

    def _add_new(self, kind: str, label: str) -> None:
        """+ Add → a kind → New…: the leaf goes in, then its picker opens (after the menu has closed), so you
        choose the screen, window, camera or file right away. Cancel takes the leaf out again."""
        before = {c.id for c in self.collection.sources}
        self._add_blank(kind, label)
        new = [c for c in self.collection.sources if c.id not in before and c.kind not in self.AUDIO_KINDS]
        if not new or not new[0].settings.get("blank"):
            return
        source_id = new[0].id

        def pick() -> None:
            item = self._item()
            if item is None or item.source != source_id:
                return
            if not self._edit_source_settings():  # Cancel leaves nothing behind (PC test of #796)
                item = self._item()
                if item is not None and item.source == source_id:
                    self._remove_item()
                    self.statusBar().clearMessage()

        QTimer.singleShot(0, pick)

    def _add_blank(self, kind: str, label: str) -> None:
        """+ Add: the leaf goes in at once, named after the entry; double-click it to pick its screen, window,
        camera, device or file (user, 2026-11-20). Sounds go in Sound, ready to use with Windows' default."""
        if kind == "audio_input" or kind == "audio_output":
            has_default = any(c.kind == kind and str(c.settings.get("device", "default")) == "default"
                              and not self.collection.device_off(c) for c in self.collection.sources)
            self._add_audio(kind, None if has_default else "default")  # Windows' default first, then pick one
            return
        if kind == "chat":
            self._add_chat()
            return
        if kind == "app_audio":
            config = SourceConfig(id=self.collection.new_id("sound-custom"), kind="app_audio", name=label,
                                  settings={"preset": "custom", "apps": [], "volume_db": 0.0, "muted": False,
                                            "blank": True})
            self.collection.sources.append(config)
            self._changed()
            self._refresh_audio()
            self._refresh_scenes()
            self.statusBar().showMessage(f"{label} is in Sound. Pick its apps there.", 6000)
            if self.mode == "build":
                self._open_build_sound(config.id)
            return
        scene = self._scene()
        if scene is None:
            msgbox.information(self, "SprigCast", "Select a scene first.")
            return
        settings: dict = {"blank": True}
        if kind in ("media:video", "media:sound"):  # a leaf like the rest: plays while its bloom is on air
            settings.update(path="", volume_db=0.0, muted=False, loop=False, only=kind.split(":")[1])
            kind = "media"
        elif kind == "display_capture":
            settings.update(monitor=1, cursor=True)
        elif kind == "game_capture":
            settings.update(mode="any", cursor=False)
        elif kind == "window_capture":
            settings.update(title="", process="", cursor=True)
        elif kind == "camera":
            settings.update(name="", path="")
        elif kind == "image":
            settings.update(path="")
        elif kind == "color":
            canvas = self.collection.canvas(scene.canvas)
            settings.update(color=theme.ACCENT + "ff", width=canvas.width, height=canvas.height)
        else:  # text, browser: their own starting settings
            cls = source_class(kind)
            settings.update(dict(getattr(cls, "defaults", {}) or {}))
        config = SourceConfig(id=self.collection.new_source_id(kind), kind=kind, name=label, settings=settings)
        self._add_item(config)
        if kind == "game_capture":
            from sprigcast.core.capture_sound import APP, set_capture_sound

            set_capture_sound(self.collection, config, APP)  # like OBS's Capture audio: the game's own sound
            self._changed()
            self._refresh_audio()
        self.statusBar().showMessage(f"{label} added. Double-click it in Leaves to choose what it shows.", 6000)

    def _name_after_pick(self, config, label: str) -> None:
        """A leaf added from + Add keeps its entry's name until you first pick what it shows: then it's named
        after that (the screen, window, camera or file), unless you renamed it meanwhile."""
        settings = dict(config.settings)
        settings.pop("blank", None)
        config.settings = settings
        if config.name != label:
            return
        kind = config.kind
        if kind == "display_capture":
            config.name = f"Monitor {settings.get('monitor', 1)}"
        elif kind in ("window_capture", "game_capture") and (settings.get("title") or settings.get("process")):
            config.name = window_name(str(settings.get("title", "")), str(settings.get("process", "")))
        elif kind == "camera" and settings.get("name"):
            config.name = short_name(str(settings["name"]), "Camera")
        elif kind in ("image", "media") and settings.get("path"):
            config.name = short_name(Path(str(settings["path"])).stem.replace("_", " "), label)
        elif kind == "browser":
            from sprigcast.sources.browser import page_name

            config.name = page_name(settings) or label

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
            msgbox.information(self, "SprigCast", "Select a scene first.")
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
            msgbox.information(self, "SprigCast", "Browser sources need Qt WebEngine, which isn't installed.")
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
            msgbox.information(self, "SprigCast", "Select a scene first.")
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
        elif kind == "text":
            from sprigcast.app.text_leaf_dialog import TextLeafDialog

            dialog = TextLeafDialog(f"New {name}", defaults, self, primary="Save")
        else:
            dialog = SettingsDialog(f"New {name}", defaults, defaults, self, cls=cls, collection=self.collection)
        if not modal(dialog):
            return
        settings = dialog.values()
        scene = self._scene()
        config = SourceConfig(id=self.collection.new_source_id(kind), kind=kind, name=_name_from(settings, name),
                              settings=settings)
        size = getattr(cls, "own_sprig", None)  # overlays like Snap: a sprig of their own, filling the canvas
        if size:  # on top of what you're editing: the bloom, or the sprig you have open
            self.collection.sources.append(config)
            self.collection.own_sprig(scene, config.id, tuple(size), config.name,
                                      getattr(cls, "own_sprig_corner", None), getattr(cls, "own_sprig_height", None),
                                      getattr(cls, "own_sprig_band", None))
            self._changed()
            self._refresh_all()
            return
        self._add_item(config)
        scene = self._scene() or scene  # a bloom sends it into its sprig: place it there
        item = next((i for i in scene.items if i.source == config.id), None)
        if item is None:
            return
        canvas = self.collection.canvas(scene.canvas)
        # In a sprig shown small in its bloom, start it the size it would be on the canvas, in the part of the
        # canvas the sprig covers (else it comes out tiny where it shows).
        k, vx, vy, left, top_, width, height = self.collection.view_region(scene)
        if fills:
            w, h = int(settings.get("width") or canvas.width), int(settings.get("height") or canvas.height)
            fit = min(width / max(1, w), height / max(1, h))
            item.scale = fit / k
            item.scale_y = None
            item.x = (left + (width - w * fit) / 2 - vx) / k
            item.y = (top_ + (height - h * fit) / 2 - vy) / k
            self._changed()
            self._refresh_properties()
            return
        # Start each new one a little lower so several don't pile up in the corner.
        stacked = sum(1 for i in scene.items if i is not item and self.collection.source(i.source).kind == kind)
        short = min(canvas.width, canvas.height)
        if getattr(cls, "sharp", False):  # text: the same share of the canvas at 720p, 1080p or 4K (and crisp)
            item.scale, item.scale_y = short / 1080 / k, None
        whole = (k, vx, vy) == (1.0, 0.0, 0.0)
        top = canvas.height * (PORTRAIT_TOP if canvas.height > canvas.width else 0.05) if whole \
            else top_ + height * 0.05  # below TikTok's tabs
        x = round(canvas.width * 0.05) if whole else left + width * 0.05
        y = top + (stacked * 100 * short / 1080) % max(1, (canvas.height if whole else height) // 2)
        item.x, item.y = round((x - vx) / k, 2), round((y - vy) / k, 2)
        self._changed()
        self._refresh_properties()

    def _fill_more_menu(self) -> None:
        """List source kinds from plugins (any registered class with a `label` that has no button of its own).

        The + menu (and its Plugins menu) is only built the first time it opens, and fills itself then: until
        that, a plugin installed or updated has no menu to refresh (PC test 2026-11-16: "no attribute
        'more_menu'" on every install)."""
        menu = getattr(self, "more_menu", None)
        if menu is None:
            return
        try:
            menu.actions()
        except RuntimeError:  # the + menu was rebuilt: the new Plugins menu fills itself when it opens
            return
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
        self.more_menu.addAction("Plugin store…").triggered.connect(lambda: self.open_plugin_store())
        folder = self.more_menu.addAction("Open plugins folder…")
        folder.setEnabled(self.plugins_dir is not None)
        folder.triggered.connect(self._open_plugins_folder)

    def _open_plugins_folder(self) -> None:
        if self.plugins_dir is None:
            return
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        open_folder(self.plugins_dir)

    def _add_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
        if not path:
            return
        self._add_item(
            SourceConfig(id=self.collection.new_id("image"), kind="image",
                         name=ask_name(self, short_name(Path(path).stem.replace("_", " "), "Image"), Path(path).stem),
                         settings={"path": path})
        )

    def _add_media(self, sound: bool = False, video: bool = False) -> None:
        """A video goes into the selected bloom like a picture; a sound file goes into Sound (every bloom).
        + Add lists them apart (user, 2026-11-20): Video file picks videos (played with their sound), Sound file
        picks sound files. With neither, either kind of file (older callers)."""
        only = "sound" if sound else "video" if video else None
        title = {"sound": "Choose a sound file", "video": "Choose a video"}.get(only, "Choose a video or sound file")
        path, _ = QFileDialog.getOpenFileName(self, title, "", media_file_filter(only))
        if not path:
            return
        stem = Path(path).stem
        settings = {"path": path, "volume_db": 0.0, "muted": False, "loop": False}
        fallback = {"sound": "Sound", "video": "Video"}.get(only, "Media")
        config = SourceConfig(id=self.collection.new_id("media"), kind="media",
                              name=ask_name(self, short_name(stem.replace("_", " "), fallback), stem),
                              settings=settings)
        if is_sound_file(path):
            self.collection.sources.append(config)
            self._changed()
            self._say(f"{config.name!r} is in Sound. Right-click it to play.")
        else:
            self._add_item(config)
        # The Sound strip and Build's list catch up on the next turns, so the click that added it answers at once:
        # doing both here held the window ~0.3 s on the PC (2026-11-20) with a garden of 89 blooms.
        self._refresh_audio_soon()
        QTimer.singleShot(1, self, self._refresh_scenes)

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
        """Right-click entries plugins add for their own sources (Add a minute, Reset to 0, Stop replay...).
        Effects on one item (Pixelate, Black out, Freeze) are not listed: they're filters (user, 2026-11-20)."""
        entries: list[tuple[str, object]] = []
        plugin_hooks.fill_item_menu(self.api, self.collection.source(source_id),
                                    lambda label, fn: entries.append((label, fn)))
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

    def _display_dialog(self, settings: dict) -> dict | None:
        """Screen capture's card: which screen and the mouse pointer. None when cancelled or not on Windows."""
        from sprigcast.app.capture_dialog import DisplayCaptureDialog

        monitors = list_monitors()
        if not monitors:
            msgbox.information(self, "SprigCast", "Display capture is only available on Windows.")
            return None
        dialog = DisplayCaptureDialog(settings, monitors, self)
        return dialog.values() if modal(dialog) else None

    def _window_dialog(self, settings: dict, has_sound: bool) -> tuple[dict, bool] | None:
        """Window capture's card: (settings, window sound on), or None when cancelled or not on Windows."""
        from sprigcast.app.capture_dialog import WindowCaptureDialog

        windows = list_windows()
        if not windows and not settings.get("title"):
            msgbox.information(self, "SprigCast", "Window capture is only available on Windows.")
            return None
        dialog = WindowCaptureDialog(settings, windows, has_sound, self, list_windows=list_windows,
                                     list_own=lambda: list_windows(include_own=True))
        return dialog.values() if modal(dialog) else None

    def _add_display(self, monitor=None) -> None:
        scene = self._scene()
        if scene is None:
            msgbox.information(self, "SprigCast", "Select a scene first.")
            return
        settings = {"monitor": monitor.index, "cursor": True} if monitor is not None else \
            self._display_dialog({"monitor": 1, "cursor": True})
        if settings is None:
            return
        if monitor is None:
            monitor = next((m for m in list_monitors() if m.index == settings["monitor"]), None)
            if monitor is None:
                return
        config = SourceConfig(
            id=self.collection.new_id("display"),
            kind="display_capture",
            name=f"Monitor {monitor.index}",
            settings=settings,
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
            msgbox.information(self, "SprigCast", "Select a scene first.")
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
            msgbox.information(self, "SprigCast", "Select a scene first.")
            return
        sound = False
        if window is not None:
            settings = {"title": window.title, "process": window.process, "cursor": True}
        else:
            picked = self._window_dialog({"cursor": True}, False)
            if picked is None:
                return
            settings, sound = picked
        title, process = str(settings["title"]), str(settings["process"])
        name = ask_name(self, window_name(title, process), title)
        config = SourceConfig(id=self.collection.new_id("window"), kind="window_capture", name=name,
                              settings=settings)
        self._add_item(config)
        if sound:
            from sprigcast.core.capture_sound import APP, set_capture_sound

            set_capture_sound(self.collection, config, APP)
            self._changed()
            self._refresh_audio()

    def _camera_max_fps(self) -> int:
        rates = [int(round(c.fps)) for c in self.collection.canvases if c.enabled]
        return max(rates) if rates else 60

    def _camera_dialog(self, current: dict | None = None, source_id: str | None = None) -> dict | None:
        cameras = list_cameras()
        if not cameras:
            msgbox.information(self, "SprigCast", "No cameras found. Is one plugged in and not in use by another app?")
            return None
        source = self.engine._sources.get(source_id) if source_id else None
        modes = getattr(source, "modes_seen", None) or None
        dialog = CameraDialog(cameras, current, self, max_fps=self._camera_max_fps(), modes=modes)
        return dialog.settings() if modal(dialog) else None

    def _add_camera(self, device=None) -> None:
        """Add a camera: pick one and its mode, or (from + Add's list) that camera at its usual mode."""
        scene = self._scene()
        if scene is None:
            msgbox.information(self, "SprigCast", "Select a scene first.")
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
        config = SourceConfig(id=self.collection.new_source_id("camera"), kind="camera", name=name, settings=settings)
        self._add_item(config)
        self._pair_capture_card_sound(config)
        # Where it goes: 1280 x 720 in the middle once its picture arrives, like every source (_place_new_sources).

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
            ("fill", "Fill canvas (crop edges)", "Ctrl+Alt+F", self._fill_item),
            ("stretch", "Stretch to canvas", "Ctrl+Shift+F", self._stretch_item),
            ("center", "Center on canvas", "Ctrl+E", self._center_item),
            ("reset", "Reset transform", "Ctrl+R", self._reset_item_size),
            ("uncrop", "Remove crop", "Ctrl+Shift+R", self._reset_crop),
            ("front", "Bring to front", "Ctrl+Shift+Up", lambda: self._to_edge(True)),
            ("up", "Bring forward", "Ctrl+Up", lambda: self._move_item(+1)),
            ("down", "Send backward", "Ctrl+Down", lambda: self._move_item(-1)),
            ("back", "Send to back", "Ctrl+Shift+Down", lambda: self._to_edge(False)),
            ("hide", "Hide", "Ctrl+H", self._toggle_item_visible),
            ("lock", "Lock", "Ctrl+L", lambda: self.prop_locked.setChecked(not self.prop_locked.isChecked())),
            ("settings", "Properties…", "Ctrl+Return", self._edit_source_settings),
            ("filters", "Filters…", "Ctrl+Shift+E", self._edit_filters),
            ("rename", "Rename…", "F2", self._rename_item),
            ("remove", "Remove", "Delete", self._remove_item),
        )
        for key, text, shortcut, slot in table:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.setShortcutVisibleInContextMenu(True)
            if key in ("settings", "filters", "rename"):  # windows: opened once a right-click menu has closed
                action.triggered.connect(lambda _=False, f=slot: QTimer.singleShot(0, self, f))
            else:
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

    def _edit_source_settings(self) -> bool:
        """Double-click or Properties: the leaf's pop-up. False when it was cancelled (or there was none)."""
        item = self._item()
        if item is None:
            return False
        config = self.collection.source(item.source)
        if config.kind == PART_KIND:
            self._open_part_item()
            return False
        blank = config.settings.get("blank") and next((k for k, v in BLANK_KINDS.items() if config.name == k), None)
        before = dict(config.settings)
        if config.kind == "app_audio":
            self._pick_app_sound_apps(config.id)
            return False
        if config.kind == "color":
            current = QColor(str(config.settings.get("color", "#000000"))[:7])
            color = QColorDialog.getColor(current, self, "Color", QColorDialog.ColorDialogOption.ShowAlphaChannel)
            if not color.isValid():
                return False
            config.settings = {
                **config.settings,
                "color": f"#{color.red():02x}{color.green():02x}{color.blue():02x}{color.alpha():02x}",
            }
        elif config.kind == "image":
            path, _ = QFileDialog.getOpenFileName(self, "Choose image", "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)")
            if not path:
                return False
            config.settings = {**config.settings, "path": path}
        elif config.kind == "display_capture":
            changes = self._display_dialog(config.settings)
            if changes is None:
                return False
            if config.name == f"Monitor {config.settings.get('monitor', 1)}":  # a name you chose stays
                config.name = f"Monitor {changes['monitor']}"
            config.settings = {**config.settings, **changes}
        elif config.kind == "camera":
            settings = self._camera_dialog(config.settings, config.id)
            if settings is None:
                return False
            old = str(config.settings.get("name", ""))
            config.settings = settings
            if config.name in (old, short_name(old, "Camera")):  # a name you chose stays
                config.name = short_name(settings["name"], "Camera")
        elif config.kind == "game_capture":
            from sprigcast.app.game_dialog import GameCaptureDialog
            from sprigcast.core.capture_sound import APP, capture_sound, follow_capture, set_capture_sound

            dialog = GameCaptureDialog(config.settings, list_windows(), capture_sound(self.collection, config.id)
                                       is not None, self, list_windows=list_windows)
            if not modal(dialog):
                return False
            changes, sound = dialog.values()
            config.settings = {**config.settings, **changes}
            if sound != (capture_sound(self.collection, config.id) is not None):
                set_capture_sound(self.collection, config, APP if sound else None)
            follow_capture(self.collection, config)
            self._refresh_audio()
        elif config.kind in ("audio_input", "audio_output"):
            # Pick the device, as + Add did (user 2026-11-20: double-click said "no settings to edit").
            old = str(config.settings.get("device", "default") or "default")
            device = self._pick_audio_device(config.kind, old, config.name, "Save")
            if device is None or device == old:
                return False
            if sound_name_is_ours(config.name, config.kind, old):  # a name you chose stays
                config.name = sound_name(config.kind, device)
            config.settings = {**config.settings, "device": device}
            self._refresh_audio()
        elif config.kind == "window_capture":
            from sprigcast.core.capture_sound import APP, capture_sound, follow_capture, set_capture_sound

            had_sound = capture_sound(self.collection, config.id) is not None
            picked = self._window_dialog(config.settings, had_sound)
            if picked is None:
                return False
            changes, sound = picked
            old = window_name(str(config.settings.get("title", "")), str(config.settings.get("process", "")))
            old_title = str(config.settings.get("title", ""))
            config.settings = {**config.settings, **changes}
            if config.name in (old, old_title, old_title[:37] + "…"):  # a name you chose stays
                config.name = window_name(str(changes["title"]), str(changes["process"]))
            if sound != had_sound:
                set_capture_sound(self.collection, config, APP if sound else None)
            follow_capture(self.collection, config)  # its own app sound follows the new window
            self._refresh_audio()
        else:
            cls = source_class(config.kind)
            defaults = getattr(cls, "defaults", None) if cls is not None else None
            if not defaults:
                msgbox.information(self, "SprigCast", f"{config.kind!r} sources have no settings to edit.")
                return False
            if config.kind == "browser":
                from sprigcast.app.web_page_dialog import WebPageDialog

                host = self.browser_host
                dialog = WebPageDialog(config.name, config.settings, self,
                                       on_reload=(lambda: host.reload(config.id)) if host else None)
            elif config.kind == "media":
                from sprigcast.app.media_dialog import MediaDialog

                dialog = MediaDialog(config.name, config.settings, self, only=config.settings.get("only"))
            elif config.kind == "text":
                from sprigcast.app.text_leaf_dialog import TextLeafDialog

                dialog = TextLeafDialog(config.name, config.settings, self)
            else:
                dialog = SettingsDialog(config.name, defaults, config.settings, self, cls=cls,
                                        collection=self.collection)
            if not modal(dialog):
                return False
            old_auto = _name_from(config.settings, config.name)
            config.settings = {**config.settings, **dialog.values()}  # volume, mute... aren't in the form
            if config.name == old_auto:  # the name followed the text, so keep it following
                config.name = _name_from(config.settings, config.name)
        if blank and config.settings != before:
            self._name_after_pick(config, blank)
        self._changed()
        self._refresh_sources(keep_row=self.source_list.currentRow())
        return True

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
                    as_data: bool = False, background: bool = False) -> list[dict]:
        """A PNG of what's on air in each shape (or just shapes), in the recordings folder's Screenshots.

        background (the buttons and hotkey): grab and save on a worker and return [] at once; the note and
        the plugins' screenshot hook follow when the files are written."""
        from sprigcast.app import screenshot

        ids = [c.id for c in self.collection.canvases if c.enabled and (not shapes or c.id in shapes)]
        folder = self._recordings_folder() / "Screenshots" if save else None
        # Plugins get ready first (Snap freezes the screen, so the picture is the moment it holds on stream).
        afters = plugin_hooks.screenshot_starting(self.api, ids) if save else []
        if background and save and not as_data:
            screenshot.take_in_background(self.engine, ids, folder, width,
                                          lambda shots, images: self._screenshot_saved(shots, images, folder, afters))
            return []
        from sprigcast.core import workers

        images: dict = {}
        try:
            # Grabbed and made into PNGs on a worker, the window drawing and answering meanwhile: the Control
            # API's screenshots (Streamer.bot, Stream Deck) waited for a frame and encoded on the window's thread
            # (54 ms+ on the PC while live, 2026-11-18).
            shots = workers.wait_answering(screenshot.take, self.engine, ids, folder, width, as_data, images)
        except Exception:
            plugin_hooks.screenshot_done(afters, [])
            raise
        if save:
            self._screenshot_saved(shots, images, folder, afters)
        return shots

    def _source_screenshot(self, source_id: str, name: str, width: int | None = None, save: bool = True,
                           as_data: bool = False) -> dict | None:
        """A PNG of one source or sprig drawn on its own (apps: screenshot.take with source or bloom)."""
        from sprigcast.app import screenshot

        from sprigcast.core import workers

        folder = self._recordings_folder() / "Screenshots" if save else None
        shot = workers.wait_answering(screenshot.take_one, self.engine, source_id, name, folder, width, as_data)
        if shot is not None and save:
            self._saved_note("Screenshot saved", Path(shot["path"]).parent)
            self._tidy_saves()
        return shot

    def _bloom_shot_for_apps(self, scene_id: str, name: str, width: int | None = None, save: bool = True,
                             as_data: bool = False) -> dict | None:
        """A PNG of a bloom that isn't on air, on its own canvas (apps: screenshot.take with bloom). Nothing on
        air changes; its sources wake for the moment it takes (see screenshot.grab_scene)."""
        from sprigcast.app import screenshot
        from sprigcast.core import workers

        scene = next((sc for sc in self.collection.scenes if sc.id == scene_id), None)
        if scene is None:
            return None
        folder = self._recordings_folder() / "Screenshots" if save else None
        shot = workers.wait_answering(screenshot.take_scene, self.engine, scene, name, folder, width, as_data)
        if shot is not None and save:
            self._saved_note("Screenshot saved", Path(shot["path"]).parent)
            self._tidy_saves()
        return shot

    # For the Control API (control/native.Later): the picture is made on a worker and handed back later; the
    # window's thread never waits for it (waiting there, events still handled, left the window not answering for
    # 514 ms on the PC, 2026-11-20).

    def _screenshot_later(self, shapes: list[str] | None, width: int | None, save: bool, as_data: bool):
        from functools import partial

        from sprigcast.app import screenshot
        from sprigcast.control.native import Later

        ids = [c.id for c in self.collection.canvases if c.enabled and (not shapes or c.id in shapes)]
        folder = self._recordings_folder() / "Screenshots" if save else None
        afters = plugin_hooks.screenshot_starting(self.api, ids) if save else []
        images: dict = {}

        def then(future):
            try:
                shots = future.result()
            except Exception:
                plugin_hooks.screenshot_done(afters, [])
                raise
            if save:
                self._screenshot_saved(shots, images, folder, afters)
            return shots

        return Later(partial(screenshot.take, self.engine, ids, folder, width, as_data, images), then)

    def _source_shot_later(self, source_id: str, name: str, width: int | None, save: bool, as_data: bool):
        from functools import partial

        from sprigcast.app import screenshot
        from sprigcast.control.native import Later

        folder = self._recordings_folder() / "Screenshots" if save else None

        def then(future):
            shot = future.result()
            if shot is not None and save:
                self._saved_note("Screenshot saved", Path(shot["path"]).parent)
                self._tidy_saves()
            return shot

        return Later(partial(screenshot.take_one, self.engine, source_id, name, folder, width, as_data), then)

    def _bloom_shots_later(self, scene_ids: list[str], name: str, width: int | None, save: bool, as_data: bool):
        """Blooms off air (each shape of one bloom), pictured together: one wait for their sources to wake."""
        from functools import partial

        from sprigcast.app import screenshot
        from sprigcast.control.native import Later

        scenes = [next((sc for sc in self.collection.scenes if sc.id == sid), None) for sid in scene_ids]
        folder = self._recordings_folder() / "Screenshots" if save else None

        def then(future):
            shots = future.result()
            saved = next((shot for shot in shots if shot is not None and shot.get("path")), None)
            if saved is not None:
                self._saved_note("Screenshot saved", Path(saved["path"]).parent)
                self._tidy_saves()
            return shots

        return Later(partial(screenshot.take_scenes, self.engine, scenes, name, folder, width, as_data), then)

    def _screenshot_saved(self, shots: list[dict], images: dict, folder, afters=()) -> None:
        try:
            self._say(f"Screenshot saved to {folder}" if shots else "Couldn't take a screenshot: nothing is drawing.")
            if shots:
                self._saved_note("Screenshot saved", self._shot_folder(shots, folder))
            self._tidy_saves()
            if shots:  # plugins such as Snap show it off
                plugin_hooks.screenshot_taken(self.api, [{**s, "image": images.get(s["shape"])} for s in shots])
        finally:
            plugin_hooks.screenshot_done(afters, shots)  # Snap lets the screen go if nothing prints

    def _saved_note(self, title: str, folder=None, **more) -> None:
        """A clear "saved" note over the window for a few seconds (a click opens the folder)."""
        note = getattr(self, "saved_note", None)
        if note is None:
            from sprigcast.app.empty import SavedNote

            note = self.saved_note = SavedNote(self)
        note.say(title, folder, **more)

    def _warn_note(self, text: str, ms: int) -> None:
        """A heads-up as a note over the window (the status bar cut long ones off): its first part as the
        title ("Your PC is busy (CPU 92%)"), the rest under it."""
        cut = max(text.find("): "), text.find("). "))
        title, detail = (text[:cut + 1], text[cut + 3:]) if cut > 0 else (text, "")
        self._saved_note(title, detail=detail, kind="warn", ms=ms)

    def _plugin_updates(self) -> None:
        """Say which store plugins updated ("Death count updated" + what's new), and put in updates held while
        live or recording (Tools → Plugin store). Waits while on air, checking every few seconds."""
        from sprigcast import plugin_store

        if not plugin_store.updated_at_start and not plugin_store.waiting:
            return
        if self.streaming is not None or self.recording is not None:
            QTimer.singleShot(5000, self._plugin_updates)
            return
        done = list(plugin_store.updated_at_start)
        plugin_store.updated_at_start.clear()
        held = list(plugin_store.waiting.values())
        plugin_store.waiting.clear()
        for plugin in held:
            if self.plugins_dir is None:
                break
            try:
                error = plugin_store.install(self.plugins_dir, plugin, self.plugin_report)
            except OSError as exc:
                error = str(exc)
            if error is None:
                done.append(plugin.id)
            else:
                log.warning("plugin store: held update of %s failed: %s", plugin.id, error)
        if held:
            self._fill_more_menu()
            self._send_whole()
        note = plugin_store.update_note(done)
        if note is not None:
            self._saved_note(note[0], detail=note[1], link="Plugins", on_click=lambda: self.open_plugin_store(),
                             ms=8000)

    @staticmethod
    def _shot_folder(shots: list[dict], folder):
        """The folder the first screenshot landed in (a dated one under Screenshots)."""
        path = shots[0].get("path")
        return Path(path).parent if path else folder

    def _tidy_saves(self) -> None:
        """Settings → Recording → Space: keep only the newest screenshots and clips, if asked."""
        from sprigcast.output import storage

        rec, root = self.collection.recording, self._recordings_folder()
        storage.keep_newest(root / "Screenshots", rec.keep_screenshots, storage.SCREENSHOT_TYPES)
        storage.keep_newest(root / "Clips", rec.keep_clips, storage.CLIP_TYPES)

    def _open_recordings_folder(self) -> None:
        folder = self._recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        open_folder(folder)

    def _on_record_clicked(self) -> None:
        if self.recording is not None and confirm_enabled(CONFIRM_RECORDING):
            answer = msgbox.question(self, "SprigCast", "Stop recording?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._toggle_recording()

    def _toggle_recording(self) -> None:
        if self.recording is None:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self, remote: bool = False, force: bool = False) -> dict:
        """Start recording. From an app (remote) nothing waits on a pop-up: a problem or the low-space warning
        comes back in the answer ({"problem"} / {"warnings"}), and force records anyway."""
        self._wait_for_stopped_recording()
        settings = self.collection.recording
        canvases = [c for c in self.collection.canvases if c.enabled and c.id in settings.canvases]
        if not canvases:
            if not remote:
                msgbox.information(self, "SprigCast", "Tick at least one canvas to record.")
            return {"problem": "Tick at least one canvas to record."}
        from sprigcast.output import storage

        # Room on the drive for every file this recording writes (each canvas, twice with source record).
        kbps = (settings.bitrate_kbps + settings.encoder.audio_kbps) * len(canvases) * (2 if settings.source_record else 1)
        self._space_watch = storage.SpaceWatch(self._recordings_folder(), kbps)
        space = self._space_watch.check()
        warnings: list[str] = []
        if space and (self._space_watch.left or 0) < 1:
            if remote:
                warnings = [space]
                if not force:
                    return {"warnings": warnings}
            else:
                answer = msgbox.question(self, "SprigCast", f"{space}\n\nRecord anyway?")
                if answer != QMessageBox.StandardButton.Yes:
                    return {"problem": "Canceled."}
        self._say("Starting the recording…")
        self._set_encoder_tracks()
        self._wake_up()
        try:
            borrowed = None
            if settings.same_as_stream and self.streaming is not None and hasattr(self.streaming, "encoders_by_canvas"):
                borrowed = self.streaming.encoders_by_canvas()
            self.recording = SharedRecording.start(
                self.encoders, canvases, self._recordings_folder(), settings.bitrate_kbps, settings.container,
                encoder=settings.encoder, short_side=settings.short_side, stream_encoders=borrowed,
                split_seconds=settings.split_minutes * 60, split_bytes=int(settings.split_gb * 1e9),
            )
        except Exception as exc:
            self.recording = None
            if not remote:
                msgbox.critical(self, "SprigCast", f"Could not start recording:\n{exc}")
            return {"problem": f"Could not start recording: {exc}"}
        plugin_hooks.output_changed("record", getattr(self.recording, "started_at", time.monotonic()))
        if settings.source_record:
            self._start_source_record(canvases)
        log.info("recording started: %s, %d kbps, %s, size %s, encoder %s preset %s",
                 ", ".join(c.id for c in canvases), settings.bitrate_kbps, settings.container,
                 f"{settings.short_side}p" if settings.short_side else "canvas", settings.encoder.rate_control,
                 settings.encoder.preset)
        self._chapters_marked = 0
        self.live_side.set_recording(True)
        self._refresh_recording_status()
        if space:
            log.warning("recording: %s", space)
            self.statusBar().showMessage(space, 20000)
        return {"started": True, "warnings": warnings}

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
        from sprigcast.app import features

        if on and self.replay is None and not features.on("clips"):
            self.statusBar().showMessage(features.OFF_NOTE["clips"], 5000)
            return
        if on and self.replay is None:
            settings = self.collection.recording
            canvases = self._clip_canvases()
            if not canvases:
                msgbox.information(self, "SprigCast", "Turn on a canvas first (Settings → Video).")
                return
            self._set_encoder_tracks()
            self._wake_up()
            ending = getattr(self, "_clips_ending", None)
            if ending is not None and ending.is_alive():
                ending.join(10)  # clips off and on again: the last encoder has stopped before the next one opens
            try:
                self.replay = SharedReplay.start(  # clips go in Clips/<day> (output/replay.py replay_path)
                    self.encoders, canvases, self._recordings_folder() / "Clips", settings.replay_seconds,
                    settings.bitrate_kbps, settings.container, encoder=settings.encoder,
                    short_side=settings.short_side, stream_encoders=self._clip_stream_encoders(),
                )
            except Exception as exc:
                msgbox.critical(self, "SprigCast", f"Could not get clips ready:\n{exc}")
                return
            self.statusBar().showMessage(f"Clips ready: Save clip keeps the last {settings.replay_seconds} s.", 4000)
            self._clips_used_at, self._clips_idle_paused = time.monotonic(), False
        elif not on and self.replay is not None:
            self._clips_ending = self.replay.stop()  # its encoder stops on a thread of its own, not here
            self.replay = None
            self._rest_soon()
        self.live_side.set_clip(self.replay is not None, self.collection.recording.replay_seconds)

    def _clip_stream_encoders(self) -> dict:
        """Canvas id -> the live stream's encoder clips may keep their seconds in ("Same as stream" on, and the
        clips track hearing exactly what the stream hears); empty when they keep their own encoder."""
        if not self.collection.recording.same_as_stream or os.environ.get("SPRIGCAST_OWN_CLIP_ENCODER"):
            return {}
        streaming = self.streaming
        if streaming is None or not hasattr(streaming, "encoders_by_canvas"):
            return {}
        mixer = getattr(self.engine, "mixer", None)
        same = getattr(mixer, "same_mix", None)
        if same is None or not same("stream", "clips"):
            return {}
        try:
            return streaming.encoders_by_canvas()
        except Exception:  # clips keep their own encoder
            log.exception("clips: asking the stream for its encoders failed")
            return {}

    def _follow_stream_with_clips(self) -> None:
        """About once a second: clips move to the stream's encoder while live and back after (SharedReplay.follow)."""
        replay = self.replay
        if replay is None or not hasattr(replay, "follow"):
            return
        try:
            replay.follow(self._clip_stream_encoders())
        except Exception:  # clips stay where they are
            log.exception("clips: moving between encoders failed")

    def _check_clips_idle(self) -> None:
        """Clips kept ready with nothing live or recording pause after a while without a clip saved; they
        come back by themselves when a stream or recording starts."""
        busy = self.streaming is not None or self.recording is not None
        now = time.monotonic()
        if busy:
            self._clips_used_at = now
            if self._clips_idle_paused and self.replay is None:
                self._clips_idle_paused = False
                self._toggle_replay(True)
            return
        minutes = self.collection.recording.clips_idle_minutes
        if self.replay is None or minutes <= 0 or now - self._clips_used_at < minutes * 60:
            return
        log.info("clips: paused after %d minutes unused (nothing live or recording)", minutes)
        self._toggle_replay(False)
        self._clips_idle_paused = True
        self.statusBar().showMessage(f"Clips paused to save memory: unused for {minutes} minutes. "
                                     "Press Clip, go live or record to get them ready again.", 15000)

    def _save_replay(self) -> list:
        """Save a clip of the last seconds; returns the files (none when clips are off or it failed)."""
        if self.replay is None:
            from sprigcast.app import features

            self.statusBar().showMessage(features.OFF_NOTE["clips"] if not features.on("clips") else
                                         "Press Clip first to get clips ready.", 4000)
            return []
        if not getattr(self, "_clip_written_wired", False):
            self._clip_written_wired = True
            self._clip_written.connect(self._clip_saved)
        try:
            # The files are written on a thread of their own (1.3 s with the window waiting on the PC,
            # 2026-11-17); _clip_saved says so when they're there. The names are known now.
            paths = self.replay.save_later(self._clip_written.emit)
        except Exception as exc:
            self.statusBar().showMessage(f"Couldn't save the replay: {exc}", 6000)
            return []
        self._clips_used_at = time.monotonic()
        self.statusBar().showMessage("Saving clip…", 4000)
        log.info("clips: saving a clip (%s)", ", ".join(Path(p).name for p in paths) or "no files")
        return paths

    def _clip_saved(self, paths: list, problems: list) -> None:
        """A clip's files are written (or couldn't be)."""
        if problems:
            self.statusBar().showMessage("Couldn't save the replay: " + "; ".join(problems), 6000)
            log.warning("clips: a clip couldn't be saved (%s)", "; ".join(str(p) for p in problems))
        if not paths:
            return
        names = ", ".join(p.name for p in paths)
        log.info("clips: clip saved (%s)", names)
        self.statusBar().showMessage(f"Saved clip: {names}", 8000)
        self._saved_note("Clip saved", paths[0].parent if paths else None)
        self._tidy_saves()
        self.live_side.clip_saved(len(paths))
        self.control.replay_saved(paths)

    def _stop_for_space(self) -> None:
        """The recordings drive is nearly full: stop now, files closed properly, rather than let it fill."""
        from sprigcast.output import storage

        free = storage.free_bytes(self._recordings_folder())
        log.warning("recording stopped: the drive is almost full (%s free)",
                    storage.size_text(free) if free is not None else "?")
        self._stop_recording(low_disk=True)
        self._saved_note("Recording stopped: the drive is almost full", self._recordings_folder(),
                         detail="Your files are saved. Free up space to record again.", kind="warn", ms=30000)

    def _stop_recording(self, low_disk: bool = False, wait: bool = False) -> None:
        """Stop recording. The encoders get no more frames at once; waiting for what they still hold to be
        written and the files to close happens on a thread of its own (it took 583 ms on the PC, 2026-11-17, and
        the window didn't answer meanwhile), then _recording_stopped finishes up here. wait: finish before
        returning (SprigCast is closing)."""
        session, self.recording = self.recording, None
        if session is None:
            return
        plugin_hooks.output_changed("record", None)
        self._say("Finishing files…")
        finishing: list = []
        session.stop(None if wait else finishing)
        self.live_side.set_recording(False)
        self._side_status("rec", None)
        if not finishing:
            self._recording_stopped(session, low_disk)
            return

        def finish() -> None:
            for enc in finishing:
                try:
                    enc.stop()
                except Exception:  # noqa: BLE001 (its error shows in session.errors)
                    log.exception("finishing a recording")
            self._record_finished.emit(session, low_disk)

        if not getattr(self, "_record_finished_wired", False):
            self._record_finished_wired = True
            self._record_finished.connect(self._recording_stopped)
        self._record_pending = (session, low_disk)
        self._record_stopping = threading.Thread(target=finish, name="record-finish", daemon=True)
        self._record_stopping.start()

    def _wait_for_stopped_recording(self) -> None:
        """Before another recording starts or SprigCast closes: the last one's files are closed first."""
        thread = getattr(self, "_record_stopping", None)
        if thread is not None and thread.is_alive():
            thread.join(20)
        pending = getattr(self, "_record_pending", None)
        if pending is not None and not (thread is not None and thread.is_alive()):  # its thread's word may still be on its way (or never come, when closing)
            self._recording_stopped(*pending)

    def _recording_stopped(self, session, low_disk: bool = False) -> None:
        """The recording's files are closed: say so, and do what follows (plain MP4s, repotting)."""
        if getattr(session, "_wrapped_up", False):
            return
        session._wrapped_up = True
        self._record_pending = None
        errors = session.errors
        names = ", ".join(p.name for p in session.paths)
        from sprigcast.core.pacing import memory_line

        eased = int(getattr(session, "eased", 0))
        log.info("recording stopped after %.0f s: %s; %d frames dropped%s%s; memory: %s", session.elapsed,
                 names or "no files", session.dropped, f", {eased} eased" if eased else "",
                 f"; errors: {errors}" if errors else "", memory_line())
        self._rest_soon()
        from sprigcast.output.naming import finish_later

        # MP4s were written crash-safe: now plain MP4s (with their chapter marks), in the background.
        finish_later(session.paths, chapters=getattr(session, "chapters", None))
        if errors:
            self.live_panel.set_recording("")
            msgbox.warning(self, "SprigCast", "Recording stopped with an error:\n" + "\n".join(errors))
        else:
            self._say(f"Saved {names}")
            self.live_panel.set_recording("")
            from sprigcast.app.recording_dialog import repot_choice

            repot, delete = repot_choice()
            mkvs = [p for p in session.paths if p.suffix.lower() == ".mkv"]
            if repot and mkvs and not low_disk:  # a repotted copy needs room the drive hasn't got
                self.repot(mkvs, delete)

    def _refresh_recording_status(self) -> None:
        session = self.recording
        if session.errors:
            self._stop_recording()
            return
        seconds = int(session.elapsed)
        clock = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        codec = codec_short(session.recorders[0][1].codec) if session.recorders else codec_short(pick_codec())
        dropped = session.dropped
        text = f"{'❚❚ PAUSED' if getattr(session, 'paused', False) else '● REC'} {clock}  ·  {codec}"
        notes = [f"{dropped} frames dropped: encoder busy"] if dropped else []
        warn = bool(notes)
        watch = getattr(self, "_space_watch", None)
        if watch is not None:
            if seconds % 15 == 0 and seconds != getattr(self, "_space_checked", -1):  # a disk check every 15 s
                self._space_checked = seconds
                space = watch.check()
                if watch.must_stop():
                    self._stop_for_space()
                    return
                if space:
                    log.warning("recording: %s", space)
                    self.statusBar().showMessage(space, 20000)
            from sprigcast.output.storage import rec_note

            note = rec_note(watch.left)
            if note:
                notes.append(note)
                warn = True
        self.live_panel.set_recording("  ·  ".join([text, *notes]))
        self._side_status("rec", seconds, "  ·  ".join(notes), warn)
        self.mode_clock.setToolTip("  ·  ".join([f"Recording with {codec}", *notes]))

    def pause_recording(self, pause: bool | None = None) -> str | None:
        """Pause the recording (pause=True), carry on in the same file (False), or switch (None). Returns a
        problem, or None. The Live header's Pause button, the hotkey, the API and Stream Deck keys all come here."""
        session = self.recording
        if session is None:
            return "Not recording."
        if not hasattr(session, "pause"):
            return "This recording can't pause."
        pause = not session.paused if pause is None else pause
        if pause == session.paused:
            return None
        (session.pause if pause else session.resume)()
        log.info("recording %s at %.0f s", "paused" if pause else "resumed", session.elapsed)
        self._say("Recording paused" if pause else "Recording again")
        self.live_side.set_recording(True, paused=pause)
        plugin_hooks.output_changed("record", getattr(session, "started_at", time.monotonic()))
        self._refresh_recording_status()
        return None

    def mark_chapter(self, name: str = "") -> str | None:
        """A chapter mark in the recording at this moment (hotkey, Stream Deck, API). Editors and players
        list them, so a long VOD can jump straight to the good parts. Returns a problem, or None."""
        session = self.recording
        if session is None:
            return "Not recording."
        self._chapters_marked = getattr(self, "_chapters_marked", 0) + 1
        name = (name or "").strip()[:80] or f"Chapter {self._chapters_marked}"
        if not getattr(session, "mark_chapter", lambda _n: False)(name):
            return "The recording hasn't started writing yet."
        log.info("chapter mark %r at %.0f s", name, session.elapsed)
        self._say(f"Marked: {name}")
        return None

    def _side_status(self, kind: str, seconds: int | None, details: str = "", warn: bool = False) -> None:
        """The header's one clock while live or recording (whichever has run longest, no label: the pink
        Record and End stream buttons beside it say which), then the details of both."""
        parts = self.__dict__.setdefault("_side_parts", {})
        if seconds is None:
            parts.pop(kind, None)
        else:
            parts[kind] = (seconds, details, warn)
        if not parts:
            self._set_header_clock(getattr(self, "_offline_clock", "Offline"), False)
            return
        shown = [parts[k] for k in ("live", "rec") if k in parts]
        top = max(p[0] for p in shown)
        clock = f"{top // 3600}:{top // 60 % 60:02d}:{top % 60:02d}"
        self._clock_html = f'<span style="color:{theme.LIVE_TEXT}; font-weight:700; font-size:16px">●&nbsp;&nbsp;{clock}</span>'
        # Beside the clock only the stream's short line ("2 of 2 sending"); anything about the recording
        # (dropped frames, low disk) turns the Garden check amber and is in the clock's tooltip, so the
        # header never grows past the window.
        live = parts.get("live")
        self._clock_details = ""
        if live and live[1]:
            self._clock_details = (f'<span style="color:{theme.WARN if live[2] else theme.MUTED}">'
                                   f"&nbsp;&nbsp;·&nbsp;&nbsp;{html.escape(live[1])}</span>")
        rec = parts.get("rec")
        self._rec_warning = rec[1] if rec and rec[2] else ""
        self.mode_clock.setStyleSheet("padding-left: 6px;")
        self.mode_clock.setTextFormat(Qt.TextFormat.RichText)
        self._show_clock()

    def _show_clock(self) -> None:
        brief = getattr(self, "_header_level", 0) >= 1
        self.mode_clock.setText(self._clock_html + ("" if brief else self._clock_details))
        self._hold_clock_width()
        QTimer.singleShot(0, self, self._fit_header)

    def _hold_clock_width(self) -> None:
        """Room for the clock measured with every digit as an 8 (the widest), so the buttons beside it
        don't shift as the seconds tick (1:11:11 is narrower than 1:08:08 in this font)."""
        label = self.mode_clock
        text = label.text()
        widest = re.sub(r"\d(?![^<]*>)", "8", text)  # digits in the words, not in the tags' colours
        label.setMinimumWidth(0)
        if widest != text:
            label.setText(widest)
        label.adjustSize()
        width = label.sizeHint().width()
        if widest != text:
            label.setText(text)
        label.setMinimumWidth(width)

    def _fit_header(self) -> None:
        """Keep Garden check, Record and Go live on screen: on a narrow window the header drops, in turn,
        the note beside the clock, the Garden check's numbers, then its name (the leaf and bars stay)."""
        bar = getattr(self, "mode_bar", None)
        if bar is None or getattr(self, "_fitting", False):
            return
        self._fitting = True
        try:
            for level in range(6):
                self._header_level = level
                self.live_side.garden.set_compact(level - 1)
                # Then tighter gaps, then the logo without its name.
                spacing = 10 if level >= 4 else 18
                if getattr(self, "_bar_spacing", 18) != spacing:
                    self._bar_spacing = spacing
                    bar.setStyleSheet(f"QToolBar#modeBar {{ background: {theme.CHROME}; border: none;"
                                      f" padding: 10px 18px; spacing: {spacing}px; }}")
                    bar.ensurePolished()
                self.brand_name.setVisible(level < 5)
                if getattr(self, "_side_parts", None) and hasattr(self, "_clock_html"):
                    self.mode_clock.setText(self._clock_html + ("" if level >= 1 else self._clock_details))
                    self._hold_clock_width()
                bar.layout().invalidate()
                if bar.sizeHint().width() <= self.width():
                    break
            bar.layout().activate()  # lay it out now, so nothing stays tucked away from a narrower moment
        finally:
            self._fitting = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_header()

    # Streaming -----------------------------------------------------------------

    def _refresh_stream_boxes(self) -> None:
        if self.streaming is None:
            self._live_destinations()

    def _sending_ids(self) -> set[str]:
        """The places the stream is going to right now (none when offline)."""
        session = self.streaming
        return set() if session is None else {getattr(st, "output_id", st.name) for _, st in session.streamers}

    def _place_switched_live(self, output_id: str, on: bool) -> None:
        """A place's switch on the Live screen while live: off ends just that place, on starts it again (with
        whatever its settings say now), and the rest of the stream carries on (user, 2026-11-21)."""
        output = next((o for o in self.collection.outputs if o.id == output_id and o.type == "stream"), None)
        if output is None:
            return
        service = SERVICES.get(output.service or "custom", SERVICES["custom"]).label
        sending = self._sending_ids()
        if not on:
            if output_id not in sending:
                problem = None
            elif len(sending) == 1:
                self._live_destinations()  # the switch goes back: this is the whole stream
                self._on_live_clicked()  # End stream, with its "are you sure" if that's on
                return
            else:  # Twitch Enhanced whose qualities others send: they keep them (StreamSession.drop)
                problem = self.stop_place(output_id)
        else:
            problem = self.start_place(output_id)
        if problem:
            log.info("places: %s (%s) not switched %s while live: %s", output_id, service, "on" if on else "off",
                     problem)
            self._say(problem)
            QTimer.singleShot(0, self, lambda: msgbox.warning(self, "SprigCast", problem))  # (after the click)
        else:
            log.info("places: %s (%s) switched %s while live", output_id, service, "on" if on else "off")
            output.enabled = on
            self.collection.save(self.scenes_path)
            self._keep_path_file()
        self._stream_status_at = 0.0
        if self.streaming is not None:
            self._refresh_stream_status()

    def _on_stream_toggled(self, output_id: str, on: bool) -> None:
        if self.streaming is not None:
            self._place_switched_live(output_id, on)
            return
        for output in self.collection.outputs:
            if output.id == output_id:
                output.enabled = on
        self.collection.save(self.scenes_path)
        self._refresh_stream_boxes()

    def _on_live_clicked(self) -> None:
        from sprigcast.app.settings_window import CONFIRM_GO_LIVE

        if self.streaming is None and QSettings().value(CONFIRM_GO_LIVE, False, type=bool):
            answer = msgbox.question(self, "SprigCast", "Go live now?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        if self.streaming is not None and confirm_enabled(CONFIRM_STREAM):
            answer = msgbox.question(self, "SprigCast", "End the stream?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._toggle_streaming()

    def _toggle_streaming(self) -> None:
        if self.streaming is None:
            self._start_streaming()
        else:
            self._stop_streaming()

    def _page_state(self) -> dict:
        """What Web page sources see in window.sprigcast: the bloom on air in each shape, which shape each page is
        shown in, and whether SprigCast is live or recording (app/browser_host.py)."""
        col = self.collection
        blooms, shapes = {}, {}
        sources = {s.id: s for s in col.sources}
        for canvas in col.canvases:
            if not canvas.enabled:
                continue
            scene = col.active_scene(canvas.id)
            if scene is None:
                continue
            blooms[canvas.id] = scene.name
            todo, seen = [scene], set()
            while todo:
                layout = todo.pop()
                if layout.id in seen:
                    continue
                seen.add(layout.id)
                for item in layout.items:
                    source = sources.get(item.source)
                    part = col.part_of(source) if source is not None else None
                    if part is not None:
                        todo.append(col.shape_for(part, canvas.id))
                    elif source is not None and source.kind == "browser":
                        shapes.setdefault(source.id, canvas.id)
        return {"blooms": blooms, "shapes": shapes, "streaming": self.streaming is not None,
                "recording": self.recording is not None}

    def _start_streaming(self, remote: bool = False, force: bool = False) -> dict:
        """Go live. From an app (remote) nothing ever waits on a pop-up: problems and the Stream doctor's
        warnings come back in the answer instead ({"problem"} / {"warnings"}), and force goes live past the
        warnings. Returns {"started": True, "warnings": [...]} when it went live."""
        outputs = [o for o in self.collection.outputs if o.type == "stream" and o.enabled]
        if not outputs:
            text = "Tick at least one place to stream to, or add one with Stream…."
            if not remote:
                msgbox.information(self, "SprigCast", text)
            return {"problem": text}
        targets, problems = [], []
        for output in outputs:
            target, problem = self._stream_target(output, ask=not remote, quiet=remote)
            if problem:
                problems.append(problem)
            elif target is None:
                return {"problem": "Canceled."}  # the user chose not to go live
            else:
                targets.append(target)
        if problems:
            if not remote:
                msgbox.warning(self, "SprigCast", "Not ready to go live:\n\n" + "\n".join(problems))
            return {"problem": "Not ready to go live: " + " ".join(problems)}
        from sprigcast.output.stream import same_key_warnings

        warnings: list[str] = []
        same_key = same_key_warnings(targets) + self._places_unreachable(targets)
        if remote:
            warnings = same_key + self._doctor_warnings()
            if warnings and not force:
                log.info("an app asked to go live; the Stream doctor said wait: %s", " | ".join(warnings))
                return {"warnings": warnings}
        elif not self._doctor_says_go(same_key):
            return {"problem": "Canceled."}
        self._say("Connecting…")
        self._wake_up()
        try:
            from sprigcast.app.settings_window import stream_delay

            self.streaming = StreamSession.start(self.engine, targets,
                                                 resolve_codec(self.collection.stream_encoder, streaming=True),
                                                 self.collection.stream_encoder, delay_s=stream_delay(),
                                                 share_all=self.collection.recording.same_as_stream)
        except Exception as exc:
            self.streaming = None
            if not remote:
                msgbox.critical(self, "SprigCast", f"Could not go live:\n{exc}")
            else:
                self._say(f"Could not go live: {exc}")
            return {"problem": f"Could not go live: {exc}"}
        self.live_side.set_live(True)
        plugin_hooks.output_changed("stream", getattr(self.streaming, "started_at", time.monotonic()))
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
        self._record_with_stream()
        self._replay_with_stream()
        return {"started": True, "warnings": warnings}

    def _replay_with_stream(self) -> None:
        """Settings → General → "Start Instant replay when I go live"."""
        from sprigcast.app import features
        from sprigcast.app.settings_window import REPLAY_ON_LIVE

        if self.replay is None and features.on("clips") and QSettings().value(REPLAY_ON_LIVE, False, type=bool):
            self._toggle_replay(True)

    def _record_with_stream(self) -> None:
        """Settings → Recording → "Record when I go live": the stream's own recording starts with it
        (from the stream's encoder when "Same as stream" is on too) and ends with it."""
        self._recording_for_stream = False
        if not self.collection.recording.record_when_live or self.recording is not None:
            return  # a recording you started yourself carries on as you left it
        answer = self._start_recording(remote=True, force=True)  # no pop-ups in the middle of going live
        self._recording_for_stream = bool(answer.get("started"))
        if not self._recording_for_stream:
            log.warning("record when live: %s", answer.get("problem", "didn't start"))
            self.statusBar().showMessage(f"Live, but not recording: {answer.get('problem', '')}", 10000)

    def _stream_target(self, output, ask: bool = True, quiet: bool = False):
        """What going live sends to one place: (target, None), (None, problem), or (None, None) when the
        user said no to Twitch's question. ask=False (an app starting a place): no questions, one quality.
        quiet (an app going live): Enhanced Broadcasting still, but no questions (single quality if Twitch can't)."""
        service = SERVICES.get(output.service or "custom", SERVICES["custom"])
        server = service.server or output.server
        key = get_key(output.key_ref)
        if output.twitch_account and output.service == "twitch":
            key = self._fresh_twitch_key(output, key)
        elif output.service == "youtube" and output.account and key:
            key = self._fresh_youtube_key(output, key)
        canvas = next((c for c in self.collection.canvases if c.id == output.canvas and c.enabled), None)
        if not server:
            return None, f"{service.label}: paste the server URL in Stream…"
        if not key:
            return None, f"{service.label}: add your stream key in Stream…"
        if canvas is None:
            return None, f"{service.label}: the {output.canvas} canvas is not available"
        name = f"{service.label} ({canvas.id})" + (" test" if output.bandwidth_test and service.bandwidth_test else "")
        kbps = output.bitrate_kbps if output.ignore_recommendations else min(output.bitrate_kbps, service.max_kbps)
        target = StreamTarget(name, canvas, server, key, kbps, output.size, output.fps,
                              output.bandwidth_test and service.bandwidth_test, output_id=output.id,
                              auto_bitrate=output.auto_bitrate, max_kbps=service.max_kbps)
        if (ask or quiet) and output.enhanced and output.service == "twitch":
            return self._enhanced_target(output, target, quiet=quiet), None
        return target, None

    def start_place(self, output_id: str) -> str | None:
        """Send to one more place while live (apps: place.start). A problem, or None."""
        output = next((o for o in self.collection.outputs if o.id == output_id and o.type == "stream"), None)
        if output is None:
            return "No such place."
        if self.streaming is None:
            return "Not live."
        if self.streaming.find(output.id) is not None:
            return None  # already sending
        target, problem = self._stream_target(output, ask=False)
        if target is None:
            return problem or "Couldn't start it."
        from sprigcast.app.settings_window import stream_delay

        try:
            self.streaming.add(target, resolve_codec(self.collection.stream_encoder, streaming=True),
                               self.collection.stream_encoder, delay_s=stream_delay())
        except Exception as exc:
            log.warning("could not start %s: %s", target.name, exc)
            return f"Couldn't start {target.name}: {exc}"
        log.info("app started %s while live", target.name)
        self._stream_status_at = 0.0
        self._refresh_stream_status()
        return None

    def stop_place(self, output_id: str) -> str | None:
        """Stop one place while the rest stay live (apps: place.stop); the last one ends the stream."""
        if self.streaming is None:
            return "Not live."
        if self.streaming.find(output_id) is None:
            return None  # not sending there: nothing to stop
        if len(self.streaming.streamers) == 1:
            QTimer.singleShot(0, self._stop_streaming)  # just after replying, like stream.stop
            return None
        self.streaming.drop(output_id)
        log.info("app stopped %s while live", output_id)
        self._stream_status_at = 0.0
        self._refresh_stream_status()
        return None

    def _enhanced_target(self, output, target, quiet: bool = False):
        """Ask Twitch which qualities to send. Returns the target to use, or None to cancel going live.
        quiet: no questions (an app going live): single quality if it's unavailable, and Twitch's warnings logged."""
        from sprigcast import __version__
        from sprigcast.output import enhanced

        canvases = [target.canvas]
        if output.enhanced_vertical and not enhanced.vertical_allowed():
            log.info("Enhanced Broadcasting: the vertical canvas isn't sent to Twitch for now (%s=1 tries it)",
                     enhanced.VERTICAL_ENV)
        if enhanced.sends_vertical(output):
            vertical = next((c for c in self.collection.canvases if c.enabled and c.height > c.width
                             and c.id != target.canvas.id), None)
            if vertical is not None:
                canvases.append(vertical)
        self._say("Asking Twitch for the Enhanced Broadcasting settings…")
        try:
            request = enhanced.build_request(
                target.key, [enhanced.CanvasInfo(
                    c.width, c.height, (output.fps or c.fps) if c is target.canvas else c.fps,
                    enhanced.place_size(output.size, c) if c is target.canvas else None) for c in canvases], __version__,
                # Auto: two qualities a canvas (each more is another picture made and another encoder on the card;
                # at 4,500 kbps viewers lose little). A number picked in the place's settings wins.
                max_tracks=output.enhanced_max_tracks or enhanced.LIGHT_TRACKS * len(canvases),
                max_bitrate_kbps=output.enhanced_max_kbps or None,
            )
            from sprigcast.core.workers import wait_answering

            answer = wait_answering(enhanced.fetch_config, request)  # a web request: off the window's thread
            config = enhanced.parse_config(answer, target.key, pick_codec(),
                                           bandwidth_test=target.bandwidth_test)
        except Exception as exc:
            log.warning("Enhanced Broadcasting unavailable: %s", exc)
            if quiet:
                return target
            answer = msgbox.question(
                self, "SprigCast",
                f"Enhanced Broadcasting isn't available right now:\n\n{exc}\n\n"
                "Go live with a single quality instead?",
            )
            return target if answer == QMessageBox.StandardButton.Yes else None
        if config.warning and quiet:
            log.warning("Twitch says: %s (an app is going live, so no question)", config.warning)
        elif config.warning:
            answer = msgbox.question(self, "SprigCast", f"Twitch says:\n\n{config.warning}\n\nGo live anyway?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        log.info("Enhanced Broadcasting: %s", config.describe())
        log.info("Enhanced Broadcasting: Twitch asked for %s", enhanced.answer_summary(answer))
        vertical = any(t.canvas_index > 0 for t in config.tracks)
        if enhanced.sends_vertical(output) and not vertical:
            # Twitch decides per channel and PC: a channel not yet allowed Dual Format, or a PC Twitch thinks can't
            # encode both, gets horizontal qualities only.
            why = ("Turn on a vertical canvas to send it to Twitch too." if len(canvases) < 2 else
                   "Twitch didn't ask for the vertical canvas this time, so it goes live horizontal only.")
            log.warning("Enhanced Broadcasting: vertical asked for but not sent (%s)", why)
            if not quiet:
                self.statusBar().showMessage(why, 15000)
        target.name = f"Twitch Enhanced ({len(config.tracks)} qualities{', + vertical' if vertical else ''})"
        target.enhanced, target.canvases = config, canvases
        return target

    def _fresh_twitch_key(self, output, saved: str | None) -> str | None:
        """Ask Twitch for the current key (it changes if you reset it); fall back to the saved one."""
        from sprigcast.output import twitch_auth
        from sprigcast.output.keys import set_key

        self._say("Getting your stream key from Twitch…")
        try:
            from sprigcast.core.workers import wait_answering

            key = wait_answering(twitch_auth.fetch_stream_key)  # a web request: off the window's thread
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

    def _reach_targets(self) -> list:
        """The switched-on places as preflight targets: their name as Destinations shows it, and their server."""
        streams = [o for o in self.collection.outputs if o.type == "stream"]
        targets = []
        for output in streams:
            service = SERVICES.get(output.service or "custom", SERVICES["custom"])
            server = service.server or output.server
            if not output.enabled or not server:
                continue
            name = service.label
            if sum(1 for o in streams if o.service == output.service) > 1:
                name += f" · {output.canvas.capitalize()}"
            targets.append(types.SimpleNamespace(name=name, server=server, output_id=output.id))
        return targets

    def _check_reach(self) -> None:
        """Try each switched-on place's server in the background (2 s at most). Only offline, on Live, shown."""
        if (self.streaming is not None or getattr(self, "mode", "") != "live" or not self.isVisible()
                or self.isMinimized() or (self._reach_job is not None and not self._reach_job.done())):
            return
        from sprigcast.core import workers
        from sprigcast.output import preflight

        targets = self._reach_targets()
        if not targets:
            self._show_reach({}, [])
            return
        self._reach_job = workers.run(lambda: (preflight.failing(targets), preflight.tried(targets)),
                                      name="place check")
        self._reach_poll.start()

    def _reach_done(self) -> None:
        job = self._reach_job
        if job is None or not job.done():
            return
        self._reach_poll.stop()
        if job.exception() is not None or self.streaming is not None:
            return
        from sprigcast.output import preflight

        failed, tried = job.result()
        unreachable = {t.output_id: why for sent, why in failed.values() for t in sent}
        if preflight.offline(failed, tried):
            lines = ["This PC can't reach the internet right now, so no place would connect."]
        else:
            lines = [f"{' and '.join(t.name for t in sent)} would not connect: {where[0]} {why}."
                     for where, (sent, why) in failed.items()]
        if unreachable != self._unreachable:
            log.info("places that won't connect: %s", "; ".join(lines) or "none")
        self._show_reach(unreachable, lines, offline=preflight.offline(failed, tried))

    def _show_reach(self, unreachable: dict[str, str], lines: list[str], offline: bool = False) -> None:
        changed = unreachable != self._unreachable
        self._unreachable, self._unreachable_lines = unreachable, lines
        names = [t.name for t in self._reach_targets() if t.output_id in unreachable]
        self.live_side.set_unreachable(names, "\n".join(lines), "No internet" if offline else None)
        if changed:
            self._live_destinations()

    def _places_unreachable(self, targets) -> list[str]:
        """Going live: a line for each place whose server can't be reached right now (output/preflight.py; a
        connection is opened and closed, the key is never used). At most 2 s, with the window still answering.
        They show in the Stream doctor's card, or in the warnings an app gets, before anything is sent."""
        from sprigcast.output import preflight

        self._say("Checking your places…")
        try:
            from sprigcast.core.workers import wait_answering

            found = wait_answering(preflight.check_places, targets)
        except Exception as exc:  # noqa: BLE001 (the check failing never stops a stream)
            log.warning("could not check the places before going live: %s", exc)
            return []
        for line in found:
            log.warning("before going live: %s", line)
        return found

    def _fresh_youtube_key(self, output, saved: str) -> str:
        """A YouTube place whose key came from signing in: before going live, ask YouTube which key its stream
        is waiting on, since a channel can have several and a saved one can be an old one that shows nothing
        (it was refused five times, live 2026-11-21). Not signed in, or YouTube not answering: the saved key."""
        from sprigcast.output import service_signin, stream_details
        from sprigcast.output.keys import set_key

        if not stream_details.account("youtube"):
            return saved
        self._say("Checking your stream key with YouTube…")
        try:
            from sprigcast.core.workers import wait_answering

            fetched = wait_answering(service_signin.key_from_account, "youtube", output.canvas == "vertical", saved)
        except Exception as exc:  # noqa: BLE001 (any failure: go live with the saved key, as before)
            log.warning("could not check the YouTube stream key: %s", exc)
            return saved
        if not fetched.key or fetched.key == saved:
            return saved
        log.warning("YouTube (%s): the saved stream key isn't the one YouTube is waiting on; using the channel's "
                    "current key", output.canvas)
        self.statusBar().showMessage("YouTube's stream key had changed. Using the current one.", 15000)
        if output.key_ref:
            try:
                set_key(output.key_ref, fetched.key)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not save the fetched YouTube key: %s", exc)
        return fetched.key

    def _stop_streaming(self) -> None:
        session, self.streaming = self.streaming, None
        if session is None:
            return
        plugin_hooks.output_changed("stream", None)
        self._say("Ending stream…")
        session.stop(wait=False)  # the encoders and connections finish on their own thread
        if getattr(self, "_recording_for_stream", False):
            self._recording_for_stream = False
            from sprigcast.app.settings_window import KEEP_RECORDING

            if self.recording is not None and not QSettings().value(KEEP_RECORDING, False, type=bool):
                self._stop_recording()  # it started with the stream: it ends with it (unless kept going)
        delayed = max((getattr(st, "delay_s", 0.0) for _, st in session.streamers), default=0.0)
        if delayed > 0:
            self.statusBar().showMessage(f"Stream delay: the last {delayed:g} s still go out to viewers.",
                                         int(delayed * 1000) + 3000)
        from sprigcast.app import transitions_panel

        transitions_panel.set_quiet(False)
        self.live_side.set_live(False)
        QTimer.singleShot(1500, self, self._check_reach)  # offline again: are the places still reachable?
        self._side_status("live", None)
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
            if proven:
                log.info("upload: this stream proved %d kbps fits", proven)
            else:
                trouble = optimize.network_trouble(streamers, time.monotonic() - session.elapsed)
                log.info("upload: this stream had network trouble: %s", " | ".join(trouble) or "no place said which")
        else:  # said too: a soak that left no line couldn't be told from one that never got here
            log.info("upload: this stream proves nothing (%d s, frames sent per place: %s)", seconds,
                     ", ".join(str(getattr(st, "frames", 0)) for st in streamers) or "none")
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
        # Help improve SprigCast (only when turned on): this stream's report and today's log, scrubbed, sent on a
        # worker. Asked once, after the first stream of a minute or more.
        from sprigcast.app import share_reports

        share_reports.send_after_stream(getattr(report, "path", None), seconds, collection=self.collection)
        if seconds >= share_reports.MIN_SECONDS and not share_reports.asked():
            QTimer.singleShot(2500, self, lambda: self._ask_to_share(seconds))
        health, self._health = getattr(self, "_health", None), None
        self.live_stage.card.set_notice("", "")
        if health is not None and seconds >= 10:  # not for a start that never got going
            health.finish(QSettings())
        self._show_last_stream()

    def _ask_to_share(self, seconds: float) -> None:
        """The one-time "Help improve SprigCast?" card, when nothing else is open and we're still offline."""
        from PySide6.QtWidgets import QApplication

        from sprigcast.app import share_reports

        if self.streaming is not None or QApplication.activeModalWidget() is not None:
            return  # asked after a later stream instead
        if share_reports.maybe_ask(self, seconds):
            self.statusBar().showMessage("Thank you! A summary goes to the developer after each stream.", 8000)

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
        if not theme.bursting():  # a pop-up being made: next second's (65 ms under Hotkeys on the PC)
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
        # How many places are getting the stream (the upload they add up to is in the tooltip).
        details = f"{len(sending)} of {len(states)} sending" if sending else ""
        if worst != "good":
            details = "  ·  ".join(filter(None, [details, "check Destinations"]))
        self._side_status("live", seconds, details, worst != "good")
        if sending:
            self.mode_clock.setToolTip(f"{sum(sending) / 1000:.1f} Mbps up in all")

    # Hotkeys -----------------------------------------------------------------

    def _sync_push_key(self) -> None:
        """Push to talk listens for its key only while it's on (Settings → Audio → Devices)."""
        push = getattr(self, "push_key", None)
        if push is not None:
            audio = self.collection.audio
            push.apply(audio.push_key, active=audio.push_mode != "off")

    def _apply_hotkeys(self) -> None:
        self.hotkeys.apply(self.collection.hotkeys)
        if self.collection.hotkeys and self.hotkeys.mode == "window":
            self.statusBar().showMessage("Global hotkeys aren't available here; they work while SprigCast is focused.", 10000)

    # Settings window -------------------------------------------------------------

    def _gear_clicked(self) -> None:
        log.info("settings: gear clicked (window %s)", "active" if self.isActiveWindow() else "not active")
        self._open_settings()

    def _open_settings(self, tab: str = "General", output_id: str | None = None) -> bool | None:
        """Open Settings and wait for it. True when something was saved (Apply or Save), False when it was
        closed without; None when no Settings window of this call was shown and closed."""
        from sprigcast.core import workers

        if workers.when_free(lambda: self._open_settings(tab, output_id)):
            return  # mid-wait (a Control API screenshot, say): opens the moment it's done
        # (not the off-screen warm-up one: a gear click in its 2 s did nothing, "already open")
        open_now = next((w for w in QApplication.topLevelWidgets() if isinstance(w, SettingsWindow) and w.isVisible()
                         and not w.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)), None)
        if open_now is not None:  # already open (behind another window, say): bring it forward
            log.info("settings: already open, brought forward")
            open_now.raise_()
            open_now.activateWindow()
            return
        if tab == "Hotkeys":  # windows of their own now; older links still land there
            self.open_hotkeys()
            return
        if tab == "Plugins":
            self.open_plugin_store()
            return

        def scan() -> dict[str, list[str]]:  # restarts PortAudio (a few hundred ms on Windows): on a worker
            return {
                "audio_input": [d.name for d in list_audio_devices(loopback=False)],
                "audio_output": [d.name for d in list_audio_devices(loopback=True, rescan=False)],  # one rescan
            }

        from sprigcast.core import workers

        devices = workers.run(scan, name="sound-devices")
        self._clips_before = self._clip_key()
        window = SettingsWindow(
            self.collection,
            default_recordings_folder=self._default_recordings_folder(),
            encoder=codec_label(pick_codec()),
            audio_devices=devices,
            streaming=self.streaming is not None,
            recording=self.recording is not None,
            live_places=self._sending_ids(),
            control=self.control,
            check_updates=lambda: self.updater.check(manual=True),
            tab=tab,
            parent=self,
            fill_after_show=True,  # the window is up at once; its page fills in just after
        )
        if output_id == "new":
            window.soon(lambda: window.stream_page.add_output())
        elif output_id:
            window.soon(lambda: window.stream_page.select_output(output_id))
        window.applied.connect(self._after_settings)
        saved = []
        window.applied.connect(lambda: saved.append(True))
        window.on_ducking = self._open_ducking  # handed to the Audio page when it's made
        window.on_clear_data = self.clear_all_data

        def import_from(settings) -> None:
            settings.reject()  # first: an import switches gardens, and Settings must not save over it
            QTimer.singleShot(0, self, self.gardens.choose_import)

        window.on_import = import_from
        window.optimize_requested.connect(lambda: QTimer.singleShot(0, self, self.open_optimizer))
        window.restart_requested.connect(lambda: QTimer.singleShot(0, self, self.restart_app))
        modal(window)
        return bool(saved)

    WARM_SETTINGS_MS = 2_000  # the off-screen Settings is let go this long after it's shown
    WARM_SETTINGS_RETRY_MS, WARM_SETTINGS_TRIES = 2_000, 30  # while the password store loads: later, then never

    def warm_settings(self) -> None:
        """Once, a little after start (not while live or recording): make Settings' Stream page off screen and let
        it go. Qt then has the style rules, fonts and icons it needs ready, and opening Settings later takes about
        half as long (268 ms the first time, 149 ms after, measured 2026-11-18)."""
        if self.streaming is not None or self.recording is not None or not self.isVisible():
            return
        if any(isinstance(w, SettingsWindow) and w.isVisible() for w in QApplication.topLevelWidgets()):
            return
        from sprigcast.core import warm

        if warm.keyring_loading.is_set():  # the password store is still loading on its worker: not now
            self._warm_waits = getattr(self, "_warm_waits", 0) + 1
            if self._warm_waits <= self.WARM_SETTINGS_TRIES:
                QTimer.singleShot(self.WARM_SETTINGS_RETRY_MS, self, self.warm_settings)
            return
        try:
            window = SettingsWindow(self.collection, default_recordings_folder=self._default_recordings_folder(),
                                    encoder="", audio_devices={}, tab="Stream", parent=self, fill_after_show=True)
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            window.show()  # made in parts a tick apart, like a real open (all at once: 111 ms on the PC)

            def done() -> None:
                window.hide()
                window.deleteLater()
            QTimer.singleShot(self.WARM_SETTINGS_MS, window, done)
        except Exception:  # only a warm-up: Settings still opens as before
            log.warning("settings warm-up failed", exc_info=True)

    def export_settings(self, path: str | None = None) -> bool:
        """File → Export settings: your choices in one file (never keys, sign-ins or passwords)."""
        from PySide6.QtWidgets import QFileDialog

        from sprigcast.app import settings_file

        if path is None:
            start = str(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))
                        / "SprigCast settings.json")
            path, _ = QFileDialog.getSaveFileName(self, "Export settings", start, "SprigCast settings (*.json)")
        if not path:
            return False
        try:
            settings_file.export_settings(self.collection, Path(path))
        except OSError as exc:
            msgbox.warning(self, "Export settings", f"Couldn't save it: {exc}")
            return False
        self._saved_note("Settings exported", Path(path).parent, detail="No stream keys or passwords are in it")
        return True

    def import_settings(self, path: str | None = None) -> bool:
        """File → Import settings: take another setup's choices (this PC's keys and sign-ins stay)."""
        from PySide6.QtWidgets import QFileDialog

        from sprigcast.app import settings_file

        if self.streaming is not None or self.recording is not None:
            msgbox.information(self, "Import settings", "Stop streaming and recording first.")
            return False
        if path is None:
            start = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
            path, _ = QFileDialog.getOpenFileName(self, "Import settings", start, "SprigCast settings (*.json)")
        if not path:
            return False
        try:
            done = settings_file.import_settings(self.collection, Path(path))
        except ValueError as exc:
            msgbox.warning(self, "Import settings", str(exc))
            return False
        log.info("settings imported from %s: %s", Path(path).name, ", ".join(done) or "nothing")
        self.collection.save(self.scenes_path)
        self._after_settings()
        self._saved_note("Settings imported", detail=", ".join(done) or "Nothing in it to change")
        return True

    def open_hotkeys(self) -> None:
        """Tools → Hotkeys."""
        from PySide6.QtWidgets import QDialog

        from sprigcast.app.hotkeys_dialog import HotkeysWindow

        if modal(HotkeysWindow(self.collection, parent=self, fill_after_show=True)) == QDialog.DialogCode.Accepted:
            self._apply_hotkeys()
            self._changed()

    def open_plugin_store(self) -> None:
        """Tools → Plugin store."""
        from sprigcast.app.plugin_store_page import PluginStoreWindow

        window = PluginStoreWindow(self.plugins_dir, self.plugin_report,
                                   self._open_plugins_folder if self.plugins_dir is not None else None,
                                   self.collection, on_air=self.streaming is not None or self.recording is not None,
                                   parent=self, fill_after_show=True)
        window.changed.connect(self._fill_more_menu)
        # A removed plugin's sources stop now (and an added one's start), not at the next restart.
        window.changed.connect(self._send_whole)
        modal(window)
        self._plugin_updates()  # an Update pressed while on air waits until the stream and recording stop

    def restart_app(self) -> None:
        """Close and start again (Settings' Restart now, for canvas changes). Never while live or recording."""
        if self.streaming is not None or self.recording is not None:
            self.statusBar().showMessage("Stop streaming and recording first, then restart.", 10000)
            return
        app = QApplication.instance()
        app.setProperty("sprigcast_restart", True)  # main() starts the new copy once this one has let go
        self.close()

    def _poll_camera(self) -> None:
        """Once a second while SprigCast Camera is on: feed it, and send the sound along while an app watches."""
        if self.camera is None:
            self.engine.set_sound_out(None)
            return
        self.camera.poll()
        self.engine.set_sound_out(self._camera_sound_device() if self.camera.watched else None)

    def _camera_sound_device(self) -> str | None:
        """Settings → Video → Virtual camera → Sound to, unless SprigCast also captures that device (an echo)."""
        from sprigcast.app.output_page import camera_sound

        device = camera_sound()
        if not device:
            return None
        captured = {str(s.settings.get("device", "")) for s in self.collection.sources_in_use() if s.kind == "audio_output"}
        if device in captured:
            if not getattr(self, "_sound_echo_said", False):
                self._sound_echo_said = True
                self._say(f"Camera sound not sent: {device} is captured as desktop sound, so it would echo")
            return None
        self._sound_echo_said = False
        return device

    def _sync_camera(self) -> None:
        """Turn SprigCast Camera on or off to match Settings (it's only added while SprigCast runs)."""
        from sprigcast.app.output_page import camera_choice, camera_wanted
        from sprigcast.output import virtualcam

        from sprigcast.app import features

        virtualcam.look_ahead()  # the camera window then opens without reading the camera's files
        want = (features.on("camera") and camera_wanted() and virtualcam.supported()[0]
                and virtualcam.installed())
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
            self.engine.set_sound_out(None)
        if self.camera is not None:
            self._camera_error = ""
        self.live_side.set_camera(self.camera is not None, getattr(self, "_camera_error", ""))

    def _toggle_camera(self) -> None:
        """Live's Virtual camera button: the same switch as Settings → Video → Virtual camera."""
        from PySide6.QtCore import QSettings

        from sprigcast.app.output_page import CAMERA_ON
        from sprigcast.output import virtualcam

        from sprigcast.app import features

        turn_on = self.camera is None
        if turn_on and not features.on("camera"):
            self._say(features.OFF_NOTE["camera"])
            return
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
            answer = msgbox.question(
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

        box = msgbox.MessageCard("SprigCast Camera", f"{why}\nIn Windows Settings, turn on Camera access and Let "
                                 "desktop apps access your camera.", self)
        box.add_button("Close")
        open_button = box.add_button("Open camera settings", primary=True)
        modal(box)
        if box.clicked is open_button:
            virtualcam.open_privacy_settings()

    def _camera_settings(self) -> None:
        """The gear beside Virtual camera: a small pop-up to pick what it shows."""
        from sprigcast.app.camera_popup import camera_popup
        from sprigcast.output import virtualcam

        ok = virtualcam.supported()[0]
        repair = virtualcam.set_up if ok and (not virtualcam.installed() or virtualcam.needs_update()) else None
        status = getattr(self, "_camera_error", "") if self.camera is None else ""
        pop = camera_popup(self.collection, self, repair, status)
        modal(pop)

    def _after_settings(self) -> None:
        """Settings were applied: save, and bring every dock up to date."""
        self._keep_path_file()  # places added, removed or edited: in the path's own file right away
        from sprigcast.app import privacy

        if privacy.supported():
            privacy.apply_to(self)  # Hide from screen capture, as just set
            self._privacy_to_open_windows()
        self.tray.sync()  # Show in the tray, as just set
        self._apply_preview_settings()  # Show what's outside the canvas, as just set
        clips = getattr(self, "_clips_before", None)
        if clips is not None:
            self._restart_clips_if_changed(clips)
            rec = self.collection.recording
            self._clips_before = self._clip_key()
        self._apply_features()
        self.live_side.set_clip(self.replay is not None, self.collection.recording.replay_seconds)
        from sprigcast.app.theme import apply_theme

        apply_theme(QApplication.instance())
        from sprigcast.app.settings_window import sleep_policy

        self.engine.sleep_policy = sleep_policy()
        self._preview_live = None  # read again (Preview while live)
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
        from sprigcast.app import settings_keeper
        from sprigcast.app.main import config_dir

        settings_keeper.keep_aside_soon(config_dir())  # the copy that brings settings back if they go missing

    def _apply_features(self) -> None:
        """Settings → General → Features: a feature that's off leaves Live's bar and stops (nothing kept loaded)."""
        from sprigcast.app import features

        clips, camera = features.on("clips"), features.on("camera")
        if not clips and getattr(self, "replay", None) is not None:
            self._toggle_replay(False)
        self.live_side.clip.setVisible(clips)
        for w in (self.live_side.camera, self.live_side.camera_settings):
            w.setVisible(camera)
        if not camera:
            self.live_side.camera_note.setVisible(False)
        # _sync_camera (run next by _after_settings, and at start) lets the camera go when it's off

    REST_AFTER_MS = 10_000

    def _rest_soon(self) -> None:
        """A recording, the stream or clips just stopped: once nothing is left running, give memory back.

        One timer for all of them: stopping everything at once stops several outputs, and the soak ran
        the clean-up three times in 0.4 s, once for each."""
        timer = getattr(self, "_rest_timer", None)
        if timer is None:
            timer = self._rest_timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._rest_if_idle)
        timer.start(self.REST_AFTER_MS)

    def _rest_if_idle(self) -> None:
        """Nothing is being recorded, streamed or kept for clips: let go of what the show left behind.

        After a 2-hour soak the app sat at ~415 MB at rest against ~200 MB fresh, flat for an hour: pages
        touched during the show (encoders, the clips buffer, frames in flight) and cached plugin art.
        Python's clean-up here only looks at what was made since its last tidy, a few ms. Looking through
        everything (pacing.settle) took 191 ms after the soak, 22 late preview frames, so that waits
        until SprigCast is minimized (see changeEvent)."""
        if self._busy_with_outputs():
            return
        from sprigcast.core import painted, pacing

        painted.forget_art()
        pacing.tidy()
        self._sweep_due = True
        if self.isMinimized():
            self._sweep_if_due()
        pacing.trim_working_set_later(lambda trimmed: log.info(
            "at rest: let go of what the show held%s; memory: %s",
            " (and handed unused pages back to Windows)" if trimmed else "", pacing.memory_line()))

    REST_AFTER_QUIET_S = 10.0

    def _going_out(self) -> bool:
        """Anything leaving SprigCast that wants the boosts: live, recording, clips, or SprigCast Camera watched."""
        camera = self.camera
        return self._busy_with_outputs() or bool(camera is not None and getattr(camera, "watched", False))

    def _wake_up(self) -> None:
        from sprigcast.app import priority

        self._quiet_since = None
        if priority.wake():
            log.info("rest mode over: something is going out")

    def _check_rest(self) -> None:
        """Every 2 s: rest mode once nothing has gone out for REST_AFTER_QUIET_S, awake as soon as something does;
        Windows' power saving only while the window is out of sight."""
        from sprigcast.app import priority

        engine, live = getattr(self, "engine", None), self.streaming is not None
        if engine is not None and getattr(engine, "live", None) is not live:
            engine.live = live  # cameras stay open while live (render/engine.py _may_sleep)
        if self._going_out():
            self._wake_up()
            return
        now = time.monotonic()
        if self._quiet_since is None:
            self._quiet_since = now
        if now - self._quiet_since >= self.REST_AFTER_QUIET_S:
            priority.rest(save_power=self.isMinimized() or not self.isVisible())

    def _busy_with_outputs(self) -> bool:
        return self.recording is not None or self.streaming is not None or self.replay is not None

    def _sweep_if_due(self) -> None:
        """Minimized and at rest: the one full look through Python's objects, where a hitch can't be seen."""
        if not getattr(self, "_sweep_due", False) or self._busy_with_outputs():
            return
        from sprigcast.core import pacing

        self._sweep_due = False
        log.info("python cleanup: looked through everything in %.0f ms while minimized", pacing.settle())

    def show_minimized_drawn(self, settle_ms: int = 300) -> None:
        """Start minimized, but drawn: shown see-through and without taking focus, drawn once, then minimized.
        A window minimized before it was ever drawn showed white in the taskbar preview and did all its
        first-time work (styles, fonts, icons, layouts) the moment it was opened, which took seconds."""
        began = time.perf_counter()
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowOpacity(0.0)
        self.show()

        def minimize() -> None:
            if self.windowOpacity() >= 1.0:  # opened meanwhile (changeEvent made it whole): leave it open
                return
            self.showMinimized()
            self.setWindowOpacity(1.0)
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
            log.info("started minimized: drawn once first (%.0f ms)", 1000 * (time.perf_counter() - began))

        QTimer.singleShot(settle_ms, minimize)

    def _may_close(self) -> bool:
        """Closing the window while live or recording ends them: ask first (like OBS)."""
        doing = [what for what, on in (("live", self.streaming is not None),
                                       ("recording", self.recording is not None)) if on]
        if not doing:
            return True
        what = "live and recording" if len(doing) == 2 else doing[0]
        answer = msgbox.question(self, "SprigCast", f"You're {what}. Close SprigCast and end it?")
        return answer == QMessageBox.StandardButton.Yes

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if not self.isMinimized() and (self.windowOpacity() < 1.0 or self.testAttribute(
                    Qt.WidgetAttribute.WA_ShowWithoutActivating)):
                # Opened (or maximised by another app) before show_minimized_drawn finished: never leave it
                # see-through or unable to take the focus.
                self.setWindowOpacity(1.0)
                self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
                log.info("window opened while still being drawn for a minimized start: made whole")
            if self.isMinimized() and getattr(self, "tray", None) is not None:
                QTimer.singleShot(0, self, self._minimized_to_tray)  # (not from inside the state change)
            self._pace_poll()
            if self.isMinimized():
                QTimer.singleShot(1000, self._sweep_if_due)

    def _minimized_to_tray(self) -> None:
        if self.isMinimized() and self.tray.minimized():
            self._pace_poll()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._pace_poll()
        if not getattr(self, "_privacy_set", False):
            self._privacy_set = True
            from sprigcast.app import privacy

            privacy.set_shown_by_capture(privacy.shows_sprigcast(self.collection))
            if privacy.hidden_now():  # Settings → General → Privacy (unless a capture shows SprigCast)
                privacy.apply_to(self, True)
            QTimer.singleShot(1500, self, self.reopen_projectors)  # once the window is up and drawn

    def reopen_projectors(self) -> list:
        """Open the pop-outs that were open last time (Settings → General → Projectors → Reopen them next
        time). One whose bloom, source, canvas or screen is gone is left out: nothing opens somewhere else."""
        from sprigcast.app import projector as pop_outs

        opened = []
        screens = QGuiApplication.screens()
        for was in pop_outs.saved():
            try:
                canvas_id, kind, ref = str(was.get("canvas", "")), str(was.get("kind", "canvas")), str(was.get("ref", ""))
                canvas = next((c for c in self.collection.canvases if c.id == canvas_id), None)
                if canvas is None or not canvas.enabled or kind not in ("canvas", "bloom", "sprig", "source"):
                    continue
                if kind == "bloom" and not any(sc.id == ref for sc in self.collection.scenes):
                    continue
                if kind in ("sprig", "source") and not any(
                        c.id == ref for c in [*self.collection.sources, *self.collection.scenes]):
                    continue
                name, window = str(was.get("screen", "")), was.get("window") or []
                screen = next((s for s in screens if s.name() == name), None) if name else None
                if name and screen is None:
                    continue  # that monitor isn't plugged in now
                if any(p.target == (kind, ref or canvas_id) and (p.screen() is screen if name else not p.isFullScreen())
                       for p in self.projectors):
                    continue  # already open (asked twice)
                projector = self.open_projector(canvas_id, screen, kind, "" if kind == "canvas" else ref)
                if not name and pop_outs.on_a_screen(window, screens):
                    projector.setGeometry(*(int(v) for v in window))
                opened.append(projector)
            except Exception:  # noqa: BLE001  a pop-out that can't come back never stops the others
                log.warning("pop-outs: couldn't reopen %r", was, exc_info=True)
        if opened:
            log.info("pop-outs: reopened %d from last time", len(opened))
        return opened

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._pace_poll()

    POLL_SHOWN_MS, POLL_HIDDEN_MS = 33, 250

    def _pace_poll(self) -> None:
        """Out of sight (minimised, or hidden to the tray) with no projector open, nothing needs 30 picture
        updates a second: the window looks for news 4 times a second instead (OBS stops its preview too).
        Shown again, it's back to 30 at once."""
        timer = getattr(self, "timer", None)
        if timer is None:
            return
        out_of_sight = self.isMinimized() or not self.isVisible()
        projecting = any(p.isVisible() for p in getattr(self, "projectors", []))
        wanted = self.POLL_HIDDEN_MS if out_of_sight and not projecting else self.POLL_SHOWN_MS
        if timer.interval() != wanted:
            timer.setInterval(wanted)
            log.info("window %s: checking for news every %d ms", "out of sight" if wanted > 33 else "shown", wanted)

    def _show_render_health(self, event) -> None:
        """One plain word in the status bar (Smooth / Busy / Struggling); the numbers are in its tooltip."""
        health = getattr(self, "_health", None)
        if health is not None:
            health.render(event)
        from sprigcast.core.scene import drawing_fps

        target = drawing_fps(self.collection.canvases)
        late = max(0, event.dropped_ticks - getattr(self, "_late_before", 0))  # the engine counts them up
        self._late_before = event.dropped_ticks
        budget = 1000 / target
        if event.fps < target * 0.85 or late > target * 0.05:
            word, colour = "Struggling", theme.BAD
        elif event.fps < target * 0.95 or late > 1 or event.render_ms > budget * 0.6:  # one hitch isn't "busy"
            word, colour = "Busy", theme.WARN
        else:
            word, colour = "Smooth", theme.ACCENT
        self._render_word = word
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
                    commands.append(Command(scene.name if canvas.id in scene.name.lower() else f"{scene.name}  ·  {canvas.id.capitalize()}", "Scene",
                                            lambda sid=scene.id: self._switch_to_scene(sid),
                                            hotkeys.get(SCENE_PREFIX + scene.id, ""),
                                            scene.id == self.collection.active_scenes.get(canvas.id)))
        from sprigcast.app import features

        live = self.streaming is not None
        recording = self.recording is not None
        commands += [
            Command("End stream" if live else "Go live", "Action", self._on_live_clicked, hotkeys.get(TOGGLE_STREAM, "")),
            Command("Stop recording" if recording else "Start recording", "Action", self._on_record_clicked,
                    hotkeys.get(TOGGLE_RECORD, "")),
        ]
        if features.on("clips"):
            commands += [
                Command("Clips ready", "Action", lambda: self._toggle_replay(self.replay is None),
                        checked=self.replay is not None),
                Command("Save replay", "Action", self._save_replay, hotkeys.get(SAVE_REPLAY, "")),
            ]
        commands += [
            Command("Stream doctor", "Action", self._open_doctor, "Ctrl+Shift+D"),
            Command("Stream details", "Action", lambda: self.open_stream_details()),
            Command("Optimize for this PC", "Action", lambda: self.open_optimizer()),
            Command("Starter kit", "Action", lambda: self.open_starter()),
            Command("Check for updates", "Action", lambda: self.updater.check(manual=True)),
            Command("Stats", "Panel", self.stats_strip.toggle, "Ctrl+Shift+S", self.stats_strip.is_open()),
            Command("Stream health", "Action", lambda: self._open_health()),
            Command("Light settings for a slower PC", "Action", lambda: self.use_light_settings()),
            Command("Ducking", "Sound", lambda: self._open_ducking()),
        ]
        choice = self.canvas_choice()
        for key, label in [("both", "both canvases"),
                           *((c.id, f"{c.id.capitalize()} only") for c in self.collection.canvases)]:
            commands.append(Command(f"Show {label.lower()}", "Canvas",
                                    lambda k=key: self.show_canvases(k), checked=key == choice))
        for canvas in self.collection.canvases:
            commands.append(Command(f"{canvas.id.capitalize()} projector", "Canvas",
                                    lambda cid=canvas.id: self.open_projector(cid, None)))
        for tab, label in (("General", "General"), ("Appearance", "Appearance"),
                           ("Stream/Places", "Places to stream"),
                           ("Stream/Quality", "Stream quality"), ("Audio", "Audio devices"), ("Video", "Canvases"),
                           ("Video/Virtual camera", "Virtual camera"), ("Recording", "Recording"),
                           ("Recording/Clips", "Clips"), ("Hotkeys", "Hotkeys"),
                           ("Connections", "Connections"), ("Plugins", "Plugin store")):
            commands.append(Command(f"Settings: {label}", "Settings", lambda t=tab: self._open_settings(t),
                                    "Ctrl+," if tab == "General" else ""))
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
        commands += [Command(name, group, switch, checked=on) for group, name, on, switch in self.gardens.switch_choices()]
        if self.mode == "build":  # the selected thing's right-click actions (they work in Build)
            commands += [Command(act.text().rstrip("…"), "Edit", act.trigger, act.shortcut().toString())
                         for act in self.item_actions.values()]
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
            label, _, tail = action.text().partition("\t")  # "Main\t✓": the tick on the one in use
            text = prefix + label.replace("&", "").rstrip("…").strip()
            if action.menu() is not None:
                out += self._menu_commands(action.menu(), group, text + ": ")
            elif action.isEnabled():
                checked = action.isChecked() if action.isCheckable() else ("✓" in tail or None)
                out.append(Command(text, group, action.trigger, action.shortcut().toString(), checked))
        return out

    def clear_all_data(self, parent=None) -> bool:
        """Settings → General → Clear all my data: ask, forget every secret and setting, delete SprigCast's
        folders once it has closed (app/clear_data.py), and close. True if it went ahead."""
        from sprigcast.app import clear_data

        if self.streaming is not None or self.recording is not None or self.replay is not None:
            msgbox.information(parent or self, "Clear all my data", "Stop streaming, recording and clips first.")
            return False
        go, recordings = clear_data.ask(parent or self)
        if not go:
            return False
        self._forgetting = True
        clear_data.clear_all(self._recordings_folder() if recordings else None)
        for window in QApplication.topLevelWidgets():  # Settings and anything else open goes too
            if window is not self and window.isVisible():
                window.close()
        self.close()
        QApplication.quit()
        return True

    def use_light_settings(self, why: str = "") -> bool:
        """Offer the light settings (720p, 30 fps, fast encoder). True if they were applied."""
        from sprigcast.app import lowend

        if self.streaming is not None or self.recording is not None or self.replay is not None:
            msgbox.information(self, "Light settings", "Stop streaming, recording and the replay buffer first.")
            return False
        cpu = pick_codec() == "libx264"
        facts = lowend.pc_facts()
        older = bool(lowend.older_reasons(facts))
        try:
            hdd = bool(lowend.on_hard_drive(self._recordings_folder()))
        except Exception:
            hdd = False
        lines, fps = lowend.light_changes(self.collection, pending_fps=lowend.waiting_fps(), cpu_encoding=cpu,
                                          older=older, hdd=hdd)
        if not lines:
            msgbox.information(self, "Light settings", "You're already on light settings.")
            return False
        text = (why + "\n\n" if why else "") + "Light settings keep streaming smooth on a slower PC:\n\n"
        text += "\n".join(f"•  {line}" for line in lines)
        text += "\n\nYou can change any of it back in Settings."
        box = msgbox.MessageCard("Light settings", text, self, "ask")
        box.add_button("Not now")
        use = box.add_button("Use light settings", primary=True)
        modal(box)
        if box.clicked is not use:
            return False
        _, fps = lowend.light_changes(self.collection, apply=True, pending_fps=lowend.waiting_fps(), cpu_encoding=cpu,
                                      older=older, hdd=hdd)
        waits = lowend.store_fps(fps, self.collection) if fps else False
        lowend.store_tier("light")
        self.collection.save(self.scenes_path)
        self._keep_path_file()  # the path's own file too, at once
        self._after_settings()  # a canvas switched off, clips and browser sources: bring the docks in line
        self.statusBar().showMessage(
            "Light settings are on." + (" Restart SprigCast for 30 fps." if waits else ""), 15000)
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
        if modal(dialog) != QDialog.DialogCode.Accepted:
            return False

        return self._after_starter(build_starter(self.collection, dialog.choice(self._device_source_settings)))

    def _device_source_settings(self, device) -> dict:
        """A camera (or capture card) as source settings: its best mode, as the camera window picks it."""
        probe = CameraDialog([device], {"name": device.name, "path": device.path}, self,
                             max_fps=self._camera_max_fps())
        settings = probe.settings()
        probe.deleteLater()
        return settings

    def _after_starter(self, made: list[str]) -> bool:
        """Show the blooms the Starter kit just made."""
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

    def open_optimizer(self, first_run: bool = False, wants: tuple[bool, bool] | None = None) -> bool:
        """The Optimize for this PC wizard: check the PC, suggest settings. True if any were applied."""
        from sprigcast.app import lowend
        from PySide6.QtWidgets import QDialog

        from sprigcast.app.wizard import OptimizeWizard

        if self.streaming is not None or self.recording is not None or self.replay is not None:
            msgbox.information(self, "Optimize for this PC", "Stop streaming, recording and the replay buffer first.")
            return False
        wizard = OptimizeWizard(self.collection, lowend.waiting_fps(), first_run=first_run, parent=self)
        if wants is not None:  # Get started already asked: live, record
            wizard.wants_stream.setChecked(wants[0])
            wizard.wants_record.setChecked(wants[1])
        accepted = modal(wizard) == QDialog.DialogCode.Accepted
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
        waits = lowend.store_fps(fps, self.collection) if fps else False
        try:
            lowend.store_tier(lowend.tier(lowend.pc_facts()))
        except Exception as exc:  # noqa: BLE001 (only the label on Settings → General)
            log.debug("optimize: no tier: %s", exc)
        self.collection.save(self.scenes_path)
        self._keep_path_file()  # the path's own file too, at once
        self._after_settings()
        self.statusBar().showMessage(
            "Optimized for this PC." + (" Restart SprigCast for the new frame rate." if waits else ""), 15000)
        return True

    def _health_records(self) -> list:
        from sprigcast.app import health

        return health.load(QSettings())

    def _show_last_stream(self) -> None:
        """Live's Destinations card: how the last stream went (opens Stream health)."""
        records = self._health_records()
        self.live_side.set_last_stream(records[0] if records else None)

    def _open_health(self):
        """How the last few streams went: the Garden check, on its Stream health tab."""
        return self._open_garden_check("health")

    def _dropped_now(self) -> int:
        dropped = sum(int(getattr(st, "dropped", 0)) for _, st in self.streaming.streamers) if self.streaming else 0
        return dropped + (int(self.recording.dropped) if self.recording is not None else 0)

    def _refresh_garden(self, ram_high: bool = False) -> None:
        """The header's Garden check pill: CPU, memory and dropped frames, coloured by how things are going."""
        sample = getattr(self, "last_resources", None) or {}
        dropped = self._dropped_now()
        parts = []
        if sample:
            parts += [f"CPU {sample['cpu']:.1f}%", f"{sample['ram_mb']:.0f} MB"]
        parts.append(f"{dropped} dropped")
        word = getattr(self, "_render_word", "Smooth")
        rec_warning = getattr(self, "_rec_warning", "") if self.recording is not None else ""
        level = ("bad" if ram_high or word == "Struggling" else
                 "warning" if dropped or word == "Busy" or rec_warning or self._overload_text() else "good")
        self.live_side.garden.setToolTip("Garden check: how SprigCast and your streams are doing. Click for everything."
                                         + (f" Recording: {rec_warning}" if rec_warning else ""))
        self.live_side.garden.set_numbers("  ·  ".join(parts), level, sample.get("cpu"))
        QTimer.singleShot(0, self, self._fit_header)

    def _open_garden_check(self, tab: str = "now"):
        """How SprigCast and the streams are doing (tiles, stream health, quick checks), and the last streams."""
        from sprigcast.app.garden_check import garden_check_popup

        # Python's lock handed back quickly while it's made and shown, as modal() does for the other pop-ups: this
        # one opens with open(), and went without (Garden check 76 ms, Last stream 57 ms on the PC, 2026-11-18).
        theme.ui_burst()
        pop = garden_check_popup(self._garden_snapshot, self._health_records(), self, self._open_logs,
                                 "health" if tab == "health" else "now", make_lighter=self._make_encoding_lighter)
        pop.open()
        return pop

    def _garden_snapshot(self):
        """Today's numbers for the Garden check: the meter, the render loop, each place and quick checks."""
        import re
        import shutil

        from sprigcast.app.destinations import live_line, problem
        from sprigcast.app.garden_check import GardenSnapshot
        from sprigcast.output.keys import probably_has_key

        sample = getattr(self, "last_resources", None) or {}
        snap = GardenSnapshot(cpu=sample.get("cpu"), ram_mb=sample.get("ram_mb"), gpu=sample.get("gpu"),
                              dropped=self._dropped_now())
        stats = getattr(self, "_last_stats", None)
        if stats is not None:
            snap.render_ms = stats.render_ms
        folder = self._recordings_folder()
        try:
            snap.disk_gb = shutil.disk_usage(folder if folder.exists() else folder.anchor or Path.home()).free / 1e9
        except OSError:
            pass
        if self.streaming is not None:
            sending = 0
            for _, st in self.streaming.streamers:
                level, text = live_line(st)
                kbps = re.match(r"(\d+) kbps", text)
                sending += int(kbps.group(1)) if kbps else 0
                canvas = str(getattr(st, "canvas_id", "") or getattr(st, "canvas", "") or "").capitalize()
                detail = "  ·  ".join(filter(None, [canvas, text.split(" · ")[0], f"{int(getattr(st, 'dropped', 0))} dropped"]))
                snap.rows.append((st.name, detail, level, {"good": "Good", "warning": "Slow", "bad": "Trouble"}
                                  .get(level, "Waiting")))
            snap.upload_kbps = sending
        else:
            for output in [o for o in self.collection.outputs if o.type == "stream"]:
                service = SERVICES.get(output.service or "custom", SERVICES["custom"])
                issue = problem(output, self.collection, probably_has_key) if output.enabled else None
                word, level = ("Off", "idle") if not output.enabled else (issue, "warning") if issue else ("Ready", "good")
                snap.rows.append((service.label, output.canvas.capitalize(), level, word))
        if self.recording is not None:
            seconds = int(self.recording.elapsed)
            snap.recording = f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
            codec = codec_short(self.recording.recorders[0][1].codec) if self.recording.recorders else ""
            dropped = int(self.recording.dropped)
            snap.rows.append(("Recording", "  ·  ".join(filter(None, [codec, f"{dropped} dropped"])),
                              "warning" if dropped else "good", "Busy" if dropped else "Good"))
        snap.checks = self._garden_checks(snap)
        snap.overload = self._overload_text()
        return snap

    def _watch_flow(self, streamers) -> None:
        """Every meter tick: has any stream, recording or replay stopped getting frames (Recorder.check_flow)?
        Said loudly, and when no frames arrive at all the render thread makes its outputs again."""
        import time as _time

        from sprigcast.output.encoder_load import _encoders

        now = _time.monotonic()
        for kind, enc in _encoders(streamers, self.recording, self.replay):
            check = getattr(enc, "check_flow", None)
            try:
                why = check(now) if check is not None else None
            except Exception:
                log.exception("frame flow check")
                continue
            if not why:
                continue
            name = getattr(getattr(enc, "path", None), "name", kind)
            log.error("%s %s is writing no frames while SprigCast runs (%.0f s or more): %s", kind, name,
                      FLOW_STALL_S, why)
            if why.startswith("no frames arrive") and self.engine is not None:
                refresh = getattr(self.engine, "refresh_outputs", None)
                if refresh is not None:
                    refresh()

    def _watch_encoders(self) -> None:
        """Every meter tick: is each running encoder keeping up? (Garden check offers a lighter one if not.)"""
        from sprigcast.output.encoder_load import EncoderWatch

        watch = getattr(self, "_encoder_watch", None)
        if watch is None:
            watch = self._encoder_watch = EncoderWatch()
        streamers = self.streaming.streamers if self.streaming is not None else None
        # Sampled and judged on a worker, read here a tick later: on the UI thread, while recording, it was 62 ms
        # of a meter tick on the PC (2026-11-18). One at a time; a tick that finds the last still going skips.
        job = getattr(self, "_encoder_job", None)
        if job is None or job.done():
            from sprigcast.core import workers

            recording, replay = self.recording, self.replay

            def judge() -> list[str]:
                watch.tick(streamers, recording, replay)
                return watch.overloaded()

            self._encoder_job = workers.run(judge, name="encoder-watch")
        self._watch_flow(streamers)
        busy = getattr(self, "_encoder_busy", [])
        if job is not None and job.done():
            try:
                busy = self._encoder_busy = job.result()
            except Exception:
                log.exception("encoder watch")
        if busy and busy != getattr(self, "_overload_logged", None):
            log.warning("encoder can't keep up: %s (codec %s)", " and ".join(busy),
                        ", ".join(watch.codecs.get(k, "?") for k in busy))
            self._offer_lighter(busy)
        self._overload_logged = busy
        self._watch_card_encoder()

    def _watch_card_encoder(self) -> None:
        """The graphics card's video encoder nearly full (85% or more for 15 s while live or recording) can't
        take a busy moment: frames go missing. Said once a session (the 3-way multistream on 2026-11-18 ran NVENC
        at 89-91% with six encodes and dropped frames in both recordings)."""
        load = (getattr(self, "last_resources", None) or {}).get("encoder")
        now = time.monotonic()
        if load is None or load < CARD_ENCODER_FULL or (self.streaming is None and self.recording is None):
            self._card_full_since = None
            return
        since = getattr(self, "_card_full_since", None)
        if since is None:
            self._card_full_since = now
            return
        if now - since < CARD_ENCODER_FULL_S or getattr(self, "_card_full_said", False):
            return
        self._card_full_said = True
        log.warning("the graphics card's encoder is %.0f%% busy with %d encodes: frames may go missing", load,
                    self._encode_sessions())
        self._saved_note(f"Your graphics card's encoder is {load:.0f}% busy",
                         detail="Fewer outputs or a lower frame rate frees it", kind="warn", ms=20000)

    def _offer_lighter(self, busy: list[str]) -> None:
        """The first overload of a session: say so right away with the one-tap fix, not only in Garden check."""
        if getattr(self, "_lighter_offered", False):
            return
        self._lighter_offered = True

        def tap() -> None:
            self._saved_note(self._make_encoding_lighter())

        self._saved_note(f"The encoder can't keep up with the {' and '.join(busy)}",
                         detail="A faster preset fixes it", link="Make it lighter", on_click=tap, kind="warn",
                         ms=20000)

    def _overload_text(self) -> str:
        watch = getattr(self, "_encoder_watch", None)
        busy = watch.overloaded() if watch is not None else []
        if not busy:
            return ""
        return f"The encoder can't keep up with the {' and '.join(busy)}."

    def _make_encoding_lighter(self) -> str:
        """Garden check's one tap: faster encoder settings for what was overloaded, from the next start."""
        from sprigcast.output.encoder_load import make_lighter

        watch = getattr(self, "_encoder_watch", None)
        busy = watch.overloaded() if watch is not None else []
        done = make_lighter(self.collection, busy or ["stream", "recording"], watch.codecs if watch else None)
        if not done:
            return "Already as light as it goes. Try a smaller size in Settings → Stream."
        self._changed()
        log.info("Garden check: made the %s encoder lighter: stream %s, recording %s", " and ".join(done),
                 self.collection.stream_encoder.preset, self.collection.recording.encoder.preset)
        return f"Lighter settings saved. They start with your next {' and '.join(done)}."

    def _garden_checks(self, snap) -> list[tuple[str, bool, str]]:
        """Quick yes/no checks, only for things this setup has (no camera: no camera check)."""
        now = time.monotonic()
        heard = getattr(self, "_heard_at", {})
        checks = []
        for kind, label, ok_tip, bad_tip in (
                ("audio_input", "Mic heard", "Your mic picked up sound in the last half minute",
                 "No sound from your mic in the last half minute: is it muted or unplugged?"),
                ("audio_output", "Desktop sound", "Desktop sound is coming through",
                 "No desktop sound in the last half minute (fine if nothing is playing)")):
            ids = [s.id for s in self.collection.sources if s.kind == kind]
            if ids:
                ok = any(now - heard.get(i, -1e9) < 30 for i in ids)
                # A quiet mic only matters on air; quiet desktop sound is fine when nothing plays.
                on_air = kind == "audio_input" and (self.streaming is not None or self.recording is not None)
                checks.append((label, True if ok else False if on_air else None, ok_tip if ok else bad_tip))
        cameras = [s.id for s in self.collection.sources if s.kind == "camera"]
        if cameras:
            sizes = getattr(self, "source_sizes", {}) or {}
            ok = any((sizes.get(i) or (0, 0))[0] > 0 for i in cameras)
            checks.append(("Camera on", ok, "Your camera is sending pictures" if ok else "No picture from your camera"))
        if self.streaming is not None:
            backlog = max((float(st.stats().get("backlog_s", 0) or 0) for _, st in self.streaming.streamers
                           if hasattr(st, "stats")), default=0.0)
            checks.append(("Upload has room", backlog < 1.0, "Everything is going out on time" if backlog < 1.0
                           else f"{backlog:.1f} s waiting to upload: your connection is behind"))
        if snap.disk_gb is not None:
            ok = snap.disk_gb >= 10
            checks.append(("Disk has room", ok, f"{snap.disk_gb:.0f} GB free for recordings"))
        from sprigcast.app.destinations import room_line

        room = room_line((getattr(self, "last_resources", None) or {}).get("encoder"), self._encode_sessions())
        if room is not None:
            checks.append(("Encoder has room", room[0], room[1]))
        codec = codec_short(pick_codec())
        gpu = any(name in codec for name in ("NVENC", "AMF", "Quick Sync"))
        checks.append(("GPU encoder", gpu, f"Recording and streaming use {codec}" if gpu
                       else "No graphics-card encoder: the CPU does the encoding (slower)"))
        return checks

    def _encode_sessions(self) -> int:
        """Video encodes running now: one per canvas for recording and clips, one per shared stream group,
        one per other destination."""
        count = len(getattr(getattr(self, "encoders", None), "encoders", {}) or {})
        if self.streaming is not None:
            shared = getattr(self.streaming, "shared", [])
            linked = {id(link) for enc in shared for link in enc.links}
            count += len(shared) + sum(1 for _, st in self.streaming.streamers if id(st) not in linked)
        return count

    def _open_logs(self) -> None:
        from sprigcast.app.logbook import log_dir

        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        open_folder(folder)

    def first_run_optimizer(self) -> None:
        """Once on a fresh install: Get started (which makes the blooms too), then Optimize for this PC
        (it also covers the old light-settings suggestion), each offered only once."""
        from sprigcast.app import getting_started, optimize
        from sprigcast.app.starter import is_fresh

        settings = QSettings()
        answers = None
        if not settings.value(getting_started.DONE, False, type=bool):
            settings.setValue(getting_started.DONE, True)
            if is_fresh(self.collection):  # only the example blooms: a new install
                answers = self.open_getting_started()
        if settings.value(optimize.DONE, False, type=bool):
            return
        settings.setValue(optimize.DONE, True)
        if answers is not None:
            self.open_optimizer(wants=(answers.live, answers.record))
            return
        self.open_optimizer(first_run=True)
        if is_fresh(self.collection):  # only the example blooms: offer to make real ones
            self.open_starter()

    def open_getting_started(self):
        """Get started: what you make, live or record, which shape, where to. Sets up a garden named after
        the show, a path named after where it goes (with a place per pick), the canvases, then makes the
        blooms that fit with the gear picked in step 2. Returns the answers, or None if closed."""
        from PySide6.QtWidgets import QDialog

        from sprigcast.app import getting_started as gs
        from sprigcast.app.starter import is_fresh
        from sprigcast.core.gardens import DEFAULT_GARDEN, DEFAULT_PATH, ShelfError
        from sprigcast.output import twitch_auth

        if self.gardens.busy():
            msgbox.information(self, "Get started", "Stop streaming and recording first.")
            return None

        def connect_twitch(parent):
            from sprigcast.app.twitch_connect import TwitchConnectDialog

            dialog = TwitchConnectDialog(parent)
            return (dialog.account, dialog.stream_key) if modal(dialog) and dialog.account else None

        def twitch_account():
            try:
                return twitch_auth.connected_account()
            except Exception:  # noqa: BLE001 (no keyring: just not signed in)
                return None

        def list_gear() -> gs.Gear:
            return gs.Gear(cameras=list_cameras(), mics=[d.name for d in list_audio_devices(loopback=False)],
                           speakers=[d.name for d in list_audio_devices(loopback=True, rescan=False)])

        from sprigcast.app.signin_dialog import sign_in
        from sprigcast.output import service_signin

        dialog = gs.GettingStartedDialog(self, twitch_account=twitch_account, connect_twitch=connect_twitch,
                                         list_gear=list_gear, can_sign_in=service_signin.available,
                                         sign_in=sign_in)
        if modal(dialog) != QDialog.DialogCode.Accepted:
            log.info("get started: closed")
            return None
        answers = dialog.answers()
        log.info("get started: %s, live %s, record %s, %s, places %s", answers.content, answers.live,
                 answers.record, answers.shape, ",".join(answers.places) or "none")
        shelf = self.gardens.shelf
        fresh = is_fresh(self.collection)
        try:
            # Garden: a fresh install's garden takes the show's name; otherwise a new one is made for it.
            garden_name = gs.free_name(gs.content_label(answers.content),
                                       [g.name for g in shelf.gardens() if g.id != shelf.garden().id])
            if fresh:
                if shelf.garden().name == DEFAULT_GARDEN:
                    shelf.rename("garden", shelf.garden().id, garden_name)
            else:
                garden_name = gs.free_name(garden_name, [g.name for g in shelf.gardens()])
                entry = self.gardens._new("garden", garden_name)
                problem = self.gardens.switch("garden", entry.id)
                if problem:
                    msgbox.information(self, "Get started", problem)
                    return None
            # Path: a new install's (or one with no places yet) takes the new name; otherwise a new one is made.
            path_name = gs.plan_path_name(answers)
            current = shelf.path()
            has_places = any(o.type == "stream" for o in self.collection.outputs)
            if (fresh or not has_places) and current.name == DEFAULT_PATH:
                shelf.rename("path", current.id,
                             gs.free_name(path_name, [p.name for p in shelf.paths() if p.id != current.id]))
            else:
                entry = self.gardens._new("path", gs.free_name(path_name, [p.name for p in shelf.paths()]))
                problem = self.gardens.switch("path", entry.id)
                if problem:
                    msgbox.information(self, "Get started", problem)
                    return None
        except ShelfError as exc:
            msgbox.information(self, "Get started", str(exc))
            return None
        places = gs.apply_path(answers, self.collection, self.gardens.taken_output_ids())
        if dialog.twitch is not None:
            account, key = dialog.twitch
            for output in places:
                if output.service == "twitch":
                    output.twitch_account = True
                    if key:
                        try:
                            from sprigcast.output.keys import set_key

                            set_key(output.key_ref, key)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("get started: couldn't save the Twitch key: %s", exc)
        elif any(o.service == "twitch" for o in places) and twitch_account():
            for output in places:
                if output.service == "twitch":
                    output.twitch_account = True
        for output in places:  # YouTube / Kick signed in: their key (and Kick's server) is already here
            fetched = dialog.fetched.get(output.service)
            if fetched is None:
                continue
            if fetched.server:
                output.server = fetched.server
            try:
                from sprigcast.output.keys import set_key

                set_key(output.key_ref, fetched.key)
            except Exception as exc:  # noqa: BLE001
                log.warning("get started: couldn't save the %s key: %s", output.service, exc)
        self.show_canvases(answers.shape)
        shelf.sync_path(self.collection)
        self._changed()
        self._refresh_stream_boxes()
        self.gardens._changed()
        self._grow_from_gear(answers, dialog.gear(), dialog.found or gs.Gear())
        self.show_canvases(answers.shape)  # vertical versions switch both canvases on: keep the shape picked
        from sprigcast.app.destinations import problem
        from sprigcast.output.keys import get_key

        def has_key(ref):
            try:
                return bool(get_key(ref))
            except Exception:  # noqa: BLE001
                return False

        waiting = next((o for o in self.collection.outputs if o.type == "stream"
                        and problem(o, self.collection, has_key)), None)
        if waiting is not None:  # a key or server still to paste: straight to that place
            self._open_settings("Stream", waiting.id)
        self.statusBar().showMessage(f"Set up {garden_name} on {shelf.path().name}.", 8000)
        return answers

    def _grow_from_gear(self, answers, picks, gear) -> list[str]:
        """Get started's step 2: the blooms for the show, made with the camera, capture card, mic and sound."""
        from sprigcast.app import getting_started as gs
        from sprigcast.app.starter import StarterChoice, build_starter, is_fresh

        blooms, names = gs.plan_blooms(answers)
        monitor = None
        if "Gaming" in blooms and picks.capture is None:  # the screen, for the Gaming (or Screen) bloom
            monitors = list_monitors()
            m = next((m for m in monitors if m.primary), monitors[0] if monitors else None)
            monitor = (m.index, m.width, m.height) if m is not None else None
        choice = StarterChoice(
            camera=self._device_source_settings(picks.camera) if picks.camera is not None else None,
            monitor=monitor, mic=picks.mic, desktop=None, app_sounds=gs.plan_sound(), blooms=blooms,
            vertical=answers.shape != "horizontal", replace_example=is_fresh(self.collection), names=names,
            capture=self._device_source_settings(picks.capture) if picks.capture is not None else None,
            capture_audio=gs.capture_audio(picks.capture.name, gear.mics) if picks.capture is not None else None)
        made = build_starter(self.collection, choice)
        self._after_starter(made)
        return made

    def _open_palette(self) -> CommandPalette:
        theme.ui_burst()  # (as modal() does)
        palette = CommandPalette(self._palette_commands(), self)
        palette.open()
        return palette

    def _hotkey_note(self, text: str) -> None:
        """A hotkey that couldn't do anything says why: in the status bar, and back to an app that ran it."""
        self._last_hotkey_note = text
        self._say(text)

    def _on_hotkey(self, action: str) -> None:
        from sprigcast.app.hotkeys import MUTE_ALL, SOUND_PREFIX, TOGGLE_CLIPS, TOGGLE_MODE

        self._last_hotkey_note = None
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
            self._screenshot(background=True)
        elif action.startswith("plugin:"):
            plugin_hooks.run_hotkey(self.api, action, self._hotkey_note)
        elif action.startswith(GAME_PREFIX):
            self._game_grab(action[len(GAME_PREFIX):])
        elif action.startswith(MEDIA_PREFIX):
            source_id, _, what = action[len(MEDIA_PREFIX):].rpartition(":")
            self.media_control(source_id, what)
        elif action == TOGGLE_STREAM:
            self._toggle_streaming()
        elif action == TOGGLE_RECORD:
            self._toggle_recording()
        elif action == PAUSE_RECORD:
            self.pause_recording(None)
        elif action == CHAPTER_MARK:
            self.mark_chapter()
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
            upload_kbps=doctor.usable_upload(QSettings()),
            unreachable=[] if self.streaming is not None else list(getattr(self, "_unreachable_lines", [])),
            camera_sizes=self._camera_sizes(),
        )

    def _camera_sizes(self) -> dict:
        """Each open camera or capture card: (the size its device delivers, the size its source uses)."""
        sizes = {}
        for source_id, source in list(self.engine._sources.items()):
            feed = getattr(source, "_feed", None)
            given = getattr(feed, "given", None) or getattr(feed, "picture", None)
            used = getattr(source, "_size", None)  # (never `size`: a text would be drawn for it)
            if feed is not None and given and isinstance(used, tuple) and len(used) == 2 and min(used) > 0:
                sizes[source_id] = ((int(given[0]), int(given[1])), (int(used[0]), int(used[1])))
        return sizes

    def _doctor_warnings(self) -> list[str]:
        """The Stream doctor's quick check when going live: only serious problems."""
        checkup = self._checkup()
        checkup.gpu_pct = None  # nvidia-smi can take a moment; the full doctor checks it
        return [f.text for f in doctor.examine(checkup) if f.level == "bad"]

    def _doctor_says_go(self, first: list[str] | None = None) -> bool:
        """Quick check when going live: only serious problems ask first. first: what going live found itself
        (two places on one stream key), shown above the doctor's."""
        bad = [*(first or []), *self._doctor_warnings()]
        if not bad:
            return True
        box = msgbox.MessageCard("Stream doctor", "\n\n".join(bad), self, "warn", width=520)
        box.add_button("Cancel")
        doctor_button = box.add_button("Open doctor")
        go = box.add_button("Go live anyway", primary=True)
        modal(box)
        if box.clicked is go:
            return True
        if box.clicked is doctor_button:
            self._open_doctor()
        return False

    def _open_doctor(self) -> None:
        modal(doctor.DoctorDialog(self._checkup, self))

    def _refresh_stats_panel(self) -> None:
        outputs = []
        if self.streaming is not None:
            outputs += [(f"Stream: {st.name}", st) for _, st in self.streaming.streamers]
        if self.recording is not None:
            outputs += [(f"Recording: {canvas_id}", rec) for canvas_id, rec in self.recording.recorders]
        self.stats_panel.refresh(self._last_stats, outputs, self._recordings_folder())
