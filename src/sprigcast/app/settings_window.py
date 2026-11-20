"""The Settings window: one place for every option, a page for each topic picked from the side.

Seven pages, each answering one question: General (how the app behaves, and This PC), Appearance (theme, text
size and colors), Stream (where you go live and how good), Audio, Video (canvases and the virtual camera), Recording (where recordings and clips
go, and how good) and Connections. Hotkeys and the plugin store have windows of their own (Tools menu).

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
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QGridLayout,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import autostart, priority, theme
from sprigcast.app import message as msgbox
from sprigcast.app.badged import BadgedGroup
from sprigcast.app.segmented import Segmented
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

TABS = ["General", "Appearance", "Stream", "Audio", "Video", "Recording", "Connections"]
# Older names, and the windows that left Settings, land somewhere sensible ("Tab/Sub": a page's own tab).
TAB_ALIASES = {"Output": "Stream/Quality", "Clips": "Recording/Clips", "Replay": "Recording/Clips",
               "Advanced": "General", "Camera": "Video/Virtual camera", "Hotkeys": "General", "Plugins": "General",
               "Accounts": "Stream/Places"}  # sign-in is on each place now (user, 2026-11-19)
ABOUT = {  # one line under each page's title
    "General": "How SprigCast behaves, and how much this PC carries.",
    "Appearance": "How SprigCast looks: theme, text size and colors.",
    "Stream": "Where you go live, and how good it looks.",
    "Recording": "Where recordings and clips go, and how good they look.",
    "Audio": "Devices, sounds and the tracks they go on.",
    "Video": "Your canvases and the virtual camera.",
    "Connections": "Let other apps on your PC control SprigCast.",
}
EXPERT = "settings/expert"  # QSettings: show the expert rows
PREVIEW_LIVE = "general/preview_live"  # QSettings: the previews while live or recording
PREVIEW_CHOICES = {"smooth": "Smooth", "light": "Light (15 fps)", "off": "Off"}
PREVIEW_FPS = {"smooth": 30, "light": 15, "off": 0}

# Settings search: what you might type, and where it lives ("Tab/Sub"). expert: shown only with Expert on.
SEARCH: list[tuple[str, str, bool]] = [
    ("Theme", "Appearance", False), ("Text size", "Appearance", False), ("Colors", "Appearance", False),
    ("Color-blind friendly", "Appearance", False), ("High contrast", "Appearance", False), ("Start with Windows", "General", False), ("Start minimized", "General", False),
    ("Updates", "General", False), ("Ask before ending the stream", "General", False),
    ("Ask before going live", "General", False), ("Keep recording when the stream ends", "General", False),
    ("Start Instant replay when I go live", "General", False), ("Snapping", "General", False),
    ("Snap distance", "General", False),
    ("Show what's outside the canvas", "General", False), ("Projectors", "General", False),
    ("Always on top", "General", False), ("Reopen projectors", "General", False), ("Hide the mouse pointer", "General", False),
    ("Hide from screen capture", "General", False), ("Privacy", "General", False),
    ("System tray", "General", False), ("Import from OBS", "General", False),
    ("Import from Streamlabs", "General", False), ("Import from Meld", "General", False), ("Minimize to the tray", "General", False),
    ("Close to the tray", "General", False),
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
    ("Sound devices", "Audio/Devices", False), ("Mic", "Audio/Devices", False), ("Desktop sound", "Audio/Devices", False),
    ("Tracks", "Audio/Tracks", False), ("Advanced audio", "Audio/Sounds", False), ("Ducking", "Audio/Sounds", False),
    ("Volume", "Audio/Sounds", False), ("Balance", "Audio/Sounds", False),
    ("Push to talk", "Audio/Devices", False), ("Push to mute", "Audio/Devices", False),
    ("Listen through", "Audio/Devices", False), ("Monitoring device", "Audio/Devices", False),
    ("Windows ducking", "Audio/Devices", False),
    ("Canvas size", "Video/Canvases", False), ("Canvas frame rate", "Video/Canvases", False),
    ("Vertical canvas", "Video/Canvases", False), ("Sent out at", "Video/Canvases", True),
    ("Output size", "Video/Canvases", True),
    ("Scaling", "Video/Canvases", True), ("Downscale filter", "Video/Canvases", True),
    ("Virtual camera", "Video/Virtual camera", False),
    ("Accounts", "Stream/Places", False), ("Sign in", "Stream/Places", False), ("Sign out", "Stream/Places", False),
    ("Twitch account", "Stream/Places", False), ("YouTube account", "Stream/Places", False),
    ("Kick account", "Stream/Places", False),
    ("Control API", "Connections", False), ("Password (API)", "Connections", False),
    ("Pair a device", "Connections", False), ("Streamer.bot", "Connections", False),
    ("OBS-style connection", "Connections", False), ("Port", "Connections", False),
    ("Phones on my network", "Connections", True), ("Web links", "Connections", True),
]


def window_style() -> str:
    """The Settings look: a pill menu on the left, titled section cards that stand off the window (a leaf
    before each title), fields that sink into the card with an edge that reads, bright labels."""
    t = theme
    try:
        leaf = t.section_title_rule(t.nature_pictures(), "#settingsWindow QGroupBox::title")
    except OSError:  # can't write the picture: titles without the leaf
        leaf = ""
    return f"""
