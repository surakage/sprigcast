"""The Settings window: one place for every option, a page for each topic picked from the side.

Six pages, each answering one question: General (how the app behaves, and This PC), Stream (where you go
live and how good), Recording (where recordings and clips go, and how good), Audio, Video (canvases and
the virtual camera) and Connections. Hotkeys and the plugin store have windows of their own (Tools menu).

Rows most people never need (encoder internals, output scaling, ports...) show only with Expert settings
on, a tick at the bottom that's remembered. OK and Apply write the changes into the scene collection and
QSettings; the main window then refreshes itself (see MainWindow._after_settings).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from PySide6.QtCore import QSettings, QStringListModel, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import autostart, priority, theme
from sprigcast.app.live_stage import heading
from sprigcast.app.output_page import (  # noqa: F401 (stream_delay and apply_naming are imported from here)
    CameraTab,
    RecordingTabs,
    StreamQuality,
    _scrolled,
    apply_naming,
    stream_delay,
)
from sprigcast.app.recording_dialog import RecordingPage
from sprigcast.app.stream_dialog import StreamPage
from sprigcast.app.theme import THEMES, chosen_theme
from sprigcast.core.scene import SceneCollection

log = logging.getLogger(__name__)

TABS = ["General", "Stream", "Recording", "Audio", "Video", "Connections"]
# Older names, and the windows that left Settings, land somewhere sensible ("Tab/Sub": a page's own tab).
TAB_ALIASES = {"Output": "Stream/Quality", "Clips": "Recording/Clips", "Replay": "Recording/Clips",
               "Advanced": "General", "Camera": "Video/Virtual camera", "Hotkeys": "General", "Plugins": "General"}
ABOUT = {  # one line under each page's title
    "General": "How SprigCast behaves, and how much this PC carries.",
    "Stream": "Where you go live, and how good it looks.",
    "Recording": "Where recordings and clips go, and how good they look.",
    "Audio": "Devices, sound tracks and which sounds go on each.",
    "Video": "Your canvases and the virtual camera.",
    "Connections": "Let other apps on your PC control SprigCast.",
}
EXPERT = "settings/expert"  # QSettings: show the expert rows
PREVIEW_LIVE = "general/preview_live"  # QSettings: the previews while live or recording
PREVIEW_CHOICES = {"smooth": "Smooth", "light": "Light (15 fps)", "off": "Off"}
PREVIEW_FPS = {"smooth": 30, "light": 15, "off": 0}

# Settings search: what you might type, and where it lives ("Tab/Sub"). expert: shown only with Expert on.
SEARCH: list[tuple[str, str, bool]] = [
    ("Theme", "General", False), ("Start with Windows", "General", False), ("Start minimised", "General", False),
    ("Updates", "General", False), ("Ask before ending a stream", "General", False),
    ("Ask before stopping a recording", "General", False), ("Keep logs", "General", False),
    ("This PC", "General", False), ("Optimize for this PC", "General", False),
    ("Process priority", "General", False), ("Sources not on screen", "General", False),
    ("Preview while live", "General", False),
    ("Places to stream", "Stream/Places", False), ("Stream key", "Stream/Places", False),
    ("Server", "Stream/Places", False), ("Resolution", "Stream/Places", False),
    ("Frame rate (stream)", "Stream/Places", False), ("Bitrate (stream)", "Stream/Places", False),
    ("Auto-pilot bitrate", "Stream/Places", False), ("Twitch Enhanced Broadcasting", "Stream/Places", False),
    ("Bandwidth test (Test only)", "Stream/Places", False), ("Ignore service recommendations", "Stream/Places", True),
    ("Stream encoder", "Stream/Quality", False), ("Stream quality", "Stream/Quality", False),
    ("Stream delay", "Stream/Quality", False), ("Rate control", "Stream/Quality", True),
    ("Keyframe interval", "Stream/Quality", True), ("Preset", "Stream/Quality", True),
    ("B-frames", "Stream/Quality", True), ("Look-ahead", "Stream/Quality", True),
    ("Audio bitrate (stream)", "Stream/Quality", True),
    ("Recordings folder", "Recording", False), ("File type", "Recording", False), ("Repot as MP4", "Recording", False),
    ("Recording size", "Recording", False), ("Recording bitrate", "Recording", False),
    ("Same as stream", "Recording", False), ("Record when I go live", "Recording", False),
    ("Split files", "Recording", False), ("Source record", "Recording", False), ("Recording encoder", "Recording", False),
    ("Recording quality", "Recording", False), ("Space", "Recording", False), ("Keep screenshots", "Recording", False),
    ("File names", "Recording", True), ("Audio bitrate (recordings)", "Recording", True),
    ("Clips", "Recording/Clips", False), ("Clip length", "Recording/Clips", False),
    ("Replay buffer", "Recording/Clips", False),
    ("Sound devices", "Audio/Devices", False), ("Mic", "Audio/Devices", False), ("Desktop audio", "Audio/Devices", False),
    ("Tracks", "Audio/Tracks", False), ("Advanced audio", "Audio/Sounds", False), ("Ducking", "Audio/Sounds", False),
    ("Canvas size", "Video/Canvases", False), ("Canvas frame rate", "Video/Canvases", False),
    ("Vertical canvas", "Video/Canvases", False), ("Output (scaled) size", "Video/Canvases", True),
    ("Downscale filter", "Video/Canvases", True), ("Virtual camera", "Video/Virtual camera", False),
    ("Control API", "Connections", False), ("Password (API)", "Connections", False),
    ("Pair a device", "Connections", False), ("Streamer.bot", "Connections", False),
    ("OBS-style connection", "Connections", False), ("Port", "Connections", False),
    ("Phones on my network", "Connections", True), ("Web links", "Connections", True),
]


def window_style() -> str:
    """The Settings look: a pill menu on the left, soft cards with titles inside, borderless fields."""
    t = theme
    return f"""
