"""Stream doctor: checks keys, upload, GPU, audio and sources before you go live, and says what to fix.

`examine()` is pure: it turns a `Checkup` (everything the doctor looked at) into findings, one sentence
each, worst first. The dialog gathers the checkup, listening to the mics for a few seconds first.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import theme
from sprigcast.app.destinations import problem
from sprigcast.core.scene import SceneCollection
from sprigcast.output.stream import SERVICES

LEVELS = {"bad": 0, "warning": 1, "info": 2, "good": 3}
COLOURS = {"bad": theme.BAD, "warning": theme.WARN, "info": theme.INFO, "good": theme.ACCENT}
LISTEN_SECONDS = 3.0
SILENT_DB = -50.0  # a mic that never peaks above this while you talk is probably the wrong device
CLIPPING_DB = -1.0
LOW_DISK_GB = 10.0
OK_STATUSES = ("ok", "starting", "capturing", "waiting", "paused")  # paused: asleep while off screen (#87)


@dataclass
class Finding:
    level: str  # "bad" | "warning" | "info" | "good"
    area: str  # "Keys", "Upload", "Encoder", "GPU", "Audio", "Sources", "Disk"
    text: str  # one sentence: what is wrong and what to do

    @property
    def title(self) -> str:
        """What's wrong, short: the part before the first colon."""
        head, _, _ = self.text.partition(": ")
        return head

    @property
    def fix(self) -> str:
        """What to do about it: the part after the first colon, capitalised (empty if none)."""
        _, _, tail = self.text.partition(": ")
        return tail[:1].upper() + tail[1:]


@dataclass
class LastStream:
    """How a destination's previous stream went, saved when it ends."""

    kbps: float  # what actually went out
    network_pct: float  # share of the time spent waiting to upload
    loss_pct: float  # frames dropped
    encode_pct: float  # encoding time as a share of the frame budget
    bitrate_kbps: int = 0  # the bitrate setting it ran with; lowering it below this clears the advice

    @classmethod
    def of(cls, streamer) -> LastStream | None:
        st = streamer.stats()
        running = max(1e-6, time.monotonic() - streamer.started_at)
        if not st["frames"]:
            return None
        return cls(kbps=streamer.bytes_out * 8 / 1000 / running, network_pct=st["network_pct"],
                   loss_pct=st["dropped_pct"], encode_pct=100 * st["encode_ms"] / max(1e-6, st["budget_ms"]),
                   bitrate_kbps=int(getattr(streamer, "bitrate_kbps", 0)))


@dataclass
class Checkup:
    collection: SceneCollection
    has_key: Callable[[str | None], bool]
    codec: str = "h264_nvenc"
    render_fps: float | None = None  # from the engine's last Stats, None before the first
    render_ms: float | None = None
    gpu_pct: float | None = None  # None when nvidia-smi isn't there
    loudest: dict[str, float] = field(default_factory=dict)  # audio source id -> loudest peak (dBFS) heard
    statuses: dict[str, str] = field(default_factory=dict)  # source id -> engine status line
    last_streams: dict[str, LastStream] = field(default_factory=dict)  # output id -> previous stream
    free_gb: float | None = None  # in the recordings folder
    recording_planned: bool = False


