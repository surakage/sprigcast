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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from sprigcast.app import icons, theme
from sprigcast.app.card import CardWindow
from sprigcast.app.destinations import problem
from sprigcast.core.scene import SceneCollection
from sprigcast.output.stream import SERVICES

LEVELS = {"bad": 0, "warning": 1, "info": 2, "good": 3}
COLOURS = {"bad": theme.BAD, "warning": theme.WARN, "info": theme.INFO, "good": theme.ACCENT}
LISTEN_SECONDS = 3.0
SILENT_DB = -50.0  # a mic that never peaks above this while you talk is probably the wrong device
CLIPPING_DB = -1.0
LOW_DISK_GB = 10.0
# Statuses that are fine, by how they start (any case). paused: asleep while off screen (#87); listening,
# loading, getting: a plugin listening, or getting what it needs ready.
OK_STATUSES = ("ok", "starting", "capturing", "waiting", "paused", "listening", "loading", "getting")


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
    upload_kbps: int | None = None  # what the upload carries (Optimize's test, or what a clean stream carried)


class PeakWatch:
    """Remembers each audio source's loudest peak over the last few seconds."""

    def __init__(self, window: float = LISTEN_SECONDS):
        self.window = window
        self._peaks: dict[str, deque[tuple[float, float]]] = {}

    def feed(self, levels: dict[str, tuple[float, float]], now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        oldest = now - self.window
        for sid, (peak, _rms) in levels.items():
            history = self._peaks.get(sid)
            if history is None:
                history = self._peaks[sid] = deque()
            history.append((now, peak))
            while history[0][0] < oldest:
                history.popleft()

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
                                                "clear some space or pick another folder in Settings → Recording."))
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
    found += _all_places(c)
    found += _shared_higher(c)
    if not c.last_streams and any(o.enabled and o.type == "stream" for o in c.collection.outputs):
        found.append(Finding("info", "Upload", "No stream measured yet: a short test stream lets the doctor "
                                               "check your upload (Twitch has a bandwidth test mode in its ⚙)."))
    return found


def planned_kbps(collection: SceneCollection) -> int:
    """What every place that's on sends together, sound included. Enhanced Broadcasting counts its cap (or
    its fallback bitrate when Twitch decides)."""
    audio = collection.stream_encoder.audio_kbps
    total = 0
    for o in collection.outputs:
        if o.type == "stream" and o.enabled:
            video = o.enhanced_max_kbps if o.enhanced and o.enhanced_max_kbps else o.bitrate_kbps
            total += video + audio
    return total


def usable_upload(settings) -> int | None:
    """The upload to check against: the test's result with headroom, or what a clean stream carried."""
    from sprigcast.app.optimize import MEASURED, UPLOAD_SHARE, load_proven

    try:
        measured = int(settings.value(MEASURED, 0) or 0)
    except (TypeError, ValueError):
        measured = 0
    best = max(int(measured * UPLOAD_SHARE), load_proven(settings))
    return best or None


def _shared_higher(c: Checkup) -> list[Finding]:
    """A place that shares an encoder goes out at that encoder's bitrate, the highest of the group's: set at 4500 next
    to one at 6000, it sent 6000 (PC live 2026-11-21: four places at 5400-6000 with 4500 wanted)."""
    from types import SimpleNamespace

    from sprigcast.output.shared_stream import bitrate_groups

    by_canvas: dict[tuple, list] = {}
    for o in c.collection.outputs:
        if o.type != "stream" or not o.enabled or o.enhanced:
            continue
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        kbps = o.bitrate_kbps if o.ignore_recommendations else min(o.bitrate_kbps, service.max_kbps)
        by_canvas.setdefault((o.canvas, o.size, o.fps), []).append(
            SimpleNamespace(bitrate_kbps=kbps, max_kbps=service.max_kbps, auto_bitrate=o.auto_bitrate, output=o))
    found = []
    for targets in by_canvas.values():
        for kbps, _auto, members in bitrate_groups(targets):
            lower = [m for m in members if m.bitrate_kbps < kbps]
            if lower and len(members) > 1:
                names = ", ".join(f"{_label(m.output)} ({m.bitrate_kbps} kbps)" for m in lower)
                found.append(Finding("warning", "Upload", f"{names} share{'s' if len(lower) == 1 else ''} one encoder "
                                     f"with a place set higher, so {'it goes' if len(lower) == 1 else 'they go'} out at "
                                     f"{kbps} kbps: set the same bitrate on these places to send what you chose."))
    return found


def _all_places(c: Checkup) -> list[Finding]:
    """Multistreaming: each place alone may fit, but together they share one upload."""
    if not c.upload_kbps:
        return []
    total = planned_kbps(c.collection)
    places = sum(1 for o in c.collection.outputs if o.type == "stream" and o.enabled)
    if total <= c.upload_kbps:
        return []
    many = f"Your {places} places add up to" if places > 1 else "This stream needs"
    fix = ("lower their bitrates or go live to fewer places" if places > 1 else "lower its bitrate")
    level = "bad" if total > c.upload_kbps * 1.25 else "warning"
    return [Finding(level, "Upload", f"{many} {total / 1000:.1f} Mbps and your upload carries about "
                                     f"{c.upload_kbps / 1000:.1f} Mbps: {fix}.")]


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
    sources = [s for s in c.collection.sources if s.kind in ("audio_input", "audio_output", "app_audio")]
    mics = [s for s in sources if s.kind == "audio_input" and not s.settings.get("capture_of")]  # not a card's sound
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
        elif s in mics and peak < SILENT_DB:
            found.append(Finding("warning", "Audio", f"{s.name} heard nothing while the doctor listened: "
                                                     "check it's the right device and not muted in Windows."))
    return found