#settingsWindow {{ background: {t.BG}; border: none; }}
#settingsNav {{ background: transparent; border: none; padding: 0px; outline: none; }}
#settingsNav::item {{ color: {t.MUTED}; padding: 10px 16px; margin: 2px 0px; border-radius: 18px; border: none; }}
#settingsNav::item:hover {{ background: {t.PANEL}; color: {t.TEXT}; }}
#settingsNav::item:selected {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; font-weight: 600; }}
#settingsWindow QGroupBox {{ background: {t.CARD}; border: none; border-radius: 20px; margin-top: 0px;
    padding: 46px 18px 16px 18px; font-weight: 400; }}
#settingsWindow QGroupBox::title {{ subcontrol-origin: padding; subcontrol-position: top left; left: 18px;
    top: 16px; padding: 0px; color: {t.TEXT_BRIGHT}; font-size: 14px; font-weight: 600; }}
#settingsWindow QLineEdit, #settingsWindow QSpinBox, #settingsWindow QDoubleSpinBox,
#settingsWindow QComboBox, #settingsWindow QPlainTextEdit {{ background: {t.PANEL}; border: none;
    border-radius: 10px; padding: 7px 14px; min-height: 20px; }}
#settingsWindow QSpinBox, #settingsWindow QDoubleSpinBox {{ padding-right: 24px; }}
#settingsWindow QLabel#valueChip {{ background: {t.PANEL}; border-radius: 10px; padding: 7px 14px; }}
#settingsWindow QLineEdit:focus, #settingsWindow QSpinBox:focus, #settingsWindow QDoubleSpinBox:focus,
#settingsWindow QComboBox:focus {{ background: {t.RAISED}; }}
#settingsWindow QComboBox::drop-down {{ border: none; width: 26px; background: transparent; }}
#settingsWindow QSpinBox::up-button, #settingsWindow QSpinBox::down-button,
#settingsWindow QDoubleSpinBox::up-button, #settingsWindow QDoubleSpinBox::down-button {{
    border: none; background: transparent; width: 18px; }}
#settingsWindow QTabWidget::pane {{ border: none; background: transparent; top: 8px; }}
#settingsWindow QTabBar::tab {{ background: transparent; color: {t.MUTED}; border: none; border-radius: 15px;
    padding: 7px 18px; margin-right: 6px; min-height: 18px; }}
#settingsWindow QTabBar::tab:hover {{ background: {t.PANEL}; color: {t.TEXT}; }}
#settingsWindow QTabBar::tab:selected {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; font-weight: 600;
    border: none; }}
#settingsWindow QScrollArea {{ background: transparent; border: none; }}
#settingsWindow QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""
FPS_CHOICES = [24, 25, 30, 48, 50, 60, 120]
# Output (scaled) resolution: the short side viewers and recordings get. None = the base size.
OUTPUT_SIDES = [None, 1440, 1080, 936, 900, 864, 720, 540, 480, 360]
DOWNSCALE_LABELS = {
    "bilinear": "Bilinear (fastest, a little soft)",
    "area": "Area (smooth, no shimmer)",
    "bicubic": "Bicubic (sharp, 16 samples)",
    "lanczos": "Lanczos (sharpest, 36 samples)",
}
AUDIO_KINDS = ("audio_input", "audio_output", "app_audio")

# QSettings keys
SAVE_STYLE = theme.PILL_STYLE + (
    f"QPushButton {{ background: {theme.ACCENT}; color: {theme.ACCENT_TEXT}; }}"
    f"QPushButton:hover {{ background: {theme.ACCENT_HOVER}; }}")

CONFIRM_STREAM = "general/confirm_end_stream"
CONFIRM_RECORDING = "general/confirm_stop_recording"
PENDING_VIDEO = "video/pending"  # canvas changes saved here, applied at the next start


def confirm_enabled(key: str) -> bool:
    return QSettings().value(key, True, type=bool)


def pending_video() -> dict[str, dict[str, Any]]:
    raw = QSettings().value(PENDING_VIDEO, "")
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


SLEEP_SETTING = "general/sleep_sources"
SLEEP_CHOICES = {
    "not_cameras": "Pause (cameras after 30 s, unless set to stay ready)",
    "all": "Pause everything (lowest memory)",
    "off": "Keep running",
}


def sleep_policy() -> str:
    """What to do with sources no scene on screen shows (Engine.sleep_policy)."""
    value = QSettings().value(SLEEP_SETTING, "not_cameras")
    return value if value in SLEEP_CHOICES else "not_cameras"


def preview_choice() -> str:
    """How the previews draw while live or recording: "smooth", "light" or "off"."""
    value = QSettings().value(PREVIEW_LIVE, "smooth")
    return value if value in PREVIEW_CHOICES else "smooth"


def expert_on() -> bool:
    return QSettings().value(EXPERT, False, type=bool)


