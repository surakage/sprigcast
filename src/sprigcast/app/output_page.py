"""Encoding in Settings: the stream's quality (Stream → Quality), recordings and clips (Recording), and the
virtual camera (Video → Virtual camera).

Each encoder card leads with one Quality choice, Lightest to Best. The rows behind it (rate control,
presets, B-frames...) show only with Expert settings on.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QPushButton,
    QRadioButton,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.segmented import Segmented
from sprigcast.core.scene import EncoderSettings, SceneCollection

RATE_CONTROLS = {"cbr": "Constant Bitrate (CBR)", "vbr": "Variable Bitrate (VBR)", "cqp": "Constant Quality (CQP)"}
PRESETS = {
    "p1": "P1: Fastest (Lowest Quality)",
    "p2": "P2: Faster (Lower Quality)",
    "p3": "P3: Fast (Low Quality)",
    "p4": "P4: Medium (Medium Quality)",
    "p5": "P5: Slow (Good Quality)",
    "p6": "P6: Slower (Better Quality)",
    "p7": "P7: Slowest (Best Quality)",
}
TUNINGS = {"hq": "High Quality", "ll": "Low Latency", "ull": "Ultra Low Latency"}
MULTIPASS = {"disabled": "Single Pass", "qres": "Two Passes (Quarter Resolution)", "fullres": "Two Passes (Full Resolution)"}
PROFILES = {"high": "High", "main": "Main", "baseline": "Baseline"}
AUDIO_KBPS = [96, 128, 160, 192, 256, 320]


def _combo(choices: dict[str, str], value: str) -> QComboBox:
    combo = QComboBox()
    for key, label in choices.items():
        combo.addItem(label, key)
    combo.setCurrentIndex(max(0, combo.findData(value)))
    return combo


CLIP_LENGTHS = (15, 30, 45, 60)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(f"color: {theme.MUTED}")
    return label


X264_PRESETS = {
    "ultrafast": "ultrafast (lightest on the CPU)", "superfast": "superfast", "veryfast": "veryfast (recommended)",
    "faster": "faster", "fast": "fast", "medium": "medium", "slow": "slow (heaviest, best quality)",
}
X264_TUNES = {"": "None", "film": "Film", "animation": "Animation", "grain": "Grain",
              "stillimage": "Still image", "zerolatency": "Zero latency"}


# Quality, lightest first: what each step sets on graphics-card encoders and on x264 (the CPU).
# "Balanced" is EncoderSettings' own defaults; "Lighter" is what Garden check's Make it lighter gives.
QUALITY_STEPS = (
    ("lightest", "Lightest", {"preset": "p1", "multipass": "disabled", "lookahead": False, "b_frames": 0},
     {"x264_preset": "ultrafast"}),
    ("lighter", "Lighter", {"preset": "p3", "multipass": "disabled", "lookahead": False, "b_frames": 0},
     {"x264_preset": "superfast"}),
    ("balanced", "Balanced", {"preset": "p5", "multipass": "disabled", "lookahead": False, "b_frames": 0},
     {"x264_preset": "veryfast"}),
    ("better", "Better looking", {"preset": "p6", "multipass": "qres", "lookahead": False, "b_frames": 0},
     {"x264_preset": "faster"}),
    ("best", "Best", {"preset": "p7", "multipass": "qres", "lookahead": False, "b_frames": 2},
     {"x264_preset": "fast"}),
)


# The three quick picks on the Quality row; the other steps (and Custom) are under Fine tuning (expert).
QUICK_QUALITY = (("lighter", "Faster"), ("balanced", "Balanced"), ("better", "Sharper"))


def encoder_chip(codec: str) -> str:
    """Where the encoder runs, in a few words, for the chip beside it."""
    from sprigcast.output.recorder import family

    return "CPU · uses your processor" if family(codec) == "x264" else "GPU · light on your game"


def quality_of(enc: EncoderSettings, codec: str = "") -> str:
    """The Quality step these settings are, or "custom" when expert rows were set some other way."""
    from sprigcast.output.recorder import family

    cpu = family(codec) == "x264" if codec else False
    for key, _label, gpu, x264 in QUALITY_STEPS:
        wanted = x264 if cpu else gpu
        if all(getattr(enc, k) == v for k, v in wanted.items()):
            return key
    return "custom"


def quality_changes(key: str) -> dict:
    """Everything one Quality step sets (both the graphics-card and the CPU rows)."""
    for k, _label, gpu, x264 in QUALITY_STEPS:
        if k == key:
            return {**gpu, **x264}
    return {}


class EncoderForm(QGroupBox):
    """The encoder settings card: the encoder and one Quality choice; with expert on, every row the chosen
    encoder uses.

    values() returns a new EncoderSettings.
    """

    def __init__(self, title: str, settings: EncoderSettings, encoder_name: str = "", streaming: bool = False,
                 parent: QWidget | None = None):
        super().__init__(title, parent)
        from sprigcast.output.recorder import codec_label, encoder_choices, pick_codec

        form = QFormLayout(self)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(10)
        self._form = form
        self.encoder = QComboBox()
        best = pick_codec()
        self.encoder.addItem(f"Best on this PC ({codec_label(best)})", "auto")
        for codec in encoder_choices(streaming):
            self.encoder.addItem(codec_label(codec), codec)
        if self.encoder.findData(settings.encoder) < 0:
            self.encoder.addItem(f"{codec_label(settings.encoder)} (not on this PC: uses the best one)", settings.encoder)
        self.encoder.setCurrentIndex(self.encoder.findData(settings.encoder))
        self._best = best
        encoder_row = QHBoxLayout()
        encoder_row.setSpacing(10)
        encoder_row.addWidget(self.encoder, 1)
        self.chip = QLabel()
        self.chip.setObjectName("encoderChip")
        self.chip.setStyleSheet(f"QLabel#encoderChip {{ background: {theme.SELECT_BG}; color: {theme.SELECT_TEXT};"
                                " border: none; border-radius: 11px; padding: 4px 12px; font-weight: 600; }")
        encoder_row.addWidget(self.chip)
        form.addRow("Video encoder", encoder_row)
        self.quick = Segmented(list(QUICK_QUALITY))
        self.quick.picked.connect(self._quick_picked)
        form.addRow("Quality", self.quick)
        self.load_row = QWidget()
        load = QVBoxLayout(self.load_row)
        load.setContentsMargins(0, 0, 0, 0)
        load.setSpacing(4)
        from PySide6.QtWidgets import QProgressBar

        self.load_bar = QProgressBar()
        self.load_bar.setRange(0, 100)
        self.load_bar.setTextVisible(False)
        self.load_bar.setFixedHeight(8)
        self.load_text = _hint("")
        load.addWidget(self.load_bar)
        load.addWidget(self.load_text)
        form.addRow("Load", self.load_row)
        self._room: tuple[float, bool, str] | None = None
        self.quality = QComboBox()
        for key, label, _gpu, _x264 in QUALITY_STEPS:
            self.quality.addItem(label, key)
        self.quality.addItem("Custom", "custom")
        self.quality.setToolTip("Lighter leaves more of your PC for games. Better looking costs more of the "
                                "graphics card (or the CPU with x264).")
        self._expert = False
        self.rate_control = _combo(RATE_CONTROLS, settings.rate_control)
        self.cq_level = QSpinBox()
        self.cq_level.setRange(1, 51)
        self.cq_level.setValue(settings.cq_level)
        self.cq_level.setToolTip("Lower is better quality and bigger files. 18 to 23 suits most recordings.")
        form.addRow("Quality level", self.cq_level)
        self.keyint = QSpinBox()
        self.keyint.setRange(0, 10)
        self.keyint.setPrefix("Every ")
        self.keyint.setSuffix(" s")
        self.keyint.setSpecialValueText("Auto (2 s)")
        self.keyint.setValue(settings.keyint_sec)
        self.preset = _combo(PRESETS, settings.preset)
        self.x264_preset = _combo(X264_PRESETS, settings.x264_preset)
        self.x264_preset.setToolTip("How hard the CPU works on each frame. Slower presets look better but "
                                    "take CPU time away from your game.")
        self.tuning = _combo(TUNINGS, settings.tuning)
        self.x264_tune = _combo(X264_TUNES, settings.x264_tune)
        self.multipass = _combo(MULTIPASS, settings.multipass)
        self.profile = _combo(PROFILES, settings.profile)
        self.b_frames = QSpinBox()
        self.b_frames.setRange(0, 4)
        self.b_frames.setValue(settings.b_frames)
        self.b_frames.setToolTip("0 keeps SprigCast's lighter NVENC path, which uses less memory.")
        self.lookahead = QCheckBox("On")
        self.lookahead.setToolTip("Lets the encoder look a few frames ahead to spend bits where motion starts. "
                                  "Uses more GPU time.")
        self.lookahead.setChecked(settings.lookahead)
        self.adaptive_quant = QCheckBox("Adaptive quantization")
        self.adaptive_quant.setToolTip("Spends more bits on flat areas (skies, walls) where blockiness shows most.")
        self.adaptive_quant.setChecked(settings.adaptive_quant)
        self.audio = QComboBox()
        for kbps in sorted({*AUDIO_KBPS, settings.audio_kbps}):
            self.audio.addItem(f"{kbps} kbps", kbps)
        self.audio.setCurrentIndex(self.audio.findData(settings.audio_kbps))
        self.reset = QPushButton("Reset to defaults")
        self.reset.setStyleSheet(theme.LINK_STYLE)
        self.reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.reset.setAutoDefault(False)
        self.reset.clicked.connect(self.reset_to_defaults)
        self.note = _hint("")
        form.addRow("", self.note)
        self._make_fine()
        self.rate_control.currentIndexChanged.connect(self._update_rows)
        self.encoder.currentIndexChanged.connect(self._update_rows)
        self.encoder.currentIndexChanged.connect(self._show_quality)
        self.quality.activated.connect(self._quality_picked)
        for combo in (self.preset, self.x264_preset, self.multipass):
            combo.currentIndexChanged.connect(self._show_quality)
        self.b_frames.valueChanged.connect(self._show_quality)
        self.lookahead.toggled.connect(self._show_quality)
        self._show_quality()
        self._update_rows()

    def set_expert(self, on: bool) -> None:
        self._expert = on
        self._update_rows()

    # Fine tuning: the encoder's own settings, two to a row, in a card of their own (expert only) ----------------

    FINE_LABELS = (("quality", "Quality steps"), ("rate_control", "Rate control"), ("keyint", "Keyframes"),
                   ("preset", "Preset"), ("x264_preset", "CPU usage preset"), ("profile", "Profile"),
                   ("tuning", "Tune"), ("x264_tune", "Tune"), ("multipass", "Multipass"), ("b_frames", "B-frames"),
                   ("lookahead", "Look-ahead"), ("audio", "Audio bitrate"))

    def _make_fine(self) -> None:
        from PySide6.QtWidgets import QGridLayout

        self._show_audio = True  # Stream → Quality shows the audio bitrate in its own Sound card
        self.fine = QGroupBox("Fine tuning", self)  # (the page puts it in its own place, under this card)
        self.fine.setObjectName("fineTuning")
        outer = QVBoxLayout(self.fine)
        outer.setSpacing(12)
        self._fine_grid = QGridLayout()
        self._fine_grid.setHorizontalSpacing(14)
        self._fine_grid.setVerticalSpacing(10)
        outer.addLayout(self._fine_grid)
        self._fine_pairs: dict[QWidget, QLabel] = {}
        for name, text in self.FINE_LABELS:
            label = QLabel(text, self.fine)  # owned by the card from the start: a row this encoder doesn't use
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            widget = getattr(self, name)
            widget.setParent(self.fine)  # is never laid out, and a widget with no parent outlives the window
            widget.hide()
            label.hide()
            self._fine_pairs[widget] = label
        outer.addWidget(self.adaptive_quant)
        outer.addWidget(self.reset, 0, Qt.AlignmentFlag.AlignLeft)
        self.fine.setVisible(False)

    def _lay_out_fine(self, shown: list[QWidget]) -> None:
        """Put the rows this encoder reads two to a row; the rest are taken out (they'd do nothing)."""
        grid = self._fine_grid
        for widget, label in self._fine_pairs.items():
            grid.removeWidget(widget)
            grid.removeWidget(label)
            widget.setVisible(False)
            label.setVisible(False)
        for i, widget in enumerate(shown):
            row, col = divmod(i, 2)
            label = self._fine_pairs[widget]
            grid.addWidget(label, row, col * 2)
            grid.addWidget(widget, row, col * 2 + 1)
            label.setVisible(True)
            widget.setVisible(True)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

    def set_room(self, load_pct: float | None, has_room: bool = True, text: str = "") -> None:
        """The encoder's load right now (from Garden check's reading); None hides the Load row."""
        self._room = None if load_pct is None else (load_pct, has_room, text)
        if self._room is not None:
            self.load_bar.setValue(int(max(0, min(100, load_pct))))
            colour = theme.ACCENT if has_room else theme.WARN
            self.load_bar.setStyleSheet(f"QProgressBar {{ background: {theme.TRACK}; border: none; border-radius: 4px; }}"
                                        f"QProgressBar::chunk {{ background: {colour}; border-radius: 4px; }}")
            self.load_text.setText(text)
        self._update_rows()

    def _quick_picked(self, key: str) -> None:
        self.quality.setCurrentIndex(max(0, self.quality.findData(key)))
        self._quality_picked()

    def _quality_picked(self, _index: int = 0) -> None:
        key = self.quality.currentData()
        if key == "custom":
            self._show_quality()  # not a step to pick: it's what the expert rows say
            return
        for name, value in quality_changes(key).items():
            widget = getattr(self, name)
            if isinstance(widget, QCheckBox):
                widget.setChecked(value)
            elif isinstance(widget, QSpinBox):
                widget.setValue(value)
            else:
                widget.setCurrentIndex(max(0, widget.findData(value)))
        self._show_quality()

    def _show_quality(self, *_):
        key = quality_of(self.values(), self.codec())
        self.quality.blockSignals(True)
        self.quality.setCurrentIndex(self.quality.findData(key))
        self.quality.blockSignals(False)
        # "Custom" is only shown when the expert rows made it so
        self.quality.view().setRowHidden(self.quality.findData("custom"), key != "custom")
        self.quick.set_value(key)  # none lit for Lightest, Best or Custom (shown under Quality steps)
        self.chip.setText(encoder_chip(self.codec()))

    def reset_to_defaults(self) -> None:
        d = EncoderSettings()
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(d.encoder)))
        for name in ("rate_control", "preset", "tuning", "multipass", "profile", "x264_preset", "x264_tune"):
            combo = getattr(self, name)
            combo.setCurrentIndex(max(0, combo.findData(getattr(d, name))))
        self.cq_level.setValue(d.cq_level)
        self.keyint.setValue(d.keyint_sec)
        self.b_frames.setValue(d.b_frames)
        self.lookahead.setChecked(d.lookahead)
        self.adaptive_quant.setChecked(d.adaptive_quant)
        self.audio.setCurrentIndex(max(0, self.audio.findData(d.audio_kbps)))
        self._show_quality()

    def codec(self) -> str:
        chosen = self.encoder.currentData()
        return self._best if chosen == "auto" else chosen

    def _update_rows(self) -> None:
        """Show the rows the chosen encoder reads; the rest would do nothing."""
        from sprigcast.output.recorder import family

        fam = family(self.codec())
        gpu = fam != "x264"
        x = self._expert
        rows = {
            self.load_row: self._room is not None,
            self.cq_level: self.rate_control.currentData() == "cqp",
        }
        for widget, visible in rows.items():
            self._form.setRowVisible(widget, visible)
        fine = {
            self.quality: True,
            self.rate_control: True,
            self.keyint: True,
            self.preset: gpu and fam != "mf",
            self.x264_preset: not gpu,
            self.tuning: fam in ("nvenc", "amf", "qsv"),
            self.x264_tune: not gpu,
            self.multipass: fam == "nvenc",
            self.profile: self.codec() in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"),
            self.b_frames: fam == "nvenc",
            self.lookahead: fam in ("nvenc", "qsv"),
            self.audio: self._show_audio,
        }
        self._lay_out_fine([w for w, show in fine.items() if show])
        self.adaptive_quant.setVisible(fam in ("nvenc", "amf", "x264"))
        self.fine.setVisible(x)
        notes = {
            "x264": "x264 encodes on the CPU. It looks great but competes with your game for CPU time.",
            "mf": "Windows Media Foundation has few options of its own. Use NVENC, AMF or Quick Sync if you have them.",
        }
        if self.codec().startswith(("hevc", "av1")):
            text = ("HEVC and AV1 make smaller files at the same quality. Most editors open them; some older "
                    "players and phones can't play AV1.")
        else:
            text = notes.get(fam, "")
        self.note.setText(text)
        self._form.setRowVisible(self.note, bool(text))

    def values(self) -> EncoderSettings:
        return EncoderSettings(
            encoder=self.encoder.currentData(),
            x264_preset=self.x264_preset.currentData(),
            x264_tune=self.x264_tune.currentData(),
            rate_control=self.rate_control.currentData(),
            cq_level=self.cq_level.value(),
            keyint_sec=self.keyint.value(),
            preset=self.preset.currentData(),
            tuning=self.tuning.currentData(),
            multipass=self.multipass.currentData(),
            profile=self.profile.currentData(),
            lookahead=self.lookahead.isChecked(),
            adaptive_quant=self.adaptive_quant.isChecked(),
            b_frames=self.b_frames.value(),
            audio_kbps=self.audio.currentData(),
        )