def _sources(c: Checkup) -> list[Finding]:
    found = []
    for s in c.collection.sources:
        status = c.statuses.get(s.id)
        if status == "No page set":  # an empty browser source shows nothing: a note, not a reason to stop
            found.append(Finding("info", "Sources", f"{s.name} has no page yet: it shows nothing until you add one."))
            continue
        if status == "no file":  # an empty media source waits for a file (a raid clip, cleared on end): idle
            found.append(Finding("info", "Sources", f"{s.name} has no file yet: it shows nothing until one is set."))
            continue
        if status and "needs Windows" in status:  # nothing to plug in: this Windows is too old for it
            found.append(Finding("bad", "Sources", f"{s.name} doesn't work on this Windows "
                                                   f"({status.removeprefix('unavailable: ')}): remove it in Sources."))
            continue
        if status and status.startswith("Sound plays on desktop"):  # a page's note, not a problem
            continue
        if status and status.startswith("Video can't play"):  # the page works; one video on it can't play
            what = "it's H.264 (MP4), which the page engine can't play yet" if "H.264" in status else \
                "its format isn't supported"
            found.append(Finding("warning", "Sources", f"{s.name}: a video on its page can't play ({what}). "
                                                       "The rest of the page still shows."))
            continue
        if status and not status.lower().startswith(OK_STATUSES):
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


AREA_ICONS = {"Keys": "key", "Upload": "upload", "Encoder": "gear", "GPU": "monitor", "Audio": "mic", "Sources": "camera",
              "Disk": "disk", "All": "leaf"}  # drawn by app/icons.py
BADGES = {"bad": "✕", "warning": "!", "good": "✓", "checking": "…"}
SUBTITLES = {
    "bad": "Fix the red ones before you go live.",
    "warning": "You can go live, but these may cause trouble.",
    "good": "Keys, upload, GPU, audio and sources all look good.",
    "checking": "Say something into your mic so the doctor can hear it.",
}
STYLE = f"""
QFrame#doctorHeader {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE}; border-top-color: {theme.HIGHLIGHT};
                      border-radius: 14px; }}
QFrame#doctorCard {{ background: {theme.PANEL}; border: 1px solid {theme.EDGE}; border-radius: 12px; }}
QLabel#doctorTitle {{ font-size: 17pt; font-weight: 700; }}
QLabel#doctorSub, QLabel#cardFix {{ color: {theme.MUTED}; }}
QLabel#cardTitle {{ font-weight: 600; font-size: 10.5pt; }}
QLabel#chip {{ color: {theme.TEXT}; background: {theme.RAISED}; border-radius: 9px; padding: 2px 9px;
              font-size: 8pt; }}
QLabel#sectionLabel {{ color: {theme.MUTED}; font-size: 8.5pt; font-weight: 600; letter-spacing: 1px; }}
QProgressBar {{ background: {theme.RAISED}; border: none; border-radius: 2px; max-height: 4px; }}
QProgressBar::chunk {{ background: {theme.ACCENT}; border-radius: 2px; }}
"""


class DoctorDialog(CardWindow):
    """Listens for a few seconds, then shows a verdict header and a card per finding."""

    def __init__(self, gather: Callable[[], Checkup], parent: QWidget | None = None, listen: float = LISTEN_SECONDS):
        super().__init__("Stream doctor", parent)
        self.setStyleSheet(STYLE)
        self.card.setMinimumWidth(560)
        self._gather, self._listen = gather, listen
        self.findings: list[Finding] = []
        root = QVBoxLayout(self.card)
        root.setContentsMargins(18, 18, 18, 16)
        root.setSpacing(12)

        header = QFrame()
        header.setObjectName("doctorHeader")
        # (No drop shadow: blurred in software on every paint, and the busy bar inside it repaints all the
        # while it listens; the first paint was most of the 96 ms the doctor took to open on the PC, 2026-11-18.)
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
        from sprigcast.app.empty import ArtPicture

        self.sloth = ArtPicture("happy", 84, height=84)  # puzzled with a loose vine for trouble, waving when all good
        self.sloth.hide()
        top.addWidget(self.sloth, 0, Qt.AlignmentFlag.AlignVCenter)
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
        self.sloth.setVisible(level in ("bad", "warning", "good"))
        if level in ("bad", "warning", "good"):
            self.sloth.set_art("happy" if level == "good" else "trouble")

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
    layout = QHBoxLayout(card)
    layout.setContentsMargins(14, 10, 12, 10)
    layout.setSpacing(12)
    icon = QLabel()
    icon.setPixmap(icons.pixmap(AREA_ICONS.get(f.area, "dot"), colour, 16))
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