def apply_pending_video(collection: SceneCollection) -> bool:
    """At startup, move canvas changes from Settings → Video into the collection. True if any applied."""
    pending = pending_video()
    if not pending:
        return False
    for canvas in collection.canvases:
        change = pending.get(canvas.id)
        if change:
            canvas.width = int(change.get("width", canvas.width))
            canvas.height = int(change.get("height", canvas.height))
            canvas.fps = int(change.get("fps", canvas.fps))
            canvas.enabled = bool(change.get("enabled", canvas.enabled))
    QSettings().remove(PENDING_VIDEO)
    return True


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    return label


def _devices(devices) -> dict[str, list[str]]:
    """The audio devices: a dict, or a Future of one (scanned on a worker); waited for with the window drawing."""
    if hasattr(devices, "result"):
        from sprigcast.core.workers import wait_answering

        try:
            return wait_answering(devices.result) if not devices.done() else devices.result()
        except Exception as exc:  # noqa: BLE001 (no sound system: empty lists)
            log.warning("settings: couldn't list sound devices: %s", exc)
            return {}
    return devices


def _page(*widgets: QWidget, stretch: bool = True) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return page


FILL_AFTER_MS = 30  # the window shows first, then its page is made (fill_after_show)
SLICE_GAP_MS = 20  # between the parts of a page made a tick apart (SettingsWindow._slice)