class SpaceBox(QGroupBox):
    """Space: how big the recordings folder is, how much the drive has left, and how many screenshots and
    clips to keep. The size is counted off the UI thread (a folder of long recordings can be big)."""

    def __init__(self, collection: SceneCollection, folder_edit=None, parent: QWidget | None = None):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QFormLayout

        super().__init__("Space", parent)
        self._folder_edit = folder_edit
        self._fallback = collection.recording.folder
        form = QFormLayout(self)
        form.setVerticalSpacing(12)
        self.usage = QLabel("Counting…")
        self.usage.setWordWrap(True)
        form.addRow("Recordings", self.usage)
        rec = collection.recording
        self.keep_screenshots = self._keep_box(rec.keep_screenshots, "screenshots")
        form.addRow("Screenshots", self.keep_screenshots)
        self.keep_clips = self._keep_box(rec.keep_clips, "clips")
        form.addRow("Clips", self.keep_clips)
        self.keep_clips.setToolTip("Screenshots and clips go in a folder for each day. With a limit, the oldest "
                                   "are deleted as new ones arrive.\nRecordings are never deleted.")
        self.keep_screenshots.setToolTip(self.keep_clips.toolTip())
        self._result: list = []
        self._poll = QTimer(self, interval=200)
        self._poll.timeout.connect(self._show)
        self.count()
        if folder_edit is not None:
            folder_edit.editingFinished.connect(self.count)

    @staticmethod
    def _keep_box(value: int, what: str) -> QSpinBox:
        box = QSpinBox()
        box.setRange(0, 10000)
        box.setSingleStep(10)
        box.setSpecialValueText("Keep all")
        box.setPrefix("Keep the last ")
        box.setSuffix(f" {what}")
        box.setValue(max(0, value))
        box.setFixedWidth(260)
        return box

    def folder(self):
        from pathlib import Path

        text = self._folder_edit.text().strip() if self._folder_edit is not None else self._fallback
        return Path(text) if text else None

    def count(self) -> None:
        from sprigcast.output import storage

        folder = self.folder()
        if folder is None:
            self.usage.setText("Pick a folder above.")
            return
        self.usage.setText("Counting…")
        result: list = []
        self._result = result

        def work():
            used = storage.folder_size(folder) if folder.is_dir() else 0
            result.append((used, storage.free_bytes(folder)))

        from sprigcast.core import workers

        workers.run(work, name="folder-size")
        self._poll.start()

    def _show(self) -> None:
        from sprigcast.output import storage

        if not self._result:
            return
        self._poll.stop()
        used, free = self._result[0]
        text = f"{storage.size_text(used)} in this folder"
        if free is not None:
            text += f"  ·  {storage.size_text(free)} free on the drive"
        self.usage.setText(text)

    def apply_to(self, collection: SceneCollection) -> None:
        collection.recording.keep_screenshots = self.keep_screenshots.value()
        collection.recording.keep_clips = self.keep_clips.value()