#settingsWindow {{ background: {t.BG}; border: none; }}
#settingsNav {{ background: transparent; border: none; padding: 0px; outline: none; }}
#settingsNav::item {{ color: {t.MUTED}; padding: 10px 16px; margin: 2px 0px; border-radius: 18px; border: none; }}
#settingsNav::item:hover {{ background: {t.PANEL}; color: {t.TEXT}; }}
#settingsNav::item:selected {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; font-weight: 600; }}
#settingsWindow QGroupBox {{ background: {t.SECTION}; border: 1px solid {t.SECTION_EDGE}; border-radius: 20px;
    margin-top: 0px; padding: 50px 18px 16px 18px; font-weight: 400; }}
#settingsWindow QGroupBox::title {{ subcontrol-origin: padding; subcontrol-position: top left; left: 18px;
    top: 16px; padding: 0px; color: {t.TEXT_BRIGHT}; font-size: {t.SECTION_TITLE_PX}px; font-weight: 600; }}
{leaf}
#settingsWindow QGroupBox QLabel {{ color: {t.LABEL}; }}
#settingsWindow QGroupBox QCheckBox, #settingsWindow QGroupBox QRadioButton {{ color: {t.LABEL}; }}
#settingsWindow QLineEdit, #settingsWindow QSpinBox, #settingsWindow QDoubleSpinBox,
#settingsWindow QComboBox, #settingsWindow QPlainTextEdit {{ background: {t.FIELD}; border: 1px solid {t.FIELD_EDGE};
    border-radius: 10px; padding: 6px 13px; min-height: 20px; }}
#settingsWindow QSpinBox, #settingsWindow QDoubleSpinBox {{ padding-right: 24px; }}
#settingsWindow QLabel#valueChip {{ background: {t.FIELD}; border: 1px solid {t.FIELD_EDGE}; border-radius: 10px;
    padding: 6px 13px; }}
#settingsWindow QLineEdit:focus, #settingsWindow QSpinBox:focus, #settingsWindow QDoubleSpinBox:focus,
#settingsWindow QComboBox:focus {{ border-color: {t.ACCENT}; }}
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
CONFIRM_GO_LIVE = "general/confirm_go_live"  # off unless picked: Go live has always gone at once
KEEP_RECORDING = "general/keep_recording_after_stream"  # "Record when I go live" carries on past the stream
REPLAY_ON_LIVE = "general/replay_on_live"  # Instant replay (clips) starts with the stream
SNAP_DISTANCES = (5, 10, 20, 40)  # px on screen (the same as Build's right-click → Snapping → Sensitivity)
CONFIRM_RECORDING = "general/confirm_stop_recording"
PENDING_VIDEO = "video/pending"  # canvas changes saved here, applied at the next start


def appearance_waiting() -> bool:
    """A theme, text size or colors saved that this run isn't drawn with yet (they're read when SprigCast starts)."""
    size = theme.text_size_in_use
    picked = chosen_theme()
    return (theme.chosen_colors() != theme.colors_in_use or (size is not None and theme.chosen_text_size() != size)
            or (picked != "system" and picked != theme.theme_in_use))


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
    # Said each time: a queued 30 fps went missing on the PC (2026-11-19) and nothing told where.
    log.info("video: queued canvas changes applied at start: %s; canvases now %s", pending,
             ", ".join(f"{c.id} {c.width}×{c.height} {c.fps} fps" for c in collection.canvases))
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


LINED_UP = "settingsLinedUp"  # a form label already set level with its box
TALL_FIELD = 48  # a box taller than this (text areas, lists) keeps its label at the top


def line_up_labels(root: QWidget) -> None:
    """Every form label in `root` sits level with its box. Qt puts a label at the top of its row, so next to
    a padded box it rode high (Settings redesign, 2026-11-19). Cheap to run again: done labels are skipped."""
    import shiboken6

    if not shiboken6.isValid(root):
        return
    label_role, field_role = QFormLayout.ItemRole.LabelRole, QFormLayout.ItemRole.FieldRole
    for form in root.findChildren(QFormLayout):
        for row in range(form.rowCount()):
            label, field = form.itemAt(row, label_role), form.itemAt(row, field_role)
            widget = label.widget() if label is not None else None
            if not isinstance(widget, QLabel) or field is None or widget.property(LINED_UP):
                continue
            if field.sizeHint().height() > TALL_FIELD:
                continue
            widget.setProperty(LINED_UP, True)
            widget.setSizePolicy(widget.sizePolicy().horizontalPolicy(), QSizePolicy.Policy.Expanding)
            flags = widget.alignment() & ~Qt.AlignmentFlag.AlignVertical_Mask
            widget.setAlignment(flags | Qt.AlignmentFlag.AlignVCenter)


EVERYDAY_FPS = (30, 60)
# A canvas runs from 720p up to 1440p, picked from a list, and is 1080p unless you pick another (user, 2026-11-20).
# Lighter output sizes for viewers and recordings are picked under Output, below.
CANVAS_SIZES = ((1280, 720, "720p"), (1366, 768, ""), (1600, 900, ""), (1920, 1080, "1080p"),
                (2048, 1152, ""), (2560, 1440, "1440p"))
