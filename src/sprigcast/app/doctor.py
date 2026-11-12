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
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from sprigcast.app.destinations import problem
from sprigcast.core.scene import SceneCollection
from sprigcast.output.stream import SERVICES

LEVELS = {"bad": 0, "warning": 1, "info": 2, "good": 3}
COLOURS = {"bad": "#ff5a5a", "warning": "#ffd23f", "info": "#7fb8ff", "good": "#3ddc84"}
LISTEN_SECONDS = 3.0
SILENT_DB = -50.0  # a mic that never peaks above this while you talk is probably the wrong device
CLIPPING_DB = -1.0
LOW_DISK_GB = 10.0
OK_STATUSES = ("ok", "starting", "capturing", "waiting")


@dataclass
class Finding:
    level: str  # "bad" | "warning" | "info" | "good"
    area: str  # "Keys", "Upload", "Encoder", "GPU", "Audio", "Sources", "Disk"
    text: str  # one sentence: what is wrong and what to do


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
        found.insert(0, Finding("good", "All", "Everything checks out."))
    return sorted(found, key=lambda f: LEVELS[f.level])


def headline(findings: list[Finding]) -> str:
    """The one sentence to read: the worst problem, or the all-clear."""
    worst = findings[0] if findings else None
    if worst is None or worst.level in ("good", "info"):
        return "You're ready to go live."
    more = sum(1 for f in findings if f.level in ("bad", "warning")) - 1
    return worst.text + (f" (+{more} more below)" if more > 0 else "")


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
            found.append(Finding("bad", "Keys", f"{_label(o)}: {why.lower()}. Fix it with ⚙ on its Destinations card."))
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        if not o.ignore_recommendations and not o.enhanced and o.bitrate_kbps > service.max_kbps:
            found.append(Finding("info", "Keys", f"{service.label} accepts up to {service.max_kbps} kbps, "
                                                 f"so {o.bitrate_kbps} kbps will be capped."))
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
    if c.codec != "h264_nvenc":
        found.append(Finding("warning", "Encoder", "The NVIDIA encoder isn't working, so your CPU encodes: "
                                                   "update the NVIDIA driver and restart SprigCast."))
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
            found.append(Finding("bad", "Sources", f"{s.name}: {status}. Check it's plugged in or open, "
                                                   "or edit it in Sources."))
    return found


# Gathering ---------------------------------------------------------------------


def gpu_load() -> float | None:
    """NVIDIA GPU utilisation in percent, or None when nvidia-smi isn't available."""
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


class DoctorDialog(QDialog):
    """Listens for a few seconds, then shows the headline and every finding with a coloured dot."""

    def __init__(self, gather: Callable[[], Checkup], parent: QWidget | None = None, listen: float = LISTEN_SECONDS):
        super().__init__(parent)
        self.setWindowTitle("Stream doctor")
        self.setMinimumWidth(520)
        self._gather, self._listen = gather, listen
        self.findings: list[Finding] = []
        root = QVBoxLayout(self)
        root.setSpacing(8)
        self.headline = QLabel()
        self.headline.setWordWrap(True)
        font = self.headline.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        self.headline.setFont(font)
        root.addWidget(self.headline)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(4)
        root.addLayout(self.rows)
        root.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.again = QPushButton("Check again")
        self.again.clicked.connect(self.start)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(self.again)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self.start()

    def start(self) -> None:
        self.again.setEnabled(False)
        self.headline.setStyleSheet("")
        self.headline.setText("Checking… say something into your mic.")
        self._clear_rows()
        QTimer.singleShot(int(self._listen * 1000), self.finish)

    def finish(self) -> None:
        self.findings = examine(self._gather())
        worst = self.findings[0].level
        self.headline.setText(headline(self.findings))
        self.headline.setStyleSheet(f"color: {COLOURS['good' if worst == 'info' else worst]}")
        self._clear_rows()
        for f in self.findings:
            self.rows.addWidget(_finding_row(f))
        self.again.setEnabled(True)
        self.layout().activate()
        self.resize(self.width(), max(self.height(), self.layout().totalHeightForWidth(self.width())))

    def _clear_rows(self) -> None:
        while self.rows.count():
            widget = self.rows.takeAt(0).widget()
            if widget is not None:
                widget.deleteLater()


def _finding_row(f: Finding) -> QWidget:
    row = QFrame()
    row.setObjectName("doctorRow")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(6, 2, 6, 2)
    dot = QLabel("●")
    dot.setStyleSheet(f"color: {COLOURS[f.level]}")
    area = QLabel(f.area)
    area.setFixedWidth(70)
    area.setStyleSheet("color: gray")
    text = QLabel(f.text)
    text.setWordWrap(True)
    text.setMinimumWidth(360)  # so wrapped lines get their full height
    text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    layout.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(area, 0, Qt.AlignmentFlag.AlignTop)
    layout.addWidget(text, 1)
    return row