# Settings → Recording → Split files: (label, minutes, GB)
SPLITS = (("Never", 0, 0.0), ("Every 30 min", 30, 0.0), ("Every hour", 60, 0.0), ("Every 2 hours", 120, 0.0),
          ("Every 4 hours", 240, 0.0), ("At 4 GB (FAT32 drives)", 0, 3.9))


def _tab_box(widget: QWidget) -> QVBoxLayout:
    box = QVBoxLayout(widget)
    box.setContentsMargins(0, 4, 0, 0)
    box.setSpacing(14)
    return box


def _scrolled(inner: QWidget) -> QWidget:
    """A sub-tab that scrolls on its own (the Stream page doesn't scroll as a whole: its place list stays put)."""
    from PySide6.QtWidgets import QScrollArea

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    holder = QWidget()
    pad = QVBoxLayout(holder)
    pad.setContentsMargins(0, 0, 14, 0)
    pad.addWidget(inner)
    scroll.setWidget(holder)
    return scroll


AUDIO_PICKS = (128, 160, 192, 320)
DELAY_PICKS = ((0, "Off"), (10, "10 s"), (30, "30 s"), (60, "60 s"))


def upload_about_kbps() -> int | None:
    """The upload to compare against: Optimize's test, or more when a clean stream carried more (optimize.py)."""
    import math

    from PySide6.QtCore import QSettings

    from sprigcast.app import optimize

    q = QSettings()
    try:
        measured = int(q.value(optimize.MEASURED, 0) or 0)
    except (TypeError, ValueError):
        measured = 0
    proven = optimize.load_proven(q)
    best = max(measured, math.ceil(proven / optimize.UPLOAD_SHARE) if proven else 0)
    return best or None