DEFAULT_CANVAS = "1080p"


def canvas_sizes(width: int, height: int) -> list[tuple[int, int, str]]:
    """The sizes offered for a canvas of this shape, 720p to 1440p, turned on their side for a tall canvas."""
    if height > width:
        return [(h, w, name) for w, h, name in CANVAS_SIZES]
    return list(CANVAS_SIZES)


class FpsPick(Segmented):
    """Frame rate as quick picks: 30 and 60, and the one in use if it's another (24, 25, 48, 50, 120: picked from
    "Other" beside it, with Expert settings on)."""

    def __init__(self, fps: int, parent: QWidget | None = None):
        choices = sorted({*FPS_CHOICES, int(fps)})
        super().__init__([(c, str(c)) for c in choices], int(fps), parent)
        self._made = True
        self.picked.connect(lambda _fps: self._label())
        self._label()

    def currentData(self) -> int:  # noqa: N802 (read like the drop-down this was)
        return self.value()

    def _label(self) -> None:
        current = self.value()
        for fps, button in self.buttons.items():
            button.setVisible(fps in EVERYDAY_FPS or fps == current)
            button.setText(f"{fps} fps" if fps == current else str(fps))

    def set_value(self, value) -> None:
        super().set_value(value)
        if hasattr(self, "_made"):
            self._label()


class CanvasShape(QWidget):
    """A canvas's shape in small: an outlined box, wide for horizontal and tall for vertical."""

    def __init__(self, width: int, height: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(68, 44)
        self.shape = (width, height)

    def set_size(self, width: int, height: int) -> None:
        if (width, height) != self.shape:
            self.shape = (width, height)
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QPainter, QPen

        w, h = self.shape
        k = min((self.width() - 2) / max(1, w), (self.height() - 2) / max(1, h))
        box = QRectF(0, 0, w * k, h * k)
        box.moveCenter(QRectF(self.rect()).center())
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(theme.ACCENT), 1.6))
        p.setBrush(QColor(theme.FIELD))
        p.drawRoundedRect(box, 3, 3)
        p.end()


SECTION_GAP = 14  # between a page's section cards


def _page(*widgets: QWidget, stretch: bool = True) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(SECTION_GAP)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return page


