"""The Settings window: one place for every option, a page for each topic picked from the side.

Tabs: General, Stream, Output, Audio, Video, Hotkeys, Connections, Plugins. OK and Apply write the changes into the
scene collection and QSettings; the main window then refreshes itself (see MainWindow._after_settings).
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import autostart, priority, theme
from sprigcast.app.hotkeys_dialog import HotkeysPage
from sprigcast.app.live_stage import heading
from sprigcast.app.output_page import OutputPage
from sprigcast.app.recording_dialog import RecordingPage
from sprigcast.app.stream_dialog import StreamPage
from sprigcast.app.theme import THEMES, chosen_theme
from sprigcast.core.filters import filter_kinds
from sprigcast.core.scene import SceneCollection
from sprigcast.core.source import known_kinds, source_class

TABS = ["General", "Stream", "Output", "Audio", "Video", "Hotkeys", "Connections", "Plugins"]
TAB_ALIASES = {"Recording": "Output", "Clips": "Output"}  # older menu entries and Output's own tabs
ABOUT = {  # one line under each page's title
    "General": "Look, startup, snapping and the questions SprigCast asks first.",
    "Stream": "The places you go live, and what each one gets.",
    "Output": "Encoder quality for streams, and how recordings and clips are saved.",
    "Audio": "Devices, sound tracks and which sounds go on each.",
    "Video": "Your canvases: their size, frame rate and which ones are on.",
    "Hotkeys": "Keys that work even while you're in a game.",
    "Connections": "Let other apps on your PC control SprigCast.",
    "Plugins": "Extra sources and filters from the plugins folder.",
}


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
#settingsWindow QTabBar::tab:selected {{ background: {t.SELECT_BG}; color: {t.SELECT_TEXT}; font-weight: 600; }}
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
AUDIO_KINDS = ("audio_input", "audio_output")

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
    "not_cameras": "Pause, but keep cameras ready",
    "all": "Pause everything (lowest memory)",
    "off": "Keep running",
}


def sleep_policy() -> str:
    """What to do with sources no scene on screen shows (Engine.sleep_policy)."""
    value = QSettings().value(SLEEP_SETTING, "not_cameras")
    return value if value in SLEEP_CHOICES else "not_cameras"


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


def plugin_source_kinds() -> list[str]:
    """Labels of source kinds that come from plugins rather than SprigCast itself."""
    out = []
    for kind in known_kinds():
        cls = source_class(kind)
        if cls is not None and not cls.__module__.startswith("sprigcast."):
            out.append(getattr(cls, "label", kind))
    return sorted(out)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    return label


def _page(*widgets: QWidget, stretch: bool = True) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    for widget in widgets:
        layout.addWidget(widget)
    if stretch:
        layout.addStretch(1)
    return page


class SettingsWindow(QDialog):
    applied = Signal()

    def __init__(
        self,
        collection: SceneCollection,
        *,
        default_recordings_folder: str,
        encoder: str,
        audio_devices: dict[str, list[str]],
        plugin_report=None,
        open_plugins_folder=None,
        streaming: bool = False,
        recording: bool = False,
        control=None,
        tab: str = "General",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setObjectName("settingsWindow")
        self.setStyleSheet(window_style())
        self.resize(1060, 700)
        self.collection = collection
        self.default_recordings_folder = default_recordings_folder
        self.streaming, self.recording = streaming, recording

        self.tabs = QListWidget()  # the side menu (a list, so keys and screen readers work)
        self.tabs.setObjectName("settingsNav")
        self.tabs.setFixedWidth(156)
        self.tabs.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabs.setCursor(Qt.CursorShape.PointingHandCursor)
        font = self.tabs.font()
        font.setPointSizeF(font.pointSizeF() * 1.1)
        self.tabs.setFont(font)
        self.stack = QStackedWidget()
        for name, page in (
            ("General", self._general_page()),
            ("Stream", self._stream_page()),
            ("Output", self._output_page(encoder)),
            ("Audio", self._audio_page(audio_devices)),
            ("Video", self._video_page()),
            ("Hotkeys", self._hotkeys_page()),
            ("Connections", self._connections_page(control)),
            ("Plugins", self._plugins_page(plugin_report, open_plugins_folder)),
        ):
            QListWidgetItem(name, self.tabs)
            if name != "Stream":  # Stream scrolls its own form; the place list and + Add stay put
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setFrameShape(QScrollArea.Shape.NoFrame)
                scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                inner = QWidget()  # room on the right so the scroll bar never sits on a card
                pad = QVBoxLayout(inner)
                pad.setContentsMargins(0, 0, 14, 0)
                pad.addWidget(page)
                scroll.setWidget(inner)
                page = scroll
            self.stack.addWidget(self._titled(name, page))
        self.tabs.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.tabs.currentRowChanged.connect(self._tab_changed)
        self.output_page.show_tab(tab)
        tab = TAB_ALIASES.get(tab, tab)
        self.tabs.setCurrentRow(TABS.index(tab) if tab in TABS else 0)

        side = QVBoxLayout()
        side.setSpacing(14)
        side.addWidget(heading("Settings", 18))
        side.addWidget(self.tabs, 1)
        body = QHBoxLayout()
        body.setSpacing(28)
        body.addLayout(side)
        body.addWidget(self.stack, 1)
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
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

    def current_tab(self) -> str:
        return TABS[self.tabs.currentRow()]

    # Pages -------------------------------------------------------------------------

    def _general_page(self) -> QWidget:
        look = QGroupBox("Appearance")
        form = QFormLayout(look)
        self.theme = QComboBox()
        for key, label in THEMES.items():
            self.theme.addItem(label, key)
        self.theme.setCurrentIndex(max(0, self.theme.findData(chosen_theme())))
        form.addRow("Theme", self.theme)

        snapping = QGroupBox("Source alignment snapping")
        snap_form = QFormLayout(snapping)
        q = QSettings()
        self.snap_enabled = QCheckBox("Enable")
        self.snap_enabled.setChecked(q.value("editing/snap", True, type=bool))
        snap_form.addRow("", self.snap_enabled)
        self.snap_distance = QDoubleSpinBox()
        self.snap_distance.setRange(1.0, 50.0)
        self.snap_distance.setSingleStep(1.0)
        self.snap_distance.setDecimals(1)
        self.snap_distance.setSuffix(" px")
        self.snap_distance.setValue(q.value("editing/snap_distance", 10.0, type=float))
        snap_form.addRow("Snap sensitivity", self.snap_distance)
        self.snap_edges = QCheckBox("Snap sources to the edge of the canvas")
        self.snap_sources = QCheckBox("Snap sources to other sources")
        self.snap_center = QCheckBox("Snap sources to the horizontal and vertical center")
        for box, key in ((self.snap_edges, "edges"), (self.snap_sources, "sources"), (self.snap_center, "center")):
            box.setChecked(q.value(f"editing/snap_{key}", True, type=bool))
            snap_form.addRow("", box)
        self.snap_enabled.toggled.connect(
            lambda on: [w.setEnabled(on) for w in (self.snap_distance, self.snap_edges, self.snap_sources, self.snap_center)]
        )
        self.snap_enabled.toggled.emit(self.snap_enabled.isChecked())
        snap_form.addRow("", _hint("Hold Ctrl while dragging to place a source without snapping."))

        safety = QGroupBox("Confirmations")
        box = QVBoxLayout(safety)
        self.confirm_stream = QCheckBox("Ask before ending a stream")
        self.confirm_stream.setChecked(confirm_enabled(CONFIRM_STREAM))
        box.addWidget(self.confirm_stream)
        self.confirm_recording = QCheckBox("Ask before stopping a recording")
        self.confirm_recording.setChecked(confirm_enabled(CONFIRM_RECORDING))
        box.addWidget(self.confirm_recording)

        performance = QGroupBox("Running next to games and other apps")
        perf_form = QFormLayout(performance)
        self.priority = QComboBox()
        for key, label in priority.LEVELS.items():
            self.priority.addItem(label, key)
        self.priority.setCurrentIndex(max(0, self.priority.findData(priority.chosen())))
        perf_form.addRow("Process priority", self.priority)
        self.sleep_sources = QComboBox()
        for key, label in SLEEP_CHOICES.items():
            self.sleep_sources.addItem(label, key)
        self.sleep_sources.setCurrentIndex(max(0, self.sleep_sources.findData(sleep_policy())))
        perf_form.addRow("Sources not on screen", self.sleep_sources)
        perf_form.addRow("", _hint(
            "Paused sources use no memory or CPU and start again when their scene is shown. "
            "A paused camera takes about a second to come back."))
        perf_form.addRow("", _hint(
            "Above normal keeps the stream smooth while a game uses the CPU. SprigCast also asks Windows to put "
            "its GPU work first; that works best when SprigCast runs as administrator."))

        startup = QGroupBox("Startup")
        start_box = QVBoxLayout(startup)
        self.start_with_windows = QCheckBox("Start SprigCast when Windows starts")
        self.start_with_windows.setChecked(autostart.enabled())
        self.start_with_windows.setEnabled(autostart.supported())
        start_box.addWidget(self.start_with_windows)
        self.start_minimized = QCheckBox("Start minimised")
        self.start_minimized.setChecked(q.value(autostart.START_MINIMIZED, False, type=bool))
        start_box.addWidget(self.start_minimized)
        start_box.addWidget(_hint("When Windows starts it, SprigCast always opens minimised to the taskbar."))

        return _page(look, startup, snapping, safety, performance)

    def _plugins_page(self, plugin_report, open_plugins_folder) -> QWidget:
        files = QGroupBox("Installed plugins")
        box = QVBoxLayout(files)
        self.plugin_list = QListWidget()
        self.plugin_list.setMinimumHeight(120)
        if plugin_report is not None:
            for name in plugin_report.loaded:
                QListWidgetItem(f"✓  {name}", self.plugin_list)
            for name, error in plugin_report.failed.items():
                item = QListWidgetItem(f"✗  {name}: {error}", self.plugin_list)
                item.setForeground(QColor(theme.BAD))
        if not self.plugin_list.count():
            QListWidgetItem("No plugins loaded.", self.plugin_list)
        box.addWidget(self.plugin_list)
        box.addWidget(_hint(
            "Drop a .py file in the plugins folder and restart. Only install plugins you trust."
        ))
        if open_plugins_folder is not None:
            open_button = QPushButton("Open plugins folder")
            open_button.clicked.connect(open_plugins_folder)
            row = QHBoxLayout()
            row.addWidget(open_button)
            row.addStretch(1)
            box.addLayout(row)

        added = QGroupBox("What plugins added")
        form = QFormLayout(added)
        sources = plugin_source_kinds()
        filters = [k.label for k in filter_kinds() if not k.builtin]
        self.plugin_sources = QLabel(", ".join(sources) or "None")
        self.plugin_filters = QLabel(", ".join(filters) or "None")
        for label in (self.plugin_sources, self.plugin_filters):
            label.setWordWrap(True)
        form.addRow("Sources", self.plugin_sources)
        form.addRow("Audio filters", self.plugin_filters)
        form.addRow("", _hint("Plugin sources are under Sources → + → Plugins; plugin filters in a source's Filters window."))
        return _page(files, added)

    def _stream_page(self) -> QWidget:
        self.stream_page = StreamPage(self.collection)
        widgets: list[QWidget] = [self.stream_page]
        if self.streaming:
            self.stream_page.setEnabled(False)
            widgets.insert(0, _hint("You're live. End the stream to change these."))
        return _page(*widgets, stretch=False)

    def _output_page(self, encoder: str) -> QWidget:
        self.recording_page = RecordingPage(
            self.collection.recording, self.default_recordings_folder, encoder, self.collection.canvases
        )
        self.output_page = OutputPage(self.collection, encoder, self.recording_page,
                                      streaming=self.streaming, recording=self.recording)
        return _page(self.output_page)

    def _tab_changed(self, row: int) -> None:
        if TABS[row] == "Output":  # the banner follows the Stream tab's unsaved choices too
            self.output_page.banner.setVisible(any(
                o.enabled and o.service == "twitch" and o.enhanced for o in self.stream_page.outputs
            ))

    def _audio_page(self, audio_devices: dict[str, list[str]]) -> QWidget:
        from sprigcast.app.audio_page import AudioPage

        self.audio_page = AudioPage(self.collection, audio_devices, recording=self.recording)
        return _page(self.audio_page)

    def _video_page(self) -> QWidget:
        """One card per canvas: base size (what you build on), output size (what viewers get), filter, fps."""
        pending = pending_video()
        locked = self.streaming or self.recording
        cards: list[QWidget] = []
        self.video_fields: dict[str, tuple[QCheckBox, QSpinBox, QSpinBox, QComboBox]] = {}
        self.video_output: dict[str, tuple[QComboBox, QComboBox]] = {}
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
            form.addRow("Base (canvas) size", size_row)
            form.addRow("", _hint("What you see and build on."))
            out = QComboBox()
            out.setMinimumWidth(240)
            form.addRow("Output (scaled) size", out)
            out_note = _hint("")
            form.addRow("", out_note)
            downscale = QComboBox()
            for key, label in DOWNSCALE_LABELS.items():
                downscale.addItem(label, key)
            downscale.setCurrentIndex(max(0, downscale.findData(canvas.downscale)))
            form.addRow("Downscale filter", downscale)
            fps = QComboBox()
            for choice in FPS_CHOICES:
                fps.addItem(f"{choice} fps", choice)
            if fps.findData(int(values["fps"])) < 0:
                fps.addItem(f"{values['fps']} fps", int(values["fps"]))
            fps.setCurrentIndex(fps.findData(int(values["fps"])))
            form.addRow("Frame rate", fps)

            def fill(_=None, out=out, width=width, height=height, note=out_note, current=canvas.output_short_side,
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
                scaled = out.currentData() is not None
                downscale.setEnabled(scaled and not locked)
                note.setText("What viewers and recordings get. Each place in Settings → Stream can still pick "
                             "its own size." if not scaled else
                             "Viewers and recordings get this smaller size: lighter on the encoder and upload.")
            width.valueChanged.connect(fill)
            height.valueChanged.connect(fill)
            out.currentIndexChanged.connect(fill)
            fill()
            self.video_fields[canvas.id] = (enabled, width, height, fps)
            self.video_output[canvas.id] = (out, downscale)
            if locked:
                for widget in (enabled, width, height, fps, out, downscale):
                    widget.setEnabled(False)
            cards.append(card)
        note = "Base size and frame rate change the next time SprigCast starts. Output size and filter apply to the next stream or recording."
        if pending:
            note += " Changes are waiting for a restart."
        if locked:
            note = "Stop streaming and recording to change the canvases. " + note
        return _page(*cards, _hint(note))

    def _connections_page(self, control) -> QWidget:
        from sprigcast.app.connections_page import ConnectionsPage

        self.connections_page = ConnectionsPage(control)
        return _page(self.connections_page)

    def _hotkeys_page(self) -> QWidget:
        self.hotkeys_page = HotkeysPage(self.collection)
        return _page(self.hotkeys_page, stretch=False)

    # Saving ------------------------------------------------------------------------

    def _ok(self) -> None:
        if self.apply():
            self.accept()

    def apply(self) -> bool:
        """Write every tab back. Returns False (and shows why) if something can't be saved."""
        problem = self.hotkeys_page.problem()
        if problem:
            self.tabs.setCurrentRow(TABS.index("Hotkeys"))
            QMessageBox.warning(self, "Hotkeys", problem)
            return False
        if not self.streaming and not self.stream_page.apply_to(self.collection):
            self.tabs.setCurrentRow(TABS.index("Stream"))
            return False
        if not self.recording:
            self.recording_page.apply_to(self.collection.recording, self.default_recordings_folder)
        self.output_page.apply_to(self.collection)
        self.collection.hotkeys = self.hotkeys_page.result_hotkeys()
        self.connections_page.apply()
        self.audio_page.apply_to(self.collection, recording=self.recording)

        settings = QSettings()
        settings.setValue("general/theme", self.theme.currentData())
        settings.setValue(CONFIRM_STREAM, self.confirm_stream.isChecked())
        settings.setValue("editing/snap", self.snap_enabled.isChecked())
        settings.setValue("editing/snap_distance", self.snap_distance.value())
        settings.setValue("editing/snap_edges", self.snap_edges.isChecked())
        settings.setValue("editing/snap_sources", self.snap_sources.isChecked())
        settings.setValue("editing/snap_center", self.snap_center.isChecked())
        settings.setValue(CONFIRM_RECORDING, self.confirm_recording.isChecked())
        settings.setValue(autostart.START_MINIMIZED, self.start_minimized.isChecked())
        if autostart.supported() and self.start_with_windows.isChecked() != autostart.enabled():
            try:
                autostart.set_enabled(self.start_with_windows.isChecked())
            except OSError as exc:
                QMessageBox.warning(self, "Start with Windows", f"Windows didn't allow it: {exc}")
        settings.setValue(SLEEP_SETTING, self.sleep_sources.currentData())
        level = self.priority.currentData()
        if level != priority.chosen():
            settings.setValue(priority.SETTING, level)
            priority.set_cpu_priority(level)
        if not (self.streaming or self.recording):
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
        self.applied.emit()
        return True