def place_rows(outputs, canvases) -> list[tuple[str, str, str, str, bool]]:
    """(place, picture, frame rate, bitrate, auto-pilot) for each stream that goes live, in the place list's order."""
    from sprigcast.output.stream import SERVICES

    rows = []
    for o in outputs:
        if o.type != "stream" or not o.enabled or not o.service:
            continue
        canvas = next((c for c in canvases if c.id == o.canvas), None)
        w, h = o.size or ((canvas.width, canvas.height) if canvas else (0, 0))
        fps = o.fps or (canvas.fps if canvas else 0)
        name = SERVICES.get(o.service, SERVICES["custom"]).label
        if o.service == "twitch" and o.enhanced:
            rows.append((name, "Enhanced (Twitch picks)", "Auto", "Auto", False))
            continue
        rows.append((name, f"{o.canvas.capitalize()} {w}×{h}", f"{fps} fps", f"{o.bitrate_kbps:,} kbps",
                     bool(o.auto_bitrate)))
    return rows


class StreamQuality(QWidget):
    """Stream → Quality: the stream encoder (one for every place) with its quick quality picks and load, what each
    place gets, the audio bitrate and the stream delay. Fine tuning (the encoder's own rows) shows with expert on."""

    def __init__(self, collection: SceneCollection, encoder_name: str, streaming: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        box = _tab_box(self)
        self.banner = QLabel("Twitch Enhanced Broadcasting controls some of these settings")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(theme.NOTE_STYLE + " padding: 10px;")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setToolTip("Twitch picks the resolutions, bitrates, keyframes and B-frames of the Twitch "
                               "output. These settings still apply to your other streams.")
        self.banner.setVisible(enhanced_in_use(collection))
        box.addWidget(self.banner)
        if streaming:
            box.addWidget(_hint("You're live. End the stream to change these."))
        self.stream_form = EncoderForm("Encoder", collection.stream_encoder, encoder_name, streaming=True)
        self.stream_form.setEnabled(not streaming)
        self.stream_form._show_audio = False  # in the Sound card below
        self.stream_form._update_rows()
        self.stream_form.fine.setEnabled(not streaming)
        box.addWidget(self.stream_form)
        box.addWidget(self.stream_form.fine)

        # What each place gets: read from the place list (its unsaved choices too: show_places)
        self.places_box = QGroupBox("What each place gets")
        places = QVBoxLayout(self.places_box)
        places.setSpacing(10)
        self.places_grid_holder = QWidget()
        self.places_grid_holder.setObjectName("placesTable")
        self.places_grid_holder.setStyleSheet(
            f"QWidget#placesTable {{ background: {theme.FIELD}; border: 1px solid {theme.SECTION_EDGE};"
            " border-radius: 12px; }")
        from PySide6.QtWidgets import QGridLayout

        self.places_grid = QGridLayout(self.places_grid_holder)
        self.places_grid.setContentsMargins(16, 12, 16, 12)
        self.places_grid.setHorizontalSpacing(18)
        self.places_grid.setVerticalSpacing(12)
        places.addWidget(self.places_grid_holder)
        self.no_places = _hint("No places go live yet.")
        places.addWidget(self.no_places)
        upload = QHBoxLayout()
        upload.addWidget(QLabel("Upload used"))
        upload.addStretch(1)
        self.upload = QLabel()
        upload.addWidget(self.upload)
        places.addLayout(upload)
        self.change_places = QPushButton("Change in Places")
        self.change_places.setStyleSheet(theme.LINK_STYLE)
        self.change_places.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_places.setAutoDefault(False)
        self.go_to_places = lambda: None  # set by Settings: switches to the Places tab
        self.change_places.clicked.connect(lambda: self.go_to_places())
        places.addWidget(self.change_places, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(self.places_box)

        # Sound: quick picks for the audio bitrate (the full list is under Fine tuning)
        sound = QGroupBox("Sound")
        sform = QFormLayout(sound)
        self.audio_quick = Segmented([(k, f"{k} kbps" if k == AUDIO_PICKS[-1] else str(k)) for k in AUDIO_PICKS],
                                     collection.stream_encoder.audio_kbps)
        self.audio_quick.picked.connect(self._audio_picked)
        self.audio_quick.setEnabled(not streaming)
        self.stream_form.audio.currentIndexChanged.connect(
            lambda _i: self.audio_quick.set_value(self.stream_form.audio.currentData()))
        sform.addRow("Audio bitrate", self.audio_quick)
        box.addWidget(sound)

        delay = QGroupBox("Delay")
        form = QFormLayout(delay)
        from sprigcast.output.delay import MAX_DELAY

        self.stream_delay = QSpinBox()
        self.stream_delay.setRange(0, MAX_DELAY)
        self.stream_delay.setSingleStep(5)
        self.stream_delay.setSpecialValueText("Off")
        self.stream_delay.setSuffix(" s")
        self.stream_delay.setValue(int(stream_delay()))
        self.stream_delay.setFixedWidth(110)
        self.stream_delay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stream_delay.setStyleSheet("QSpinBox::up-button, QSpinBox::down-button { width: 0px; border: none; }")
        self.stream_delay.setToolTip("Viewers see everything this many seconds late, so nobody can use your stream "
                                     "to find you in a game.\nRecordings and clips aren't delayed.")
        self.stream_delay.setEnabled(not streaming)
        self.delay_quick = Segmented(list(DELAY_PICKS), self.stream_delay.value())
        self.delay_quick.setToolTip(self.stream_delay.toolTip())
        self.delay_quick.setEnabled(not streaming)
        self.delay_quick.picked.connect(self.stream_delay.setValue)
        self.stream_delay.valueChanged.connect(self.delay_quick.set_value)
        delay_row = QHBoxLayout()
        delay_row.setSpacing(12)
        delay_row.addWidget(self.delay_quick)
        delay_row.addWidget(self.stream_delay)
        delay_row.addStretch(1)
        form.addRow("Viewers see it", delay_row)
        box.addWidget(delay)
        box.addStretch(1)
        self.streaming = streaming
        self._expert = False
        self.show_places(collection.outputs, collection.canvases)
        self.set_expert(False)

    def set_expert(self, on: bool) -> None:
        self._expert = on
        self.stream_form.set_expert(on)
        self.stream_delay.setVisible(on)  # any number of seconds; the quick picks cover most

    def _audio_picked(self, kbps: int) -> None:
        combo = self.stream_form.audio
        combo.setCurrentIndex(max(0, combo.findData(kbps)))

    def show_places(self, outputs, canvases) -> None:
        """Fill in What each place gets (and the upload it takes) from these places."""
        grid = self.places_grid
        while grid.count():
            item = grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        rows = place_rows(outputs, canvases)
        self.places_grid_holder.setVisible(bool(rows))
        self.no_places.setVisible(not rows)
        for col, title in enumerate(("Place", "Picture", "Frame rate", "Bitrate", "Auto-pilot")):
            head = QLabel(title.upper())
            head.setStyleSheet(f"color: {theme.MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px;"
                               " background: transparent; border: none;")
            grid.addWidget(head, 0, col)
        for r, (name, picture, fps, kbps, pilot) in enumerate(rows, start=1):
            cells = [name, picture, fps, kbps, "On" if pilot else "Off"]
            for col, text in enumerate(cells):
                label = QLabel(text)
                style = "background: transparent; border: none;"
                if col == 0:
                    style += f" color: {theme.TEXT_BRIGHT}; font-weight: 600;"
                elif col == 4:
                    style += f" color: {theme.ACCENT if pilot else theme.MUTED};"
                label.setStyleSheet(style)
                grid.addWidget(label, r, col)
        grid.setColumnStretch(1, 1)
        audio = self.stream_form.audio.currentData() or 0
        used = sum(o.bitrate_kbps + audio for o in outputs
                   if o.type == "stream" and o.enabled and o.service and not (o.service == "twitch" and o.enhanced))
        about = upload_about_kbps()
        text = f"<b>{used / 1000:.1f} Mbps</b>"
        if about:
            text += f" of about {about / 1000:.0f} Mbps"
        self.upload.setText(text)
        self.upload.setStyleSheet(f"color: {theme.WARN};" if about and used > about * 0.75 else "")

    def apply_to(self, collection: SceneCollection) -> None:
        from PySide6.QtCore import QSettings

        if not self.streaming:
            collection.stream_encoder = self.stream_form.values()
            QSettings().setValue(STREAM_DELAY, self.stream_delay.value())


STREAM_DELAY = "advanced/stream_delay_s"  # QSettings (the name it has had since it lived on Advanced)
FILENAME = "advanced/filename"
REPLAY_PREFIX = "advanced/replay_prefix"


def stream_delay() -> float:
    """Seconds every stream waits before it goes out (0: no delay)."""
    from PySide6.QtCore import QSettings

    from sprigcast.output.delay import MAX_DELAY

    return float(min(MAX_DELAY, max(0, QSettings().value(STREAM_DELAY, 0, type=int))))


def apply_naming() -> None:
    """Hand the saved file-name patterns to the recorders (at startup and after Settings)."""
    from PySide6.QtCore import QSettings

    from sprigcast.output import naming

    q = QSettings()
    naming.set_patterns(q.value(FILENAME, "", type=str), q.value(REPLAY_PREFIX, "", type=str))


class FileNamesBox(QGroupBox):
    """File names for recordings and clips (expert)."""

    def __init__(self, parent: QWidget | None = None):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QLineEdit

        from sprigcast.output import naming

        super().__init__("File names", parent)
        q = QSettings()
        form = QFormLayout(self)
        self.filename = QLineEdit(q.value(FILENAME, naming.RECORDING_PATTERN, type=str))
        self.filename.setPlaceholderText(naming.RECORDING_PATTERN)
        self.filename.setToolTip("Use {date} {time} {canvas}, or {year} {month} {day} {hour} {minute} {second}.")
        form.addRow("Recordings", self.filename)
        self.replay_prefix = QLineEdit(q.value(REPLAY_PREFIX, naming.REPLAY_PREFIX, type=str))
        self.replay_prefix.setPlaceholderText(naming.REPLAY_PREFIX)
        form.addRow("Clips start with", self.replay_prefix)
        self.filename_example = _hint("")
        form.addRow("", self.filename_example)
        self.filename.textChanged.connect(self._example)
        self.replay_prefix.textChanged.connect(self._example)
        self._example()

    def _example(self, *_):
        from datetime import datetime

        from sprigcast.output import naming

        saved = dict(naming._patterns)
        naming.set_patterns(self.filename.text(), self.replay_prefix.text())
        when = datetime(2026, 11, 14, 19, 30, 5)
        self.filename_example.setText(f"{naming.recording_name('horizontal', when)}.mkv   ·   "
                                      f"{naming.replay_name('vertical', when)}.mkv")
        naming._patterns.update(saved)

    def apply(self) -> None:
        from PySide6.QtCore import QSettings

        from sprigcast.output import naming

        q = QSettings()
        q.setValue(FILENAME, self.filename.text().strip() or naming.RECORDING_PATTERN)
        q.setValue(REPLAY_PREFIX, self.replay_prefix.text().strip() or naming.REPLAY_PREFIX)
        apply_naming()


class RecordingTabs(QWidget):
    """Settings → Recording: the Recording and Clips sub-tabs. The Recording one holds the RecordingPage
    passed in."""

    TABS = ("Recording", "Clips")

    def __init__(self, collection: SceneCollection, encoder_name: str, recording_page: QWidget,
                 recording: bool = False, parent: QWidget | None = None, parts_later: bool = False):
        """parts_later: the encoder, source, space and file name boxes and the Clips tab are made by later
        (one part each, Settings makes them a tick apart: SettingsWindow._slice) or by finish()."""
        super().__init__(parent)
        self.later: list = []
        self._expert = False
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        record_tab = QWidget()
        box = _tab_box(record_tab)
        if recording:
            box.addWidget(_hint("Recording now. Stop the recording to change these."))
        recording_group = QGroupBox("Recording")
        inner = QVBoxLayout(recording_group)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(recording_page)
        rec = collection.recording
        self.same_as_stream = QCheckBox("Same as stream while live")
        self.same_as_stream.setToolTip("Records what you stream (same size, bitrate and sound) without a second "
                                       "encoder.\nLighter on your PC. Recording first and going live after uses "
                                       "the settings below.")
        self.same_as_stream.setChecked(rec.same_as_stream)
        form = recording_page.layout()
        if not isinstance(form, QFormLayout):  # a page without a form: a plain one under it
            form = QFormLayout()
            inner.addLayout(form)
        # Ticks without a label line up with the boxes above them, never out under the labels (user, 2026-11-19).
        form.addRow("", self.same_as_stream)
        self.record_when_live = QCheckBox("Record when I go live")
        self.record_when_live.setToolTip("Ending the stream stops that recording too.")
        self.record_when_live.setChecked(rec.record_when_live)
        form.addRow("", self.record_when_live)
        split_row = QHBoxLayout()
        split_row.setSpacing(10)
        self.split = QComboBox()
        for label, minutes, gb in SPLITS:
            self.split.addItem(label, (minutes, gb))
        here = next((i for i, (_l, m, g) in enumerate(SPLITS) if (m, g) == (rec.split_minutes, rec.split_gb)), None)
        if here is None:  # set some other way (a file edited by hand): shown as it is
            self.split.addItem(f"Every {rec.split_minutes} min" if rec.split_minutes else f"At {rec.split_gb:g} GB",
                               (rec.split_minutes, rec.split_gb))
            here = self.split.count() - 1
        self.split.setCurrentIndex(here)
        self.split.setFixedWidth(220)
        self.split.setToolTip("A new file starts with nothing lost between them.\n"
                              "Drives formatted FAT32 can't hold a file over 4 GB.")
        split_row.addWidget(self.split)
        split_row.addStretch(1)
        form.addRow("Split files", split_row)
        recording_group.setEnabled(not recording)
        box.addWidget(recording_group)
        self.recording = recording

        def encoder():
            self.record_form = EncoderForm("Encoder", rec.encoder, encoder_name)
            self.record_form.setEnabled(not recording)
            self.record_form.set_expert(self._expert)
            self.record_form.fine.setEnabled(not recording)
            box.insertWidget(box.count() - 1, self.record_form)
            box.insertWidget(box.count() - 1, self.record_form.fine)

        def sources():
            from sprigcast.app.source_record_box import SourceRecordBox

            self.source_record = SourceRecordBox(collection)
            self.source_record.setEnabled(not recording)
            box.insertWidget(box.count() - 1, self.source_record)

        def space():
            self.space = SpaceBox(collection, getattr(recording_page, "folder", None))
            box.insertWidget(box.count() - 1, self.space)

        def names():
            self.names = FileNamesBox()
            self.names.setVisible(self._expert)
            box.insertWidget(box.count() - 1, self.names)

        clips_tab = QWidget()

        def clips():
            clips_box = _tab_box(clips_tab)
            clips_box.addWidget(self._clips_group(collection))
            clips_box.addStretch(1)

        box.addStretch(1)
        self.tabs.addTab(record_tab, "Recording")
        self.tabs.addTab(clips_tab, "Clips")
        parts = [encoder, sources, space, names, clips]
        if parts_later:
            self.later = parts
        else:
            for part in parts:
                part()

    def __getattr__(self, name: str):
        """A box asked for before its part was made (parts_later): make them now."""
        later = self.__dict__.get("later")
        if not later or name.startswith("_"):
            raise AttributeError(name)
        self.finish()
        return object.__getattribute__(self, name)

    def take_part(self) -> None:
        """Make the next part still waiting (parts_later)."""
        if self.later:
            self.later.pop(0)()

    def finish(self) -> None:
        while self.later:
            self.take_part()

    def set_expert(self, on: bool) -> None:
        self._expert = bool(on)
        if "record_form" in self.__dict__:
            self.record_form.set_expert(on)
        if "names" in self.__dict__:
            self.names.setVisible(on)

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(1 if name in ("Clips", "Replay") else 0)

    def apply_to(self, collection: SceneCollection) -> None:
        self.finish()
        if not self.recording:
            collection.recording.encoder = self.record_form.values()
            self.source_record.apply_to(collection)
            collection.recording.same_as_stream = self.same_as_stream.isChecked()
            collection.recording.split_minutes, collection.recording.split_gb = self.split.currentData()
            collection.recording.record_when_live = self.record_when_live.isChecked()
        collection.recording.replay_seconds = self.clip_length.checkedId()
        collection.recording.clip_shapes = self.clip_shapes.checkedButton().property("shape")
        collection.recording.clips_on_start = self.clips_on_start.isChecked()
        collection.recording.clips_idle_minutes = int(self.clips_idle.currentData())
        self.space.apply_to(collection)
        self.names.apply()

    def _clips_group(self, collection: SceneCollection) -> QGroupBox:
        """Clips: how long, which shapes, and whether they're ready from the start."""
        group = QGroupBox("Clips")
        form = QFormLayout(group)
        form.setVerticalSpacing(12)
        lengths = QHBoxLayout()
        lengths.setSpacing(6)
        self.clip_length = QButtonGroup(group)
        current = min(CLIP_LENGTHS, key=lambda n: abs(n - collection.recording.replay_seconds))
        for seconds in CLIP_LENGTHS:
            pill = QPushButton(f"{seconds} s")
            pill.setCheckable(True)
            pill.setChecked(seconds == current)
            pill.setStyleSheet(theme.SMALL_PILL_STYLE)
            pill.setCursor(Qt.CursorShape.PointingHandCursor)
            self.clip_length.addButton(pill, seconds)
            lengths.addWidget(pill)
        lengths.addStretch(1)
        form.addRow("Length", lengths)
        shapes = QVBoxLayout()
        shapes.setSpacing(6)
        self.clip_shapes = QButtonGroup(group)
        for key, label, note in (
            ("both", "Both shapes", "two files from the same moment, one wide and one tall"),
            ("horizontal", "Horizontal", "for YouTube and highlights"),
            ("vertical", "Vertical", "for TikTok, Shorts and Reels"),
        ):
            choice = QRadioButton(f"{label}  ·  {note}")
            choice.setProperty("shape", key)
            choice.setChecked(collection.recording.clip_shapes == key)
            self.clip_shapes.addButton(choice)
            shapes.addWidget(choice)
        form.addRow("Shapes", shapes)
        self.clips_on_start = QCheckBox("Get clips ready when SprigCast opens")
        self.clips_on_start.setToolTip("Otherwise the first press of Clips on Live gets them ready")
        self.clips_on_start.setChecked(collection.recording.clips_on_start)
        form.addRow("", self.clips_on_start)
        self.clips_idle = QComboBox()
        for minutes, label in ((5, "After 5 minutes"), (15, "After 15 minutes"), (30, "After 30 minutes"), (0, "Never")):
            self.clips_idle.addItem(label, minutes)
        idle = collection.recording.clips_idle_minutes
        self.clips_idle.setCurrentIndex(max(0, self.clips_idle.findData(idle if idle in (0, 5, 15, 30) else 15)))
        self.clips_idle.setToolTip("While nothing is streamed or recorded and no clip is saved, clips pause to\n"
                                   "save memory and the graphics card. They come back when you go live or record.")
        form.addRow("Pause when unused", self.clips_idle)
        return group


def enhanced_in_use(collection: SceneCollection) -> bool:
    return any(o.type == "stream" and o.enabled and o.service == "twitch" and o.enhanced for o in collection.outputs)


CAMERA_ON = "camera/on"  # QSettings: show SprigCast as a camera while it runs
CAMERA_SHOWS = "camera/shows"  # "auto", "canvas:<id>", "bloom:<scene id>" or "source:<source id>"
CAMERA_SOUND = "camera/sound_to"  # a playback device that gets the stream mix while an app watches, or ""
VIDEO_KINDS_SKIP = ("audio_input", "audio_output", "app_audio", "scene")


def camera_choice() -> str:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_SHOWS, "auto", type=str) or "auto"


def camera_sound() -> str:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_SOUND, "", type=str) or ""