class PeakWatch:
    """Remembers each audio source's loudest peak over the last few seconds."""

    def __init__(self, window: float = LISTEN_SECONDS):
        self.window = window
        self._peaks: dict[str, list[tuple[float, float]]] = {}

    def feed(self, levels: dict[str, tuple[float, float]], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for sid, (peak, _rms) in levels.items():
            history = self._peaks.setdefault(sid, [])
            history.append((now, peak))
            while history and history[0][0] < now - self.window:
                history.pop(0)

    def loudest(self) -> dict[str, float]:
        return {sid: max(p for _, p in h) for sid, h in self._peaks.items() if h}


def examine(c: Checkup) -> list[Finding]:
    """Everything worth knowing before going live, worst first."""
    found: list[Finding] = []
    found += _keys(c)
    found += _upload(c)
    found += _encoder(c)
    found += _audio(c)
    found += _sources(c)
    if c.recording_planned and c.free_gb is not None and c.free_gb < LOW_DISK_GB:
        found.append(Finding("warning", "Disk", f"Only {c.free_gb:.1f} GB free for recordings: "
                                                "clear some space or pick another folder in Settings → Output."))
    if not any(f.level in ("bad", "warning") for f in found):
        found.insert(0, Finding("good", "All", "Everything checks out: keys, upload, GPU, audio and sources look good."))
    return sorted(found, key=lambda f: LEVELS[f.level])


def headline(findings: list[Finding]) -> str:
    """The verdict in a few words: ready, or how many things to fix or check."""
    bad = sum(1 for f in findings if f.level == "bad")
    warn = sum(1 for f in findings if f.level == "warning")
    if bad:
        return f"{bad} thing{'s' * (bad > 1)} to fix" + (f", {warn} to check" if warn else "")
    if warn:
        return f"{warn} thing{'s' * (warn > 1)} to check"
    return "Ready to go live"


def overall(findings: list[Finding]) -> str:
    worst = findings[0].level if findings else "good"
    return "good" if worst == "info" else worst


def _label(output) -> str:
    return SERVICES.get(output.service or "custom", SERVICES["custom"]).label


def _keys(c: Checkup) -> list[Finding]:
    outputs = [o for o in c.collection.outputs if o.type == "stream" and o.enabled]
    if not outputs:
        return [Finding("warning", "Keys", "No destination is switched on: turn one on in the Destinations dock.")]
    found = []
    for o in outputs:
        why = problem(o, c.collection, c.has_key)
        if why:
            found.append(Finding("bad", "Keys", f"{_label(o)} {why.lower()}: fix it with ⚙ on its Destinations card."))
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        if not o.ignore_recommendations and not o.enhanced and o.bitrate_kbps > service.max_kbps:
            found.append(Finding("info", "Keys", f"{service.label} caps the bitrate at {service.max_kbps} kbps: "
                                                 f"your {o.bitrate_kbps} kbps will be lowered to that."))
    return found


def _upload(c: Checkup) -> list[Finding]:
    found = []
    for o in c.collection.outputs:
        if o.type != "stream" or not o.enabled:
            continue
        last = c.last_streams.get(o.id)
        if last is None or (last.bitrate_kbps and o.bitrate_kbps < last.bitrate_kbps):
            continue  # never measured, or already lowered since
        if last.loss_pct > 1 and last.network_pct > 50:
            suggest = int(max(1000, last.kbps * 0.8) // 500 * 500)
            found.append(Finding("bad", "Upload", f"Last stream to {_label(o)} dropped {last.loss_pct:.0f}% because "
                                                  f"the upload couldn't keep up: lower its bitrate to about {suggest} kbps."))
        elif last.network_pct > 50:
            found.append(Finding("warning", "Upload", f"Last stream to {_label(o)} was close to your upload limit: "
                                                      "lower its bitrate a little for headroom."))
        elif last.loss_pct > 1 and last.encode_pct > 60:
            found.append(Finding("bad", "Encoder", f"Last stream to {_label(o)} dropped {last.loss_pct:.0f}% because "
                                                   "encoding was too slow: lower its resolution or frame rate."))
    if not c.last_streams and any(o.enabled and o.type == "stream" for o in c.collection.outputs):
        found.append(Finding("info", "Upload", "No stream measured yet: a short test stream lets the doctor "
                                               "check your upload (Twitch has a bandwidth test mode in its ⚙)."))
    return found


def _encoder(c: Checkup) -> list[Finding]:
    found = []
    if c.codec == "libx264":
        found.append(Finding("warning", "Encoder", "No graphics-card encoder is working, so your CPU encodes: "
                                                   "update the graphics driver and restart SprigCast."))
    target = max((cv.fps for cv in c.collection.canvases if cv.enabled), default=60)
    if c.render_fps is not None and c.render_fps < 0.9 * target:
        found.append(Finding("bad", "GPU", f"The preview runs at {c.render_fps:.0f} of {target} fps: "
                                           "close games or heavy apps, or lower the canvas frame rate."))
    elif c.render_ms is not None and c.render_ms > 0.7 * 1000 / target:
        found.append(Finding("warning", "GPU", f"Drawing a frame takes {c.render_ms:.1f} of {1000 / target:.1f} ms: "
                                               "there is little room left, so hide sources you don't need."))
    if c.gpu_pct is not None and c.gpu_pct > 90:
        found.append(Finding("warning", "GPU", f"Your graphics card is {c.gpu_pct:.0f}% busy: "
                                               "a game or browser may slow the stream down."))
    return found


def _audio(c: Checkup) -> list[Finding]:
    found = []
    sources = [s for s in c.collection.sources if s.kind in ("audio_input", "audio_output")]
    mics = [s for s in sources if s.kind == "audio_input"]
    if not mics:
        found.append(Finding("info", "Audio", "No microphone added: add one with + in the Audio Mixer if you talk."))
    for s in sources:
        if s.settings.get("muted"):
            if s.kind == "audio_input":
                found.append(Finding("warning", "Audio", f"{s.name} is muted in the mixer: click its speaker to unmute."))
            continue
        peak = c.loudest.get(s.id)
        if peak is None:
            continue
        if peak >= CLIPPING_DB:
            found.append(Finding("warning", "Audio", f"{s.name} is clipping: lower its fader or the gain in Windows."))
        elif s.kind == "audio_input" and peak < SILENT_DB:
            found.append(Finding("warning", "Audio", f"{s.name} heard nothing while the doctor listened: "
                                                     "check it's the right device and not muted in Windows."))
    return found


def _sources(c: Checkup) -> list[Finding]:
    found = []
    for s in c.collection.sources:
        status = c.statuses.get(s.id)
        if status and not status.startswith(OK_STATUSES):
            found.append(Finding("bad", "Sources", f"{s.name} isn't working ({status}): "
                                                   "check it's plugged in or open, or edit it in Sources."))
    return found


# Gathering ---------------------------------------------------------------------


_nvml = None  # (library, device handles) once NVML is up; False if it can't be used


def _nvml_load() -> float | None:
    """GPU busy % straight from the NVIDIA driver's NVML library, in this process. None if unavailable.

    Much cheaper than starting nvidia-smi (a whole new process, which itself loads NVML) every few seconds.
    """
    global _nvml
    import ctypes

    if _nvml is None:
        _nvml = False
        try:
            lib = ctypes.WinDLL("nvml.dll") if sys.platform == "win32" else ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                return None
            count = ctypes.c_uint()
            if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != 0:
                return None
            handles = []
            for i in range(count.value):
                h = ctypes.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                    handles.append(h)
            if handles:
                _nvml = (lib, handles)
        except (OSError, AttributeError):
            return None
    if not _nvml:
        return None

    class Utilization(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    lib, handles = _nvml
    best = None
    for h in handles:
        u = Utilization()
        if lib.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0:
            best = max(best or 0.0, float(u.gpu))
    return best


def gpu_load() -> float | None:
    """GPU utilisation in percent, or None when it can't be read.

    Windows' own counters first (any card, almost free); NVIDIA's NVML and nvidia-smi only if those
    aren't there.
    """
    from sprigcast.app.gpu_usage import windows_gpu_load

    load = windows_gpu_load()
    if load is not None:
        return load
    load = _nvml_load()
    if load is not None or _nvml:
        return load
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=2,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
        return max(float(v) for v in out.stdout.split() if v.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def free_space_gb(folder: Path) -> float | None:
    try:
        existing = folder if folder.exists() else Path(folder.anchor or Path.home())
        return shutil.disk_usage(existing).free / 1e9
    except OSError:
        return None


def save_last_streams(settings, streamers) -> None:
    """Remember how each destination's stream went (QSettings "doctor/last_streams")."""
    saved = load_last_streams(settings)
    for _, st in streamers:
        last = LastStream.of(st)
        if last is not None and getattr(st, "output_id", None):  # bandwidth tests count: that's what they're for
            saved[st.output_id] = last
    settings.setValue("doctor/last_streams", json.dumps({k: vars(v) for k, v in saved.items()}))


def load_last_streams(settings) -> dict[str, LastStream]:
    try:
        raw = json.loads(str(settings.value("doctor/last_streams", "{}") or "{}"))
        return {k: LastStream(**v) for k, v in raw.items()}
    except (ValueError, TypeError):
        return {}


# Dialog -------------------------------------------------------------------------


AREA_ICONS = {"Keys": "🔑", "Upload": "📶", "Encoder": "⚙", "GPU": "🖥", "Audio": "🎙", "Sources": "🎥",
              "Disk": "💾", "All": "🌿"}
BADGES = {"bad": "✕", "warning": "!", "good": "✓", "checking": "…"}
SUBTITLES = {
    "bad": "Fix the red ones before you go live.",
    "warning": "You can go live, but these may cause trouble.",
    "good": "Keys, upload, GPU, audio and sources all look good.",
    "checking": "Say something into your mic so the doctor can hear it.",
}
STYLE = f"""
QDialog#doctor {{ background: {theme.BG}; }}
QFrame#doctorHeader {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE}; border-top-color: {theme.HIGHLIGHT};
                      border-radius: 14px; }}
QFrame#doctorCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE}; border-radius: 12px; }}
QLabel#doctorTitle {{ font-size: 17pt; font-weight: 700; }}
QLabel#doctorSub, QLabel#cardFix {{ color: {theme.MUTED}; }}
QLabel#cardTitle {{ font-weight: 600; font-size: 10.5pt; }}
QLabel#chip {{ color: {theme.TEXT}; background: {theme.RAISED}; border-radius: 9px; padding: 2px 9px;
              font-size: 8pt; }}
QLabel#sectionLabel {{ color: {theme.MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
"""


def _shadow(widget: QWidget, blur: int = 18, dy: int = 4, alpha: int = 150) -> None:
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)


class DoctorDialog(QDialog):
    """Listens for a few seconds, then shows a verdict header and a card per finding."""

    def __init__(self, gather: Callable[[], Checkup], parent: QWidget | None = None, listen: float = LISTEN_SECONDS):
        super().__init__(parent)
        self.setObjectName("doctor")
        self.setWindowTitle("Stream doctor")
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(560)
        self._gather, self._listen = gather, listen
        self.findings: list[Finding] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("doctorHeader")
        _shadow(header, 24, 6)
        top = QHBoxLayout(header)
        top.setContentsMargins(16, 14, 16, 14)
        top.setSpacing(14)
        self.badge = QLabel()
        self.badge.setFixedSize(56, 56)
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)
        words = QVBoxLayout()
        words.setSpacing(4)
        self.headline = QLabel()
        self.headline.setObjectName("doctorTitle")
        self.subtitle = QLabel()
        self.subtitle.setObjectName("doctorSub")
        self.subtitle.setWordWrap(True)
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setTextVisible(False)
        words.addWidget(self.headline)
        words.addWidget(self.subtitle)
        words.addWidget(self.busy)
        top.addLayout(words, 1)
        root.addWidget(header)

        holder = QWidget()
        holder.setStyleSheet("background: transparent")
        self.rows = QVBoxLayout(holder)
        self.rows.setContentsMargins(2, 0, 2, 2)
        self.rows.setSpacing(8)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        self.scroll.viewport().setStyleSheet("background: transparent")
        self.scroll.setWidget(holder)
        root.addWidget(self.scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.again = QPushButton("↻  Check again")
        self.again.clicked.connect(self.start)
        done = QPushButton("Done")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        buttons.addWidget(self.again)
        buttons.addWidget(done)
        root.addLayout(buttons)
        self.start()

    def _set_verdict(self, level: str, title: str) -> None:
        colour = COLOURS.get(level, theme.ACCENT)
        self.badge.setText(BADGES[level])
        self.badge.setStyleSheet(
            f"background: qradialgradient(cx:0.35, cy:0.3, radius:0.9, stop:0 {QColor(colour).lighter(125).name()},"
            f" stop:1 {colour}); color: {theme.ACCENT_TEXT}; border-radius: 28px; font-size: 24pt; font-weight: 800;"
        )
        self.headline.setText(title)
        self.headline.setStyleSheet(f"color: {colour if level != 'checking' else theme.TEXT_BRIGHT}")
        self.subtitle.setText(SUBTITLES[level])
        self.busy.setVisible(level == "checking")

    def start(self) -> None:
        self.again.setEnabled(False)
        self.scroll.setVisible(False)
        self._set_verdict("checking", "Checking…")
        self._clear_rows()
        QTimer.singleShot(int(self._listen * 1000), self.finish)

    def finish(self) -> None:
        self.findings = examine(self._gather())
        self._set_verdict(overall(self.findings), headline(self.findings))
        self._clear_rows()
        problems = [f for f in self.findings if f.level in ("bad", "warning")]  # the header covers all-clear
        tips = [f for f in self.findings if f.level == "info"]
        for f in problems:
            self.rows.addWidget(_finding_card(f))
        if tips:
            label = QLabel("GOOD TO KNOW")
            label.setObjectName("sectionLabel")
            self.rows.addSpacing(4)
            self.rows.addWidget(label)
            for f in tips:
                self.rows.addWidget(_finding_card(f))
        self.rows.addStretch(1)
        self.scroll.setVisible(bool(problems or tips))
        self.again.setEnabled(True)
        holder = self.scroll.widget()
        holder.adjustSize()
        wanted = min(520, holder.layout().totalHeightForWidth(max(500, self.scroll.viewport().width())))
        self.scroll.setMinimumHeight(max(80, wanted))
        self.adjustSize()

    def _clear_rows(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()


def _finding_card(f: Finding) -> QWidget:
    colour = COLOURS[f.level]
    card = QFrame()
    card.setObjectName("doctorCard")
    card.setStyleSheet(f"QFrame#doctorCard {{ border-left: 4px solid {colour}; }}")
    _shadow(card, 14, 3, 120)
    layout = QHBoxLayout(card)
    layout.setContentsMargins(14, 10, 12, 10)
    layout.setSpacing(12)
    icon = QLabel(AREA_ICONS.get(f.area, "•"))
    icon.setFixedSize(34, 34)
    icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
    icon.setStyleSheet(f"background: {QColor(colour).darker(260).name()}; border: 1px solid {colour};"
                       " border-radius: 17px; font-size: 13pt;")
    layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
    words = QVBoxLayout()
    words.setSpacing(2)
    title = QLabel(f.title)
    title.setObjectName("cardTitle")
    title.setWordWrap(True)
    title.setMinimumWidth(340)  # so wrapped lines get their full height
    title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    words.addWidget(title)
    if f.fix:
        fix = QLabel(f.fix)
        fix.setObjectName("cardFix")
        fix.setWordWrap(True)
        fix.setMinimumWidth(340)
        fix.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        words.addWidget(fix)
    layout.addLayout(words, 1)
    chip = QLabel(f.area if f.area != "All" else "All clear")
    chip.setObjectName("chip")
    layout.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)
    return card