class SettingsWindow(QDialog):
    applied = Signal()
    optimize_requested = Signal()  # This PC → Optimize again (the main window opens it once Settings closes)
    restart_requested = Signal()  # the restart bar's Restart now

    def __init__(
        self,
        collection: SceneCollection,
        *,
        default_recordings_folder: str,
        encoder: str,
        audio_devices,  # {"audio_input": [...], "audio_output": [...]}, or a Future of that
        streaming: bool = False,
        recording: bool = False,
        control=None,
        check_updates=None,
        tab: str = "General",
        parent: QWidget | None = None,
        fill_after_show: bool = False,
        **_old,  # plugin_report, open_plugins_folder, plugins_dir: the plugin store has its own window now
    ):
        theme.ui_burst()  # from the first line: Python's lock comes back quickly while the window is made
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setObjectName("settingsWindow")
        self.setStyleSheet(window_style())
        self.resize(1060, 700)
        self.collection = collection
        self.default_recordings_folder = default_recordings_folder
        self.streaming, self.recording = streaming, recording
        self.check_updates = check_updates
        self._expert = expert_on()

        self.tabs = QListWidget()  # the side menu (a list, so keys and screen readers work)
        self.tabs.setObjectName("settingsNav")
        self.tabs.setFixedWidth(156)
        self.tabs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        font = self.tabs.font()
        font.setPointSizeF(font.pointSizeF() * 1.1)
        self.tabs.setFont(font)
        self.stack = QStackedWidget()
        # Each page is made when it's first shown, the rest one at a time once the window is up: making all
        # of them before showing anything kept the window from answering 2.4 s on the PC (2026-11-16).
        self._makers = {
            "General": self._general_page,
            "Stream": lambda: self._stream_page(encoder),
            "Recording": lambda: self._recording_page(encoder),
            "Audio": lambda: self._audio_page(lambda: _devices(audio_devices)),
            "Video": self._video_page,
            "Connections": lambda: self._connections_page(control),
        }
        self._built: set[str] = set()
        self._slices: list = []  # parts of pages still to make, one per tick (_slice)
        self._audio_devices = audio_devices
        self._subs: dict[str, str] = {}  # page -> its own tab to open on once it's made
        self.on_ducking = None  # passed on to the Audio page (the main window opens the Ducking window)
        self.on_clear_data = None  # the main window's clear_all_data(parent)
        for name in TABS:
            QListWidgetItem(name, self.tabs)
            self.stack.addWidget(QWidget())  # stands in until the page is made
        self.tabs.currentRowChanged.connect(self._show_row)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(156)
        completer = QCompleter(QStringListModel([label for label, _where, _x in SEARCH]), self.search)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self.find)
        self.search.setCompleter(completer)
        self.search.returnPressed.connect(lambda: self.find(self.search.text()))

        # Only this page is made, the others when their tab is picked (making them all up front held SprigCast up
        # for seconds while live). fill_after_show: not even this one before the window is up. The window shows at
        # once and its page fills in a moment later; made first, a click on a destination took 352 ms to show
        # anything on the PC (2026-11-18), the canvas preview standing still meanwhile.
        self._fill_tab: str | None = tab if fill_after_show else None
        self._after_fill: list = []
        if self._fill_tab is None:
            self.open_tab(tab)
        else:
            name = TAB_ALIASES.get(tab, tab).partition("/")[0]
            row = TABS.index(name) if name in TABS else 0
            self.tabs.blockSignals(True)
            self.tabs.setCurrentRow(row)
            self.tabs.blockSignals(False)
            self.stack.setCurrentIndex(row)

        side = QVBoxLayout()
        side.setSpacing(14)
        side.addWidget(heading("Settings", 18))
        side.addWidget(self.search)
        side.addWidget(self.tabs, 1)
        body = QHBoxLayout()
        body.setSpacing(28)
        body.addLayout(side)
        body.addWidget(self.stack, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.expert = QCheckBox("Expert settings")
        self.expert.setToolTip("Encoder internals, output scaling, ports and other rows most people never need")
        self.expert.setChecked(self._expert)
        self.expert.toggled.connect(self.set_expert)
        buttons.addWidget(self.expert)
        buttons.addSpacing(16)
        self.restart_note = QLabel("")
        self.restart_note.setStyleSheet(f"color: {theme.MUTED}")
        buttons.addWidget(self.restart_note)
        self.restart_button = QPushButton("Restart now")
        self.restart_button.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.restart_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restart_button.setAutoDefault(False)
        self.restart_button.clicked.connect(self._restart)
        buttons.addWidget(self.restart_button)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.apply_button = QPushButton("Apply")
        self.save_button = QPushButton("Save")
        for button, style, slot in ((self.cancel_button, theme.PILL_STYLE, self.reject),
                                    (self.apply_button, theme.PILL_STYLE, self.apply),
                                    (self.save_button, SAVE_STYLE, self._ok)):
            button.setStyleSheet(style)
            button.setMinimumWidth(96)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        self.save_button.setDefault(True)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        root.addLayout(body, 1)
        root.addLayout(buttons)
        self._show_restart_note()

    @staticmethod
    def _titled(name: str, page: QWidget) -> QWidget:
        """A page with its title and a one-line note about what it holds."""
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        col.addWidget(heading(name, 20))
        note = _hint(ABOUT.get(name, ""))
        col.addWidget(note)
        col.addSpacing(12)
        col.addWidget(page, 1)
        return holder

    # Opening a page ----------------------------------------------------------------

    def open_tab(self, tab: str) -> None:
        """Show a page, or one of its own tabs ("Audio/Sounds"). Older names are taken too."""
        tab = TAB_ALIASES.get(tab, tab)
        tab, _, sub = tab.partition("/")
        if tab not in TABS:
            tab = "General"
        if sub:
            self._subs[tab] = sub
            if tab in self._built:
                self._show_sub(tab)
        row = TABS.index(tab)
        if self.tabs.currentRow() == row:
            self._show_row(row)  # already current: no change signalled
        else:
            self.tabs.setCurrentRow(row)

    def find(self, text: str) -> None:
        """Search: go to the page (and tab) that holds what was typed."""
        words = text.lower().split()
        if not words:
            return
        hits = [(label, where, x) for label, where, x in SEARCH if all(w in label.lower() for w in words)]
        if not hits:
            hits = [(label, where, x) for label, where, x in SEARCH if any(w in label.lower() for w in words)]
        if not hits:
            return
        label, where, expert = next((h for h in hits if h[0].lower() == text.lower()), hits[0])
        if expert and not self.expert.isChecked():
            self.expert.setChecked(True)
        self.open_tab(where)

    def _show_sub(self, tab: str) -> None:
        sub = self._subs.pop(tab, "")
        holder = {"Stream": "stream_tabs", "Recording": "recording_tabs", "Audio": "audio_page",
                  "Video": "video_tabs"}.get(tab)
        page = self.__dict__.get(holder) if holder else None
        if page is None or not sub:
            return
        if isinstance(page, QTabWidget):
            for i in range(page.count()):
                if page.tabText(i) == sub:
                    page.setCurrentIndex(i)
        elif hasattr(page, "show_tab"):
            page.show_tab(sub)

    # Making pages ------------------------------------------------------------------

    _PAGE_OF = {"stream_page": "Stream", "stream_quality": "Stream", "stream_tabs": "Stream",
                "recording_tabs": "Recording", "recording_page": "Recording", "audio_page": "Audio",
                "video_tabs": "Video", "camera_tab": "Video", "video_fields": "Video", "video_output": "Video",
                "connections_page": "Connections"}

    def __getattr__(self, name: str):
        """A page's widgets asked for before the page was made: make that page (not knowing which: up to the one)."""
        makers = self.__dict__.get("_makers")
        if makers is None or name.startswith("_"):
            raise AttributeError(name)
        if self.__dict__.get("_slices"):  # part of a page made a tick later: now, it's wanted
            self._flush_slices()
            if name in self.__dict__:
                return self.__dict__[name]
        if len(self.__dict__.get("_built", ())) == len(TABS):
            raise AttributeError(name)
        page = self._PAGE_OF.get(name)
        if page is not None and page not in self._built:
            self.ensure(page)
        else:  # page by page, stopping at the one that has it
            for page in TABS:
                if name in self.__dict__:
                    break
                self.ensure(page)
        return object.__getattribute__(self, name)

    def ensure(self, name: str) -> None:
        """Make this page now if it isn't yet."""
        if name in self._built:
            return
        self._built.add(name)
        import time

        began = time.perf_counter()
        page = self._makers[name]()
        if name == "Audio":
            self.audio_page.on_ducking = lambda page: self.on_ducking and self.on_ducking(page)
        if hasattr(page, "set_expert"):
            page.set_expert(self._expert)
        if name not in ("Stream", "Recording", "Video"):  # those scroll each of their own tabs
            page = _scrolled(page)
        index = TABS.index(name)
        current = self.stack.currentIndex()
        placeholder = self.stack.widget(index)
        self.stack.insertWidget(index, self._titled(name, page))
        self.stack.removeWidget(placeholder)
        placeholder.deleteLater()
        self.stack.setCurrentIndex(current)
        self._show_sub(name)
        took = 1000 * (time.perf_counter() - began)
        if took >= 100:
            log.info("settings: the %s page took %.0f ms to make", name, took)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_tab is not None:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(FILL_AFTER_MS, self._fill)  # after the window's first paint

    def _fill(self) -> None:
        tab, self._fill_tab = self._fill_tab, None
        if tab is None or getattr(self, "_closed", False):
            return
        self.open_tab(tab)
        later, self._after_fill = self._after_fill, []
        for call in later:
            call()

    def _slice(self, call) -> None:
        """Part of a page made a tick later, one part per tick: the window answers and the canvases get their
        turn in between (a whole Audio or Stream page at once held both 200+ ms on the PC, 2026-11-18)."""
        from PySide6.QtCore import QTimer

        self._slices.append(call)
        if len(self._slices) == 1:
            QTimer.singleShot(SLICE_GAP_MS, self._next_slice)

    def _next_slice(self) -> None:
        if not self._slices:
            return
        if getattr(self, "_closed", False):
            self._slices.clear()
            return
        self._slices.pop(0)()
        if self._slices:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(SLICE_GAP_MS, self._next_slice)

    def _flush_slices(self) -> None:
        while self._slices:
            self._slices.pop(0)()

    def soon(self, call) -> None:
        """call() once the first page is made (now, if it is)."""
        if self._fill_tab is None:
            call()
        else:
            self._after_fill.append(call)

    def done(self, result: int) -> None:  # Save, Cancel, Esc or the window's X
        self._closed = True
        super().done(result)

    def ensure_all(self) -> None:
        for name in TABS:
            self.ensure(name)
        self._flush_slices()

    def _show_row(self, row: int) -> None:
        if 0 <= row < len(TABS):
            self.ensure(TABS[row])
            self.stack.setCurrentIndex(row)
            self._tab_changed(row)

    def current_tab(self) -> str:
        return TABS[self.tabs.currentRow()]

    def set_expert(self, on: bool) -> None:
        """Show or hide the expert rows on every page made so far (and on the ones made later)."""
        self._expert = bool(on)
        QSettings().setValue(EXPERT, self._expert)
        for holder in ("stream_quality", "stream_page", "recording_tabs", "connections_page"):
            page = self.__dict__.get(holder)
            if page is not None and hasattr(page, "set_expert"):
                page.set_expert(self._expert)
        if "Video" in self._built:
            self._video_expert()

    # Pages -------------------------------------------------------------------------

    def _general_page(self) -> QWidget:
        look = QGroupBox("Appearance")
        form = QFormLayout(look)
        self.theme = QComboBox()
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(chosen_theme())))
        form.addRow("Theme", self.theme)

        q = QSettings()
        startup = QGroupBox("Startup")
        start_box = QVBoxLayout(startup)
        self.start_with_windows = QCheckBox("Start SprigCast when Windows starts")
        self.start_with_windows.setChecked(autostart.enabled())
        self.start_with_windows.setEnabled(autostart.supported())
        self.start_with_windows.setToolTip("When Windows starts it, SprigCast always opens minimised to the taskbar")
        start_box.addWidget(self.start_with_windows)
        self.start_minimized = QCheckBox("Start minimised")
        self.start_minimized.setChecked(q.value(autostart.START_MINIMIZED, False, type=bool))
        start_box.addWidget(self.start_minimized)

        from sprigcast import __version__
        from sprigcast.app.updates import auto_check_enabled

        updates = QGroupBox("Updates")
        up_box = QVBoxLayout(updates)
        self.auto_updates = QCheckBox("Check for updates automatically")
        self.auto_updates.setChecked(auto_check_enabled())
        self.auto_updates.setToolTip("Updates never install while you're live or recording")
        up_box.addWidget(self.auto_updates)
        up_row = QHBoxLayout()
        up_row.setSpacing(12)
        self.check_updates_button = QPushButton("Check now")
        self.check_updates_button.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.check_updates_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_updates_button.setAutoDefault(False)
        self.check_updates_button.setEnabled(self.check_updates is not None)
        self.check_updates_button.clicked.connect(lambda: self.check_updates and self.check_updates())
        up_row.addWidget(self.check_updates_button)
        up_row.addWidget(_hint(f"Version {__version__}"), 1)
        up_box.addLayout(up_row)

        safety = QGroupBox("Confirmations")
        box = QVBoxLayout(safety)
        self.confirm_stream = QCheckBox("Ask before ending a stream")
        self.confirm_stream.setChecked(confirm_enabled(CONFIRM_STREAM))
        box.addWidget(self.confirm_stream)
        self.confirm_recording = QCheckBox("Ask before stopping a recording")
        self.confirm_recording.setChecked(confirm_enabled(CONFIRM_RECORDING))
        box.addWidget(self.confirm_recording)

        from sprigcast.app import logbook

        logs = QGroupBox("Logs")
        lform = QFormLayout(logs)
        self.keep_logs = QComboBox()
        for days in logbook.KEEP_CHOICES:
            self.keep_logs.addItem(f"{days} days" if days else "Forever", days)
        self.keep_logs.setCurrentIndex(max(0, self.keep_logs.findData(logbook.keep_days())))
        lform.addRow("Keep logs for", self.keep_logs)

        from sprigcast.app import features

        used = QGroupBox("Features")
        used_box = QVBoxLayout(used)
        off = features.off()
        self.feature_ticks: dict[str, QCheckBox] = {}
        for name, label in sorted(features.FEATURES.items(), key=lambda kv: kv[1]):
            tick = QCheckBox(label)
            tick.setChecked(name not in off)
            tick.setToolTip("Off: hidden from Live and nothing of it is loaded")
            used_box.addWidget(tick)
            self.feature_ticks[name] = tick

        return _page(look, self._this_pc_card(), startup, used, updates, safety, logs, self._clear_data_card())

    def _clear_data_card(self) -> QGroupBox:
        """Clear all my data: keys, sign-ins, settings and files go, then SprigCast closes (app/clear_data.py)."""
        card = QGroupBox("Your data")
        row = QHBoxLayout(card)
        row.addWidget(QLabel("Delete everything SprigCast keeps on this PC."), 1)
        self.clear_data_button = QPushButton("Clear all my data…")
        self.clear_data_button.setStyleSheet(theme.SMALL_PILL_STYLE + f"QPushButton {{ color: {theme.BAD}; }}")
        self.clear_data_button.clicked.connect(lambda: self.on_clear_data and self.on_clear_data(self))
        row.addWidget(self.clear_data_button)
        return card

    def _this_pc_card(self) -> QGroupBox:
        """This PC: the tier Optimize picked, and the settings that decide how much of the PC SprigCast uses."""
        from sprigcast.app import lowend

        card = QGroupBox("This PC")
        form = QFormLayout(card)
        form.setVerticalSpacing(12)
        tier_row = QHBoxLayout()
        tier_row.setSpacing(12)
        self.tier = QLabel("Checking…")
        self.tier.setObjectName("valueChip")
        tier_row.addWidget(self.tier)
        self.optimize_button = QPushButton("Optimize again")
        self.optimize_button.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.optimize_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.optimize_button.setAutoDefault(False)
        self.optimize_button.setToolTip("Check this PC and pick stream and recording settings that fit it")
        self.optimize_button.setEnabled(not (self.streaming or self.recording))
        self.optimize_button.clicked.connect(self._optimize)
        tier_row.addWidget(self.optimize_button)
        tier_row.addStretch(1)
        form.addRow("Settings for", tier_row)
        stored = lowend.stored_tier()
        if stored:
            self.tier.setText(lowend.TIERS[stored])
        else:  # never optimized: worked out from the PC on a worker (reading the registry can take a moment)
            from sprigcast.core import workers

            found: list[str] = []
            workers.run(lambda: found.append(lowend.tier(lowend.pc_facts())), name="pc-tier")

            def show() -> None:
                if found:
                    self.tier.setText(f"{lowend.TIERS[found[0]]} (not optimized yet)")
                    self._tier_poll.stop()

            self._tier_poll = QTimer(self, interval=100)
            self._tier_poll.timeout.connect(show)
            self._tier_poll.start()

        self.priority = QComboBox()
        for key, label in priority.LEVELS.items():
            self.priority.addItem(label, key)
        self.priority.setCurrentIndex(max(0, self.priority.findData(priority.chosen())))
        self.priority.setToolTip("Above normal keeps the stream smooth while a game uses the CPU")
        form.addRow("Process priority", self.priority)
        self.sleep_sources = QComboBox()
        for key, label in SLEEP_CHOICES.items():
            self.sleep_sources.addItem(label, key)
        self.sleep_sources.setCurrentIndex(max(0, self.sleep_sources.findData(sleep_policy())))
        self.sleep_sources.setToolTip("Paused sources use no memory or CPU and start again when their bloom is "
                                      "shown.\nA paused camera takes about a second to come back.")
        form.addRow("Sources not on screen", self.sleep_sources)
        self.preview_live = QComboBox()
        for key, label in PREVIEW_CHOICES.items():
            self.preview_live.addItem(label, key)
        self.preview_live.setCurrentIndex(max(0, self.preview_live.findData(preview_choice())))
        self.preview_live.setToolTip("Light or Off leaves more of the graphics card for the stream on a "
                                     "slower PC.\nPop-out projectors keep drawing.")
        form.addRow("Preview while live", self.preview_live)
        return card

    def _optimize(self) -> None:
        self.optimize_requested.emit()
        self.reject()

    def _stream_page(self, encoder: str) -> QWidget:
        self.stream_page = StreamPage(self.collection)
        gardens = getattr(self.parent(), "gardens", None)
        if gardens is not None:
            self.stream_page.other_paths_ids = gardens.taken_output_ids()
        places = QWidget()
        box = QVBoxLayout(places)
        box.setContentsMargins(0, 4, 0, 0)
        if self.streaming:
            self.stream_page.setEnabled(False)
            box.addWidget(_hint("You're live. End the stream to change these."))
        box.addWidget(self.stream_page, 1)
        self.stream_tabs = QTabWidget()
        self.stream_tabs.addTab(places, "Places")
        self.stream_tabs.addTab(QWidget(), "Quality")  # made a tick later (_slice), or now if it's the one asked

        def quality(now: bool = False):
            self.stream_quality = StreamQuality(self.collection, encoder, streaming=self.streaming)
            if hasattr(self.stream_quality, "set_expert"):
                self.stream_quality.set_expert(self._expert)
            if now:
                put_in()
            else:  # made, then put in its tab (laid out and styled) a tick later: both at once were 150 ms
                self._slice(put_in)

        def put_in():
            import shiboken6

            if not shiboken6.isValid(self.stream_tabs):
                return
            tabs = self.stream_tabs
            current = tabs.currentIndex()
            stand_in = tabs.widget(1)
            tabs.removeTab(1)
            tabs.insertTab(1, _scrolled(self.stream_quality), "Quality")
            tabs.setCurrentIndex(current)
            stand_in.deleteLater()
            self._tab_changed(self.stack.currentIndex())
        if self._subs.get("Stream") == "Quality":
            quality(now=True)
        else:
            self._slice(quality)
        return self.stream_tabs

    def _recording_page(self, encoder: str) -> QWidget:
        self.recording_page = RecordingPage(
            self.collection.recording, self.default_recordings_folder, encoder, self.collection.canvases
        )
        self.recording_tabs = RecordingTabs(self.collection, encoder, self.recording_page, recording=self.recording,
                                            parts_later=True)
        for _part in self.recording_tabs.later:  # one part a tick (_slice); Save makes any left first
            self._slice(self.recording_tabs.take_part)
        tabs = self.recording_tabs.tabs
        for i in range(tabs.count()):  # each of its tabs scrolls on its own
            name, widget = tabs.tabText(i), tabs.widget(i)
            tabs.removeTab(i)
            tabs.insertTab(i, _scrolled(widget), name)
        tabs.setCurrentIndex(0)
        return self.recording_tabs

    def _tab_changed(self, row: int) -> None:
        quality = self.__dict__.get("stream_quality")  # (made a tick after the page: then this runs again)
        if 0 <= row < len(TABS) and TABS[row] == "Stream" and quality is not None:
            # the banner follows the place list's unsaved choices
            quality.banner.setVisible(any(
                o.enabled and o.service == "twitch" and o.enhanced for o in self.stream_page.outputs
            ))

    def _audio_page(self, audio_devices) -> QWidget:
        from sprigcast.app.audio_page import AudioPage

        # The tab asked for (the Sound card's gear: Sounds) now, the others a tick apart (_slice)
        first = self._subs.get("Audio") or "Devices"
        self.audio_page = AudioPage(self.collection, audio_devices, recording=self.recording,
                                    streaming=self.streaming, first=first)
        for name in list(self.audio_page.later):
            self._slice(lambda name=name: self.audio_page.build_tab(name))
        return _page(self.audio_page)

    def _video_page(self) -> QWidget:
        """Canvases (one card each: on, base size, frame rate; expert: output size and filter) and the camera."""
        pending = pending_video()
        locked = self.streaming or self.recording
        cards: list[QWidget] = []
        self.video_fields: dict[str, tuple[QCheckBox, QSpinBox, QSpinBox, QComboBox]] = {}
        self.video_output: dict[str, tuple[QComboBox, QComboBox]] = {}
        self._video_forms: list[tuple[QFormLayout, QComboBox, QComboBox]] = []
        for canvas in self.collection.canvases:
            values = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps}
            values.update({k: v for k, v in pending.get(canvas.id, {}).items() if k in values})
            card = QGroupBox(f"{canvas.id.capitalize()} canvas")
            form = QFormLayout(card)
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(18)
            enabled = QCheckBox("On")
            enabled.setChecked(canvas.enabled)
            enabled.setToolTip("Off: this canvas stops rendering and leaves the window right away")
            form.addRow("Canvas", enabled)
            width, height = QSpinBox(), QSpinBox()
            for box, value in ((width, values["width"]), (height, values["height"])):
                box.setRange(16, 7680)
                box.setSingleStep(2)
                box.setValue(int(value))
                box.setFixedWidth(112)
                # NoButtons + the window stylesheet collapses the text to nothing, so hide the arrows instead.
                box.setStyleSheet("QSpinBox { padding: 7px 14px; }"
                                  " QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }")
                box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            size_row = QHBoxLayout()
            size_row.setSpacing(10)
            size_row.addWidget(width)
            times = QLabel("×")
            times.setStyleSheet(f"color: {theme.MUTED}")
            size_row.addWidget(times)
            size_row.addWidget(height)
            size_row.addStretch(1)
            form.addRow("Size", size_row)
            fps = QComboBox()
            for choice in FPS_CHOICES:
                fps.addItem(f"{choice} fps", choice)
            if fps.findData(int(values["fps"])) < 0:
                fps.addItem(f"{values['fps']} fps", int(values["fps"]))
            fps.setCurrentIndex(fps.findData(int(values["fps"])))
            form.addRow("Frame rate", fps)
            out = QComboBox()
            out.setMinimumWidth(240)
            out.setToolTip("What viewers and recordings get. Smaller is lighter on the encoder and upload.\n"
                           "Each place in Stream can still pick its own size.")
            form.addRow("Output (scaled) size", out)
            downscale = QComboBox()
            for key, label in DOWNSCALE_LABELS.items():
                downscale.addItem(label, key)
            downscale.setCurrentIndex(max(0, downscale.findData(canvas.downscale)))
            form.addRow("Downscale filter", downscale)

            def fill(_=None, out=out, width=width, height=height, current=canvas.output_short_side,
                     downscale=downscale):
                keep = out.currentData() if out.count() else current
                w, h = width.value(), height.value()
                short = min(w, h)
                out.blockSignals(True)
                out.clear()
                out.addItem(f"Same as base ({w}×{h})", None)
                for side in OUTPUT_SIDES[1:]:
                    if side < short:
                        k = side / short
                        out.addItem(f"{round(w * k / 2) * 2}×{round(h * k / 2) * 2}", side)
                out.setCurrentIndex(max(0, out.findData(keep)))
                out.blockSignals(False)
                downscale.setEnabled(out.currentData() is not None and not locked)
            width.valueChanged.connect(fill)
            height.valueChanged.connect(fill)
            out.currentIndexChanged.connect(fill)
            fill()
            self.video_fields[canvas.id] = (enabled, width, height, fps)
            self.video_output[canvas.id] = (out, downscale)
            self._video_forms.append((form, out, downscale))
            if locked:
                for widget in (enabled, width, height, fps, out, downscale):
                    widget.setEnabled(False)
            cards.append(card)
        widgets = list(cards)
        if locked:
            widgets.insert(0, _hint("Stop streaming and recording to change the canvases."))
        self._video_expert()
        self.video_tabs = QTabWidget()
        self.video_tabs.addTab(_scrolled(_page(*widgets)), "Canvases")
        self.video_tabs.addTab(QWidget(), "Virtual camera")  # made a tick later (_slice), or now if it's asked

        def camera():
            self.camera_tab = CameraTab(self.collection)
            tabs = self.video_tabs
            current = tabs.currentIndex()
            stand_in = tabs.widget(1)
            tabs.removeTab(1)
            tabs.insertTab(1, _scrolled(self.camera_tab), "Virtual camera")
            tabs.setCurrentIndex(current)
            stand_in.deleteLater()
        if self._subs.get("Video") == "Virtual camera":
            camera()
        else:
            self._slice(camera)
        return self.video_tabs

    def _video_expert(self) -> None:
        """Output size and filter: expert rows, unless one is already set (then it's shown, so it's never hidden
        while it changes what viewers get)."""
        for form, out, downscale in self.__dict__.get("_video_forms", []):
            show = self._expert or out.currentData() is not None
            form.setRowVisible(out, show)
            form.setRowVisible(downscale, show)

    def _connections_page(self, control) -> QWidget:
        from sprigcast.app.connections_page import ConnectionsPage

        self.connections_page = ConnectionsPage(control)
        return _page(self.connections_page)

    # Restart -----------------------------------------------------------------------

    def _apply_general(self) -> None:
        settings = QSettings()
        settings.setValue("general/theme", self.theme.currentData())
        settings.setValue(CONFIRM_STREAM, self.confirm_stream.isChecked())
        settings.setValue(CONFIRM_RECORDING, self.confirm_recording.isChecked())
        settings.setValue(autostart.START_MINIMIZED, self.start_minimized.isChecked())
        from sprigcast.app.updates import AUTO_CHECK

        settings.setValue(AUTO_CHECK, self.auto_updates.isChecked())
        if autostart.supported() and self.start_with_windows.isChecked() != autostart.enabled():
            try:
                autostart.set_enabled(self.start_with_windows.isChecked())
            except OSError as exc:
                QMessageBox.warning(self, "Start with Windows", f"Windows didn't allow it: {exc}")
        settings.setValue(SLEEP_SETTING, self.sleep_sources.currentData())
        settings.setValue(PREVIEW_LIVE, self.preview_live.currentData())
        from sprigcast.app import features

        features.set_off({name for name, tick in self.feature_ticks.items() if not tick.isChecked()}, settings)
        from sprigcast.app import logbook

        settings.setValue(logbook.KEEP_SETTING, self.keep_logs.currentData())
        level = self.priority.currentData()
        if level != priority.chosen():
            priority.remember(level, settings)
            priority.set_cpu_priority(level)

    def _apply_video(self) -> None:
        settings = QSettings()
        on = {cid for cid, fields in self.video_fields.items() if fields[0].isChecked()}
        if on:  # switching a canvas on or off happens right away; at least one stays on
            for canvas in self.collection.canvases:
                canvas.enabled = canvas.id in on
        changes = {}
        for canvas in self.collection.canvases:
            enabled, width, height, fps = self.video_fields[canvas.id]
            wanted = {
                "width": width.value(),
                "height": height.value(),
                "fps": fps.currentData(),
            }
            current = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps}
            if wanted != current:
                changes[canvas.id] = wanted
        for canvas in self.collection.canvases:
            out, downscale = self.video_output[canvas.id]
            canvas.output_short_side = out.currentData()
            canvas.downscale = downscale.currentData()
        if changes:
            settings.setValue(PENDING_VIDEO, json.dumps(changes))
        else:
            settings.remove(PENDING_VIDEO)

    def _show_restart_note(self) -> None:
        waiting = bool(pending_video())
        self.restart_note.setText("Canvas changes wait for a restart" if waiting else "")
        self.restart_note.setVisible(waiting)
        self.restart_button.setVisible(waiting and not (self.streaming or self.recording))

    def _restart(self) -> None:
        if self.apply():
            self.restart_requested.emit()
            self.accept()

    # Saving ------------------------------------------------------------------------

    def _ok(self) -> None:
        if self.apply():
            self.accept()

    def apply(self) -> bool:
        """Write back every page that was made (one never opened still matches what's saved). Returns False
        (and shows why) if something can't be saved. Making every page here held Save up for seconds while live."""
        self._flush_slices()  # the parts of made pages still waiting for their tick
        built = self._built
        if "Stream" in built:
            if not self.streaming and not self.stream_page.apply_to(self.collection):
                self.open_tab("Stream/Places")
                return False
            self.stream_quality.apply_to(self.collection)
        if "Recording" in built:
            if not self.recording:
                self.recording_page.apply_to(self.collection.recording, self.default_recordings_folder)
            self.recording_tabs.apply_to(self.collection)
        if "Video" in built:
            self.camera_tab.apply()
        if "Connections" in built:
            self.connections_page.apply()
        if "Audio" in built:
            self.audio_page.apply_to(self.collection, recording=self.recording)
        if "General" in built:
            self._apply_general()
        if "Video" in built and not (self.streaming or self.recording):
            self._apply_video()
        self._show_restart_note()
        self.applied.emit()
        return True