def camera_wanted() -> bool:
    from PySide6.QtCore import QSettings

    return QSettings().value(CAMERA_ON, False, type=bool)


class CameraTab(QWidget):
    """Virtual camera: set it up once (administrator prompt), then switch it on or off."""

    def __init__(self, collection: SceneCollection | None = None, parent: QWidget | None = None):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QComboBox, QFormLayout, QHBoxLayout, QPushButton

        from sprigcast.output import virtualcam

        super().__init__(parent)
        self.vc = virtualcam
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 4, 0, 0)
        box.setSpacing(14)
        from sprigcast.app.live_stage import Switch

        group = QGroupBox("SprigCast Camera")
        inner = QVBoxLayout(group)
        inner.setSpacing(12)
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.on = Switch()  # a whole feature on or off: a switch (Settings redesign, 2026-11-19)
        self.on.setChecked(camera_wanted())
        self.on_text = QLabel()
        on_row = QHBoxLayout()
        on_row.setSpacing(10)
        on_row.addWidget(self.on)
        on_row.addWidget(self.on_text)
        on_row.addStretch(1)
        form.addRow("Camera", on_row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        form.addRow("Status", self.status)
        self.on.toggled.connect(lambda _on: self.refresh())
        self.shows = QComboBox()
        self.shows.addItem("Matches the app (horizontal or vertical)", "auto")
        for canvas in (collection.canvases if collection else []):
            self.shows.addItem(f"{canvas.id.capitalize()} canvas ({canvas.width}\u00d7{canvas.height})",
                               f"canvas:{canvas.id}")
        if collection is not None:
            for scene in collection.scenes:
                if scene.kind == "show" and not scene.shape_of:
                    self.shows.addItem(f"Bloom: {scene.name} ({scene.canvas})", f"bloom:{scene.id}")
            for source in collection.sources:
                if source.kind not in VIDEO_KINDS_SKIP:
                    self.shows.addItem(f"Source: {source.name}", f"source:{source.id}")
        current = self.shows.findData(camera_choice())
        self.shows.setCurrentIndex(max(0, current))
        form.addRow("Shows", self.shows)
        self.sound = QComboBox()
        self.sound.addItem("Not sent", "")
        from sprigcast.sources.audio import device_label

        chosen = camera_sound()
        if chosen:
            self.sound.addItem(device_label(chosen), chosen)
        self.sound.setCurrentIndex(max(0, self.sound.findData(chosen)))
        # The playback devices are listed on a worker: PortAudio's lock is held while Settings scans the sound
        # devices, and waiting for it here froze the window 300 ms on the PC (2026-11-17).
        self._devices: list = []
        from sprigcast.core import workers
        from sprigcast.sources.audio import list_playback_devices

        found = self._devices
        workers.run(lambda: found.append(list_playback_devices()), name="playback-devices")
        self._device_poll = QTimer(self, interval=100)
        self._device_poll.timeout.connect(self._fill_sound)
        self._device_poll.start()
        self.sound.setToolTip("For TikTok LIVE Studio, send the sound to a virtual cable (like CABLE Input) and "
                              "pick its other end (CABLE Output) as the mic there.\n"
                              "It plays only while an app has the camera open.")
        form.addRow("Sound to", self.sound)
        works = QHBoxLayout()
        works.setSpacing(8)
        self.works_with: list[QLabel] = []
        for name in ("TikTok LIVE Studio", "Zoom", "Discord", "OBS", "Browsers"):
            chip = QLabel(name)
            chip.setObjectName("worksWith")
            chip.setToolTip("Lists it as \u201cSprigCast Camera\u201d. If the app wants another shape than what "
                            "it shows, you get black bars instead of a stretched picture.")
            works.addWidget(chip)
            self.works_with.append(chip)
        works.addStretch(1)
        form.addRow("Works with", works)
        group.setStyleSheet(f"QLabel#worksWith {{ color: {theme.LABEL}; border: 1px solid {theme.FIELD_EDGE};"
                            f" border-radius: 12px; padding: 4px 11px; }}")
        inner.addLayout(form)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.setup = QPushButton("Set up the camera")
        self.setup.setStyleSheet(theme.PILL_STYLE)
        self.setup.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setup.clicked.connect(self._set_up)
        row.addWidget(self.setup)
        self.remove = QPushButton("Remove")
        self.remove.setStyleSheet(theme.PILL_STYLE)
        self.remove.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove.clicked.connect(self._remove)
        row.addWidget(self.remove)
        self.privacy = QPushButton("Open settings")
        self.privacy.setToolTip("Windows Settings → Privacy & security → Camera")
        self.privacy.setStyleSheet(theme.PILL_STYLE)
        self.privacy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.privacy.clicked.connect(lambda: self.vc.open_privacy_settings())
        row.addWidget(self.privacy)
        row.addStretch(1)
        inner.addLayout(row)
        self.setup.setToolTip("Copies the camera into Program Files and registers it with Windows, which asks for "
                              "permission once.\nIt uses no memory or CPU until an app opens it.")
        box.addWidget(group)
        box.addStretch(1)
        self._timer = QTimer(self, interval=1500)  # after the admin prompt: notice when setup finished
        self._timer.timeout.connect(self._tick)
        self._ticks = 0
        self.refresh()

    def _fill_sound(self) -> None:
        if not self._devices:
            return
        self._device_poll.stop()
        from sprigcast.sources.audio import device_label

        chosen = self.sound.currentData()
        for name in self._devices[0]:
            if self.sound.findData(name) < 0:
                self.sound.addItem(device_label(name), name)
        self.sound.setCurrentIndex(max(0, self.sound.findData(chosen)))

    def _tick(self) -> None:
        self._ticks += 1
        before = self.remove.isVisible()
        self.refresh()
        if self.remove.isVisible() != before or self._ticks > 40:  # finished, or the prompt was declined
            self._timer.stop()

    def refresh(self) -> None:
        ok, why = self.vc.supported()
        installed = self.vc.installed()
        if not ok:
            text = why
        elif not installed:
            text = "Not set up yet"
        elif self.vc.needs_update():
            text = "Set up, but this SprigCast has a newer camera. Press Update."
        else:
            text = "Ready" if self.on.isChecked() else "Set up"
        blocked = self.vc.privacy_block() if ok else ""
        if blocked:
            text = f"{blocked} Windows won't add SprigCast Camera until you turn it on."
        elif installed and self.vc.state.privacy_failed:
            text = "Windows blocked the camera. Its camera settings may say why."
        ready = text == "Ready"
        self.status.setText(f"\u25cf  {text}" if ready else text)
        self.on_text.setText("On" if self.on.isChecked() else "Off")
        self.privacy.setVisible(bool(blocked) or self.vc.state.privacy_failed)
        colour = theme.ACCENT if ready else theme.MUTED if installed or not ok else theme.TEXT
        self.status.setStyleSheet(f"color: {colour}; font-weight: {600 if ready else 400}")
        self.setup.setVisible(ok and (not installed or self.vc.needs_update()))
        self.setup.setText("Update the camera" if installed else "Set up the camera")
        self.remove.setVisible(ok and installed)
        self.on.setEnabled(ok)

    def _set_up(self) -> None:
        if self.vc.set_up():
            self.status.setText("Waiting for Windows… say Yes on its prompt.")
            self._ticks = 0
            self._timer.start()

    def _remove(self) -> None:
        self.on.setChecked(False)
        if self.vc.remove():
            self._ticks = 0
            self._timer.start()

    def apply(self) -> None:
        from PySide6.QtCore import QSettings

        QSettings().setValue(CAMERA_ON, self.on.isChecked())
        QSettings().setValue(CAMERA_SHOWS, self.shows.currentData())
        QSettings().setValue(CAMERA_SOUND, self.sound.currentData() or "")