FILL_AFTER_MS = 30  # the window shows first, then its page is made (fill_after_show)
STAND_IN = "settingsStandIn"
PAGE_LATE_MS = 400  # a picked page still not in its place this long after: put it in now (_make_sure_shown)
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
            "Appearance": self._appearance_page,
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
        self.on_import = None  # the main window's: closes Settings, then offers what can be imported
        for name in TABS:
            QListWidgetItem(name, self.tabs)
            stand_in = QWidget()  # stands in until the page is made
            stand_in.setObjectName(STAND_IN)
            self.stack.addWidget(stand_in)
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
                "connections_page": "Connections", "theme": "Appearance", "text_size": "Appearance",
                "color_preset": "Appearance"}

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
            if name not in self.__dict__.setdefault("_asked_blind", set()):
                self._asked_blind.add(name)  # said once: a probe from outside made every page at once (2026-11-19)
                log.info("settings: '%s' asked for before its page was made; making pages to find it", name)
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

        def put_in() -> None:
            import shiboken6

            if not shiboken6.isValid(self.stack):
                return
            index = TABS.index(name)
            current = self.stack.currentIndex()
            placeholder = self.stack.widget(index)
            self.stack.insertWidget(index, self._titled(name, page))
            self.stack.removeWidget(placeholder)
            placeholder.deleteLater()
            self.stack.setCurrentIndex(current)
            self._show_sub(name)
            self._tab_changed(current)
            line_up_labels(page)

        if self.isVisible():
            # Made now, put in its place (titled, styled, laid out) first thing next tick: both at once were the
            # General page's 106-131 ms and the Sound gear's 62-122 ms on the PC while live (2026-11-18).
            self._slice_first(put_in)
        else:
            put_in()
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

    def _slice_first(self, call) -> None:
        """Like _slice, but ahead of every part already waiting."""
        if self._slices:
            self._slices.insert(0, call)
        else:
            self._slice(call)

    def _next_slice(self) -> None:
        if not self._slices:
            return
        if getattr(self, "_closed", False):
            self._slices.clear()
            return
        call = self._slices.pop(0)
        try:
            call()
        except Exception:  # noqa: BLE001  one part going wrong never stops the parts after it (pages stay blank)
            log.exception("settings: part of a page failed to make")
        line_up_labels(self.stack)
        if self._slices:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(SLICE_GAP_MS, self._next_slice)

    def _flush_slices(self) -> None:
        while self._slices:
            call = self._slices.pop(0)
            try:
                call()
            except Exception:  # noqa: BLE001
                log.exception("settings: part of a page failed to make")
        line_up_labels(self.stack)

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
            if self._fill_tab is not None:
                # Picked before the window's first page was filled in (a busy PC can be late with it): fill in this
                # one, not the page it opened on. Filled in later, it took the window back to General (2026-11-18).
                self._fill_tab = TABS[row]
            self.ensure(TABS[row])
            self.stack.setCurrentIndex(row)
            self._tab_changed(row)
            if self.isVisible() and self._stand_in(row):
                from PySide6.QtCore import QTimer

                QTimer.singleShot(PAGE_LATE_MS, self, lambda: self._make_sure_shown(row))

    def _stand_in(self, row: int) -> bool:
        widget = self.stack.widget(row)
        return widget is not None and widget.objectName() == STAND_IN

    def _make_sure_shown(self, row: int) -> None:
        """The picked page is made but not yet in its place: its parts wait on timers that a busy UI can hold
        back. Put everything waiting in now, so a picked page never stays blank or on the last one."""
        if getattr(self, "_closed", False) or self.tabs.currentRow() != row or not self._stand_in(row):
            return
        log.warning("settings: the %s page was still waiting after %d ms; putting it in now", TABS[row], PAGE_LATE_MS)
        self._flush_slices()
        if self.stack.currentIndex() != row:
            self.stack.setCurrentIndex(row)

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
        q = QSettings()
        startup = QGroupBox("Startup")
        start_box = QVBoxLayout(startup)
        self.start_with_windows = QCheckBox("Start SprigCast when Windows starts")
        self.start_with_windows.setChecked(autostart.enabled())
        self.start_with_windows.setEnabled(autostart.supported())
        self.start_with_windows.setToolTip("When Windows starts it, SprigCast always opens minimized to the taskbar")
        start_box.addWidget(self.start_with_windows)
        self.start_minimized = QCheckBox("Start minimized")
        self.start_minimized.setChecked(q.value(autostart.START_MINIMIZED, False, type=bool))
        start_box.addWidget(self.start_minimized)

        page = _page(self._this_pc_card(), startup)

        def rest() -> None:
            """Features, Updates, Confirmations, Logs and Your data: a tick after the page (Save, or asking for one
            of their boxes, makes them first)."""
            import shiboken6

            if not shiboken6.isValid(page):
                return
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

            safety = QGroupBox("Going live")
            box = QVBoxLayout(safety)
            self.confirm_go_live = QCheckBox("Ask before going live")
            self.confirm_go_live.setChecked(q.value(CONFIRM_GO_LIVE, False, type=bool))
            box.addWidget(self.confirm_go_live)
            self.confirm_stream = QCheckBox("Ask before ending the stream")
            self.confirm_stream.setChecked(confirm_enabled(CONFIRM_STREAM))
            box.addWidget(self.confirm_stream)
            self.confirm_recording = QCheckBox("Ask before stopping a recording")
            self.confirm_recording.setChecked(confirm_enabled(CONFIRM_RECORDING))
            box.addWidget(self.confirm_recording)
            self.keep_recording = QCheckBox("Keep recording when the stream ends")
            self.keep_recording.setToolTip("For \"Record when I go live\" (Recording): the recording carries on")
            self.keep_recording.setChecked(q.value(KEEP_RECORDING, False, type=bool))
            box.addWidget(self.keep_recording)
            self.replay_on_live = QCheckBox("Start Instant replay when I go live")
            self.replay_on_live.setChecked(q.value(REPLAY_ON_LIVE, False, type=bool))
            box.addWidget(self.replay_on_live)

            from sprigcast.app.preview import snap_settings

            snap_now = snap_settings()
            snapping = QGroupBox("Snapping")
            sform = QFormLayout(snapping)
            self.snap_on = QCheckBox("On")
            self.snap_on.setToolTip("Hold Ctrl while dragging to place a part without snapping")
            self.snap_on.setChecked(snap_now["enabled"])
            sform.addRow("Snap", self.snap_on)
            self.snap_distance = QComboBox()
            for px in SNAP_DISTANCES:
                self.snap_distance.addItem(f"{px} px", float(px))
            near = min(SNAP_DISTANCES, key=lambda px: abs(px - snap_now["distance"]))
            self.snap_distance.setCurrentIndex(SNAP_DISTANCES.index(near))
            sform.addRow("Distance", self.snap_distance)
            snap_row = QHBoxLayout()
            snap_row.setSpacing(18)
            self.snap_to: dict[str, QCheckBox] = {}
            for key, label in (("edges", "Edges"), ("sources", "Other parts"), ("center", "Center")):
                tick = QCheckBox(label)
                tick.setChecked(snap_now[key])
                snap_row.addWidget(tick)
                self.snap_to[key] = tick
            snap_row.addStretch(1)
            sform.addRow("Snap to", snap_row)

            def snap_follow(on: bool) -> None:
                self.snap_distance.setEnabled(on)
                for tick in self.snap_to.values():
                    tick.setEnabled(on)
            self.snap_on.toggled.connect(snap_follow)
            snap_follow(self.snap_on.isChecked())

            from sprigcast.app.preview import show_overflow_setting

            canvas = QGroupBox("Preview")
            cbox = QVBoxLayout(canvas)
            self.show_overflow = QCheckBox("Show what's outside the canvas")
            self.show_overflow.setToolTip("In Build, the parts of things hanging off the canvas are drawn hatched")
            self.show_overflow.setChecked(show_overflow_setting())
            cbox.addWidget(self.show_overflow)

            from sprigcast.app import projector, privacy

            popouts = QGroupBox("Projectors")
            pbox = QVBoxLayout(popouts)
            self.projector_hide_pointer = QCheckBox("Hide the mouse pointer")
            self.projector_hide_pointer.setToolTip("On full-screen projectors")
            self.projector_hide_pointer.setChecked(projector.hide_pointer())
            pbox.addWidget(self.projector_hide_pointer)
            self.projector_on_top = QCheckBox("Always on top")
            self.projector_on_top.setToolTip("Projector windows stay above other windows")
            self.projector_on_top.setChecked(projector.on_top())
            pbox.addWidget(self.projector_on_top)
            self.projector_reopen = QCheckBox("Reopen them next time")
            self.projector_reopen.setToolTip("The projectors open when you close SprigCast come back when it starts")
            self.projector_reopen.setChecked(projector.reopen())
            pbox.addWidget(self.projector_reopen)

            from sprigcast.app import tray

            in_tray = QGroupBox("System tray")
            tbox = QVBoxLayout(in_tray)
            self.tray_show = QCheckBox("Show in the tray")
            self.tray_show.setChecked(tray.wanted())
            tbox.addWidget(self.tray_show)
            self.tray_minimize = QCheckBox("Minimize to the tray")
            self.tray_minimize.setToolTip("With Start minimized, SprigCast starts there")
            self.tray_minimize.setChecked(q.value(tray.MINIMIZE_TO, False, type=bool))
            tbox.addWidget(self.tray_minimize)
            self.tray_close = QCheckBox("Close to the tray")
            self.tray_close.setToolTip("The window's × hides it and SprigCast keeps running. Quit is on the tray icon.")
            self.tray_close.setChecked(q.value(tray.CLOSE_TO, False, type=bool))
            tbox.addWidget(self.tray_close)

            kept = self._tray_kept = {}  # what the two were before the tray was unticked

            def tray_on(on: bool) -> None:
                # Without the tray icon there's nowhere to go: the two show as off, and come back as they
                # were when it's ticked again.
                for tick in (self.tray_minimize, self.tray_close):
                    if on:
                        tick.setChecked(kept.pop(tick, tick.isChecked()))
                    else:
                        kept[tick] = tick.isChecked()
                        tick.setChecked(False)
                    tick.setEnabled(on)

            self.tray_show.toggled.connect(tray_on)
            tray_on(self.tray_show.isChecked())

            private = QGroupBox("Privacy")
            prbox = QVBoxLayout(private)
            self.hide_from_capture = QCheckBox("Hide SprigCast from screen capture")
            self.hide_from_capture.setToolTip("Display captures and screen sharing leave out SprigCast's own windows."
                                              "\nProjectors still show.")
            self.hide_from_capture.setChecked(privacy.hidden())
            self.hide_from_capture.setEnabled(privacy.supported())
            prbox.addWidget(self.hide_from_capture)

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

            layout = page.layout()
            for card in (safety, snapping, canvas, popouts, in_tray, private, used, updates, logs, self._import_card(),
                         self._clear_data_card()):
                layout.insertWidget(layout.count() - 1, card)  # above the stretch

        if self.isVisible():
            self._slice(rest)
        else:
            rest()
        return page

    def _appearance_page(self) -> QWidget:
        """Appearance: the theme, text size and a color preset. Text size and colors show after a restart: every
        style in the app is built from them as SprigCast starts (theme.py)."""

        from sprigcast.app.look_preview import LookPreview, ThemeTile

        look = QGroupBox("Theme")
        box = QVBoxLayout(look)
        box.setSpacing(14)
        self.theme = QComboBox()  # what is picked; the tiles below are how you pick it
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(chosen_theme())))
        self.theme.hide()
        box.addWidget(self.theme)
        tiles = QGridLayout()
        tiles.setContentsMargins(0, 0, 0, 0)
        tiles.setSpacing(10)
        self.theme_tiles: dict[str, ThemeTile] = {}
        for i, (key, label) in enumerate(THEMES.items()):
            tile = ThemeTile(key, label, theme.chosen_colors())
            tile.setChecked(key == self.theme.currentData())
            tile.clicked.connect(lambda _=False, k=key: self.theme.setCurrentIndex(self.theme.findData(k)))
            tiles.addWidget(tile, i // 6, i % 6)
            self.theme_tiles[key] = tile
        for column in range(6):
            tiles.setColumnStretch(column, 1)
        box.addLayout(tiles)
        self.color_preview = LookPreview(self.theme.currentData(), theme.chosen_colors(), theme.chosen_text_size())
        box.addWidget(self.color_preview)

        def theme_picked() -> None:
            key = self.theme.currentData()
            for name, tile in self.theme_tiles.items():
                tile.setChecked(name == key)
            self.color_preview.show_theme(key)

        self.theme.currentIndexChanged.connect(theme_picked)

        text = QGroupBox("Text and colors")
        form = QFormLayout(text)
        form.setVerticalSpacing(12)
        self.text_size = Segmented([(key, label) for key, (label, _scale) in theme.TEXT_SIZES.items()],
                                   theme.chosen_text_size())
        self.text_size.picked.connect(self.color_preview.show_text_size)
        form.addRow("Text size", self.text_size)
        self.color_preset = Segmented(list(theme.COLOR_PRESETS.items()), theme.chosen_colors())
        self.color_preset.picked.connect(self.color_preview.show_preset)
        self.color_preset.picked.connect(lambda preset: [t.show_preset(preset) for t in self.theme_tiles.values()])
        form.addRow("Colors", self.color_preset)
        return _page(look, text)

    def _apply_appearance(self) -> None:
        settings = QSettings()
        settings.setValue(theme.THEME_KEY, self.theme.currentData())
        settings.setValue(theme.TEXT_SIZE_KEY, self.text_size.value() or "normal")
        settings.setValue(theme.COLORS_KEY, self.color_preset.value() or "default")

    def _import_card(self) -> QGroupBox:
        """Import: scenes from OBS, Streamlabs or Meld (the Gardens menu's Import, where people look for it)."""
        card = QGroupBox("Import")
        row = QHBoxLayout(card)
        row.addWidget(QLabel("Bring your scenes over from OBS, Streamlabs or Meld."), 1)
        self.import_button = QPushButton("Import…")
        self.import_button.setStyleSheet(theme.SMALL_PILL_STYLE)
        self.import_button.setToolTip("Settings closes first. Changes you haven't applied are dropped.")
        self.import_button.clicked.connect(lambda: self.on_import and self.on_import(self))
        row.addWidget(self.import_button)
        return card

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
                                     "slower PC.\nProjectors keep drawing.")
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
        self.stream_tabs.currentChanged.connect(self._stream_tab_changed)

        def quality(now: bool = False):
            # Owned by the page from the start: made a tick before it's put in its tab, it was left behind (with
            # its drop-downs' lists) when the window went in between (the warm-up one, on a busy PC).
            self.stream_quality = StreamQuality(self.collection, encoder, streaming=self.streaming,
                                                parent=self.stream_tabs)
            self.stream_quality.hide()
            if hasattr(self.stream_quality, "set_expert"):
                self.stream_quality.set_expert(self._expert)
            self.stream_quality.go_to_places = lambda: self.stream_tabs.setCurrentIndex(0)
            self.stream_quality.show_places(self.stream_page.outputs, self.collection.canvases)
            self._show_encoder_room()
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
            self.stream_quality.setHidden(False)  # (hidden while it waited to be put in)
            tabs.insertTab(1, _scrolled(self.stream_quality), "Quality")
            tabs.setCurrentIndex(current)
            stand_in.deleteLater()
            self._tab_changed(self.stack.currentIndex())
        if self._subs.get("Stream") == "Quality":
            quality(now=True)
        else:
            self._slice(quality)
        return self.stream_tabs

    def _stream_tab_changed(self, index: int) -> None:
        """Quality shows what each place gets from the place list as it is now, unsaved choices too."""
        quality = self.__dict__.get("stream_quality")
        if index == 1 and quality is not None:
            quality.show_places(self.stream_page.outputs, self.collection.canvases)
            self._show_encoder_room()

    def _show_encoder_room(self) -> None:
        """The encoder's load (Garden check's last reading) on Quality's Load row; hidden when there is none."""
        from sprigcast.app.destinations import encoder_room, room_line

        main = self.parent()
        load = (getattr(main, "last_resources", None) or {}).get("encoder")
        sessions = main._encode_sessions() if hasattr(main, "_encode_sessions") else 0
        line = room_line(load, sessions)
        form = self.stream_quality.stream_form
        if line is None or not load:
            form.set_room(None)
        else:
            form.set_room(load, (encoder_room(load, sessions) or 0) > 0, line[1])

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
            widget.setHidden(False)  # (taking a tab out hides its page: left hidden, the Recording page was blank)
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
        main = self.parent()
        levels = (lambda: main._last_levels) if hasattr(main, "_last_levels") else None  # the Devices meters
        # Made inside its page from the start: put in afterwards, Qt went through every field again to hand it the
        # page's look (51 ms of the Sound gear's first open on the PC, 2026-11-19).
        page = _page()
        self.audio_page = AudioPage(self.collection, audio_devices, recording=self.recording,
                                    streaming=self.streaming, first=first, levels=levels, parent=page)
        page.layout().insertWidget(0, self.audio_page)
        for name in list(self.audio_page.later):
            self._slice(lambda name=name: self.audio_page.build_tab(name, later=self._slice_first))
        return page

    def _video_page(self) -> QWidget:
        """Canvases (a card each, side by side: on, size, frame rate; "Sent out at" for experts) and the camera."""
        from sprigcast.app.live_stage import Switch

        pending = pending_video()
        locked = self.streaming or self.recording
        self.video_fields: dict[str, tuple[QCheckBox, QSpinBox, QSpinBox, FpsPick]] = {}
        self.video_output: dict[str, tuple[QComboBox, QComboBox]] = {}
        self.video_sizes: dict[str, QComboBox] = {}
        self.video_other_fps: dict[str, QComboBox] = {}
        self.video_heads: dict[str, tuple[QLabel, QLabel]] = {}
        side_by_side = QHBoxLayout()
        side_by_side.setContentsMargins(0, 0, 0, 0)
        side_by_side.setSpacing(SECTION_GAP)
        sent = self.sent_out = BadgedGroup("Sent out at", "Expert")
        sent_form = QFormLayout(sent)
        sent_form.setVerticalSpacing(12)
        sent_form.setHorizontalSpacing(18)
        scaling = self.scaling = QComboBox()
        for key, label in DOWNSCALE_LABELS.items():
            scaling.addItem(label, key)
        first = self.collection.canvases[0].downscale if self.collection.canvases else None
        scaling.setCurrentIndex(max(0, scaling.findData(first)))
        self._scaling_was = scaling.currentData()

        def scaling_needed(_=None) -> None:
            scaling.setEnabled(not locked and any(out.currentData() is not None for out, _s in self.video_output.values()))

        for canvas in self.collection.canvases:
            values = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps}
            values.update({k: v for k, v in pending.get(canvas.id, {}).items() if k in values})
            card = QGroupBox(f"{canvas.id.capitalize()} canvas")
            box = QVBoxLayout(card)
            box.setSpacing(14)
            head = QHBoxLayout()
            head.setSpacing(14)
            shape = CanvasShape(int(values["width"]), int(values["height"]))
            head.addWidget(shape)
            words = QVBoxLayout()
            words.setSpacing(2)
            big, small = QLabel(), QLabel()
            big.setObjectName("canvasSize")
            small.setObjectName("canvasNote")
            words.addWidget(big)
            words.addWidget(small)
            head.addLayout(words)
            head.addStretch(1)
            enabled = Switch()
            enabled.setChecked(canvas.enabled)
            enabled.setToolTip("Off: this canvas stops rendering and leaves the window right away")
            on_text = QLabel("On" if canvas.enabled else "Off")
            enabled.toggled.connect(lambda on, label=on_text: label.setText("On" if on else "Off"))
            head.addWidget(enabled)
            head.addWidget(on_text)
            box.addLayout(head)
            form = QFormLayout()
            form.setVerticalSpacing(12)
            form.setHorizontalSpacing(18)
            form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            size = QComboBox()
            for w, h, name in canvas_sizes(int(values["width"]), int(values["height"])):
                size.addItem(f"{w}×{h} ({name})" if name else f"{w}×{h}", f"{w}x{h}")
            form.addRow("Size", size)
            width, height = QSpinBox(), QSpinBox()
            for number, value in ((width, values["width"]), (height, values["height"])):
                number.setRange(16, 7680)
                number.setSingleStep(2)
                number.setValue(int(value))
                number.setFixedWidth(96)
                # NoButtons + the window stylesheet collapses the text to nothing, so hide the arrows instead.
                number.setStyleSheet("QSpinBox { padding: 7px 14px; }"
                                     " QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }")
                number.setAlignment(Qt.AlignmentFlag.AlignCenter)
            custom = QWidget()
            size_row = QHBoxLayout(custom)
            size_row.setContentsMargins(0, 0, 0, 0)
            size_row.setSpacing(10)
            size_row.addWidget(width)
            times = QLabel("×")
            times.setStyleSheet(f"color: {theme.MUTED}")
            size_row.addWidget(times)
            size_row.addWidget(height)
            size_row.addStretch(1)
            form.addRow("", custom)
            fps = FpsPick(int(values["fps"]))
            other = QComboBox()  # the rarer frame rates, for experts
            other.addItem("Other", None)
            for choice in FPS_CHOICES:
                if choice not in EVERYDAY_FPS:
                    other.addItem(f"{choice} fps", choice)
            other.setFixedWidth(104)

            def other_picked(_=None, other=other, fps=fps):
                if other.currentData() is not None:
                    fps.set_value(other.currentData())
                    fps.picked.emit(other.currentData())
                    other.setCurrentIndex(0)

            other.currentIndexChanged.connect(other_picked)
            self.video_other_fps[canvas.id] = other
            fps_row = QHBoxLayout()
            fps_row.setSpacing(10)
            fps_row.addWidget(fps)
            fps_row.addWidget(other)
            fps_row.addStretch(1)
            form.addRow("Frame rate", fps_row)
            box.addLayout(form)
            box.addStretch(1)
            out = QComboBox()
            out.setMinimumWidth(240)
            out.setToolTip("What viewers and recordings get. Smaller is lighter on the encoder and upload.\n"
                           "Each place in Stream can still pick its own size.")
            sent_form.addRow(canvas.id.capitalize(), out)

            def fill(_=None, out=out, width=width, height=height, current=canvas.output_short_side,
                     shape=shape, big=big, small=small, fps=fps):
                keep = out.currentData() if out.count() else current
                w, h = width.value(), height.value()
                short = min(w, h)
                out.blockSignals(True)
                out.clear()
                out.addItem(f"Same as the canvas ({w}×{h})", None)
                for side in OUTPUT_SIDES[1:]:
                    if side < short:
                        k = side / short
                        out.addItem(f"{round(w * k / 2) * 2}×{round(h * k / 2) * 2}", side)
                out.setCurrentIndex(max(0, out.findData(keep)))
                out.blockSignals(False)
                shape.set_size(w, h)
                big.setText(f"{w}×{h}")
                small.setText(f"{'Landscape' if w > h else 'Portrait' if h > w else 'Square'} · {fps.value()} fps")
                scaling_needed()

            def picked(_=None, size=size, width=width, height=height):
                wanted = size.currentData()
                if wanted is not None:
                    w, h = wanted.split("x")
                    width.setValue(int(w))
                    height.setValue(int(h))

            form.setRowVisible(custom, False)  # no sizes of your own: the boxes only carry the picked size
            at = size.findData(f"{int(values['width'])}x{int(values['height'])}")
            if at < 0:  # any other size (4K, an old custom one) comes back to 1080p when you save
                at = next(i for i in range(size.count()) if size.itemText(i).endswith(f"({DEFAULT_CANVAS})"))
            size.setCurrentIndex(at)
            size.currentIndexChanged.connect(picked)
            width.valueChanged.connect(fill)
            height.valueChanged.connect(fill)
            out.currentIndexChanged.connect(fill)
            fps.picked.connect(fill)
            self.video_fields[canvas.id] = (enabled, width, height, fps)
            self.video_output[canvas.id] = (out, scaling)
            self.video_sizes[canvas.id] = size
            self.video_heads[canvas.id] = (big, small)
            picked()
            fill()
            if locked:
                for widget in (enabled, size, width, height, fps, other, out):
                    widget.setEnabled(False)
            side_by_side.addWidget(card, 1)
        sent_form.addRow("Scaling", scaling)
        scaling_needed()
        canvases = QWidget()
        canvases.setLayout(side_by_side)
        canvases.setStyleSheet(
            f"QLabel#canvasSize {{ color: {theme.TEXT_BRIGHT}; font-size: 16px; font-weight: 700; }}"
            f"QLabel#canvasNote {{ color: {theme.MUTED}; }}")
        widgets: list[QWidget] = [canvases, sent]
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
        """Sent out at (output size and scaling): for experts, unless a size is already set (then it's shown, so
        it's never hidden while it changes what viewers get). Experts also get every frame rate."""
        outputs = self.__dict__.get("video_output", {})
        if outputs:
            self.sent_out.setVisible(self._expert or any(out.currentData() is not None for out, _s in outputs.values()))
        for other in self.__dict__.get("video_other_fps", {}).values():
            other.setVisible(self._expert)

    def _connections_page(self, control) -> QWidget:
        from sprigcast.app.connections_page import ConnectionsPage

        self.connections_page = ConnectionsPage(control)
        return _page(self.connections_page)

    # Restart -----------------------------------------------------------------------

    def _apply_general(self) -> None:
        settings = QSettings()
        settings.setValue(CONFIRM_STREAM, self.confirm_stream.isChecked())
        settings.setValue(CONFIRM_RECORDING, self.confirm_recording.isChecked())
        settings.setValue(CONFIRM_GO_LIVE, self.confirm_go_live.isChecked())
        settings.setValue(KEEP_RECORDING, self.keep_recording.isChecked())
        settings.setValue(REPLAY_ON_LIVE, self.replay_on_live.isChecked())
        settings.setValue("editing/snap", self.snap_on.isChecked())
        settings.setValue("editing/snap_distance", self.snap_distance.currentData())
        for key, tick in self.snap_to.items():
            settings.setValue(f"editing/snap_{key}", tick.isChecked())
        from sprigcast.app.preview import OVERFLOW_KEY

        settings.setValue(OVERFLOW_KEY, self.show_overflow.isChecked())
        from sprigcast.app import projector, privacy

        settings.setValue(projector.HIDE_POINTER, self.projector_hide_pointer.isChecked())
        settings.setValue(projector.ON_TOP, self.projector_on_top.isChecked())
        settings.setValue(projector.REOPEN, self.projector_reopen.isChecked())
        settings.setValue(privacy.HIDE_FROM_CAPTURE, self.hide_from_capture.isChecked())
        from sprigcast.app import tray

        settings.setValue(tray.SHOW, self.tray_show.isChecked())
        for key, tick in ((tray.MINIMIZE_TO, self.tray_minimize), (tray.CLOSE_TO, self.tray_close)):
            settings.setValue(key, self._tray_kept.get(tick, tick.isChecked()))  # kept for when the tray is back
        settings.setValue(autostart.START_MINIMIZED, self.start_minimized.isChecked())
        from sprigcast.app.updates import AUTO_CHECK

        settings.setValue(AUTO_CHECK, self.auto_updates.isChecked())
        if autostart.supported() and self.start_with_windows.isChecked() != autostart.enabled():
            try:
                autostart.set_enabled(self.start_with_windows.isChecked())
            except OSError as exc:
                msgbox.warning(self, "Start with Windows", f"Windows didn't allow it: {exc}")
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
            out, scaling = self.video_output[canvas.id]
            canvas.output_short_side = out.currentData()
            if scaling.currentData() != self._scaling_was:  # one Scaling for every canvas: only when it was changed
                canvas.downscale = scaling.currentData()
        self._scaling_was = self.scaling.currentData()
        before = pending_video()
        if changes:
            settings.setValue(PENDING_VIDEO, json.dumps(changes))
        else:
            settings.remove(PENDING_VIDEO)
        if changes != before:
            log.info("video: Settings changed what waits for a restart: %s (was %s)", changes or "nothing",
                     before or "nothing")

    def _show_restart_note(self) -> None:
        canvas = bool(pending_video())
        looks = appearance_waiting()
        waiting = canvas or looks
        text = "Canvas changes wait for a restart" if canvas else "Theme, text size and colors change after a restart"
        self.restart_note.setText(text if waiting else "")
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
        if "Appearance" in built:
            self._apply_appearance()
        if "Video" in built and not (self.streaming or self.recording):
            self._apply_video()
        self._show_restart_note()
        self.applied.emit()
        return True
