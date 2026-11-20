"""Optimize for this PC: check the PC, then suggest stream and recording settings that fit it.

Two lists drive the wizard (app/wizard.py), and both are open for more entries later:

* ``CHECKS``: each one looks at one thing (CPU, memory, encoder, upload...) and fills in the
  ``PcProfile``. Add a check with ``register_check``; it runs in a background thread, in order.
* ``RULES``: each one reads the profile and the scene collection and returns ``Change`` rows the user
  can tick or untick. Add a rule with ``register_rule``.

Nothing here changes the collection until a Change's ``apply`` runs. Canvas frame rates are the
exception to "right away": like Settings → Video they change at the next start (``Plan.fps``).
"""

from __future__ import annotations

import math
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from sprigcast.core.scene import EncoderSettings, SceneCollection

log = logging.getLogger(__name__)

# Quality steps, best first: (short side, fps). 720p60 comes before 1080p30: smoother motion reads
# better on stream than extra sharpness, and it costs a little less to encode.
LADDER: list[tuple[int, int]] = [(1080, 60), (720, 60), (1080, 30), (720, 30), (480, 30)]
# Bitrate each step needs to look clean on H.264 (roughly what Twitch and OBS suggest).
STEP_KBPS = {(1080, 60): 6000, (720, 60): 4500, (1080, 30): 4500, (720, 30): 3000, (480, 30): 1500}
UPLOAD_SHARE = 0.75  # use at most this share of the measured upload for video, the rest is headroom
CPU_BUDGET = 0.45  # x264 may use this share of the whole CPU (games need the rest) ...
CPU_BUDGET_WEAK = 0.35  # ... less on a PC with little to spare
STRONG_PRESET = "p5"
RECORDING_KBPS = {1080: 12000, 720: 8000, 480: 4000}
DONE = "general/optimized"  # QSettings: the wizard was offered at first start


@dataclass
class PcProfile:
    """What the checks found. None means "not checked" or "couldn't tell"."""

    threads: int = 0
    memory_gb: float = 0.0
    cpu_name: str = ""
    codec: str = "libx264"
    x264_ms_720: float | None = None  # one 1280x720 frame on x264 veryfast, all threads (CPU encoding only)
    upload_kbps: int | None = None
    wants_stream: bool = True
    wants_record: bool = True
    extra: dict = field(default_factory=dict)  # room for later checks

    @property
    def hardware(self) -> bool:
        from sprigcast.output.recorder import is_hardware

        return is_hardware(self.codec)

    @property
    def weak_reasons(self) -> list[str]:
        from sprigcast.app import lowend

        return lowend.weak_reasons(self.facts)

    @property
    def facts(self):
        from sprigcast.app import lowend

        return lowend.PcFacts(self.threads, self.memory_gb, self.codec, self.cpu_name, tuple(self.extra.get("gpus", ())))

    @property
    def older(self) -> bool:
        """A PC from about 2015 or before, like a 2013 all-in-one: older CPU or older built-in graphics."""
        from sprigcast.app import lowend

        return bool(lowend.older_reasons(self.facts))


@dataclass
class Change:
    group: str  # "Video", "Stream", "Recording", "Encoder"
    label: str  # what it becomes, short: "Twitch: 1920×1080, 60 fps, 6000 kbps"
    now: str  # what it is now
    apply: Callable[[], None]
    fps: dict[str, int] = field(default_factory=dict)  # canvas frame rates queued for the next start


@dataclass
class Plan:
    headline: str
    changes: list[Change]
    notes: list[str] = field(default_factory=list)
    quality: tuple[int, int] = LADDER[0]  # the one quality every stream and recording follows
    recommended: tuple[int, int] = LADDER[0]  # what fits this PC and upload
    limits: list[str] = field(default_factory=list)  # streams a site holds below the quality: "TikTok: 720p · 30 fps"
    recording: tuple[int, int] = LADDER[0]  # recordings use no upload, so they can sit above the stream quality

    def apply(self, chosen: list[Change] | None = None) -> dict[str, int]:
        """Apply the chosen changes (all by default); returns canvas frame rates to queue for the next start."""
        fps: dict[str, int] = {}
        for change in self.changes if chosen is None else chosen:
            change.apply()
            fps.update(change.fps)
        return fps


# Checks -------------------------------------------------------------------------------


@dataclass
class Check:
    key: str
    label: str  # shown while it runs: "Testing upload"
    run: Callable[[PcProfile], str]  # fills in the profile, returns a short result: "16 threads"
    when: Callable[[PcProfile], bool] = lambda p: True
    background: bool = True  # False: must run on the UI thread (e.g. touches the GPU encoder cache)


CHECKS: list[Check] = []


def register_check(check: Check, before: str | None = None) -> None:
    keys = [c.key for c in CHECKS]
    if check.key in keys:
        CHECKS[keys.index(check.key)] = check
    elif before in keys:
        CHECKS.insert(keys.index(before), check)
    else:
        CHECKS.append(check)


def _cpu_name() -> str:
    from sprigcast.app.lowend import cpu_name

    return cpu_name()


def _check_cpu(p: PcProfile) -> str:
    import os

    p.threads = os.cpu_count() or 1
    p.cpu_name = _cpu_name()
    threads = f"{p.threads} thread{'s' if p.threads != 1 else ''}"
    return f"{p.cpu_name}, {threads}" if p.cpu_name else threads


def _check_memory(p: PcProfile) -> str:
    try:
        import psutil

        p.memory_gb = psutil.virtual_memory().total / 2**30
    except Exception:
        return "Couldn't tell"
    return f"{round(p.memory_gb)} GB"


def _check_graphics(p: PcProfile) -> str:
    from sprigcast.app.lowend import gpu_names, short_gpu

    try:
        p.extra["gpus"] = gpu_names()
    except Exception:
        p.extra["gpus"] = ()
    return ", ".join(short_gpu(g) for g in p.extra["gpus"]) or "Couldn't tell"


def _check_encoder(p: PcProfile) -> str:
    from sprigcast.output.recorder import codec_label, pick_codec

    p.codec = pick_codec()
    return codec_label(p.codec)


def x264_speed(frames: int = 40, width: int = 1280, height: int = 720) -> float | None:
    """Milliseconds x264 (veryfast) takes per frame here, using every thread like a real encode."""
    from fractions import Fraction

    import av
    import numpy as np

    try:
        ctx = av.CodecContext.create("libx264", "w")
        ctx.width, ctx.height, ctx.pix_fmt = width, height, "yuv420p"
        ctx.time_base = Fraction(1, 30)
        ctx.bit_rate = 4_500_000
        ctx.options = {"preset": "veryfast", "tune": "zerolatency"}
        ctx.open()
        # Moving noise over a gradient: about as hard as a busy game, unlike a flat test picture.
        rng = np.random.default_rng(1)
        base = np.add.outer(np.arange(height) // 3, np.arange(width) // 5).astype(np.uint8)
        noise = rng.integers(0, 40, (8, height, width), dtype=np.uint8)
        from sprigcast.core.frames import reformat

        pictures = [reformat(av.VideoFrame.from_ndarray(np.stack([base + noise[i % 8]] * 3, axis=-1), format="rgb24"),
                             format="yuv420p") for i in range(8)]
        for i in range(5):  # warm up: the first frames pay for setting up threads
            ctx.encode(pictures[i % 8])
        start = time.perf_counter()
        for i in range(frames):
            pictures[i % 8].pts = i + 5
            ctx.encode(pictures[i % 8])
        ctx.encode(None)
        return (time.perf_counter() - start) * 1000 / frames
    except Exception as exc:
        log.info("x264 speed test failed: %s", exc)
        return None


def _check_x264(p: PcProfile) -> str:
    p.x264_ms_720 = x264_speed()
    if p.x264_ms_720 is None:
        return "Couldn't test"
    return f"{p.x264_ms_720:.1f} ms a 720p frame"


UPLOAD_URL = "https://speed.cloudflare.com/__up"  # Cloudflare's public speed test: takes bytes, keeps nothing
UPLOAD_SECONDS = 8.0
# Python's own "Python-urllib" name is refused by the test site (HTTP 403 on every connection, 2026-11-19): the
# test says who it is. A second shape, the one the site's own page sends, is tried when the first is refused.
UPLOAD_HEADERS = (
    {"User-Agent": "SprigCast upload test", "Accept": "*/*", "Content-Type": "application/octet-stream"},
    {"User-Agent": "SprigCast upload test", "Accept": "*/*", "Content-Type": "text/plain;charset=UTF-8",
     "Origin": "https://speed.cloudflare.com", "Referer": "https://speed.cloudflare.com/"},
    {"User-Agent": "Mozilla/5.0 (compatible; SprigCast upload test)", "Accept": "*/*",
     "Content-Type": "text/plain;charset=UTF-8",
     "Origin": "https://speed.cloudflare.com", "Referer": "https://speed.cloudflare.com/"},
)
UPLOAD_WARMUP_S = 1.5  # connecting and each connection's slow start don't count
UPLOAD_CONNECTIONS = 4  # at once, like streaming to several places: one connection alone reads the line low
UPLOAD_BODY = 24 * 2**20  # one request's size: longer than the test on most lines, so it's cut off, not awaited
UPLOAD_BUFFER = 256 * 1024  # each connection's send buffer: enough for 100+ Mbps over four, small next to the test
UPLOAD_PIECE = 32 * 1024  # handed to the connection at a time; each piece is counted as it goes


def _upload_shape(url: str, timeout: float = 6.0) -> dict | None:
    """The request shape the test site takes, found with one tiny request each. None when it can't be reached."""
    import urllib.error
    import urllib.request

    for headers in UPLOAD_HEADERS:
        request = urllib.request.Request(url, data=b"0", method="POST", headers=dict(headers))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                reply.read()
            return dict(headers)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                continue  # refused as it is: the next shape
            log.info("upload test stopped: %s", exc)
            return None
        except Exception as exc:
            log.info("upload test stopped: %s", exc)
            return None
    log.info("upload test stopped: the test site refused every request shape")
    return None


def _upload_sender(url: str, headers: dict):
    """Sends request after request on one connection until the deadline, counting each piece as it's handed
    over. A request still under way at the deadline is dropped: what counts is what left, not what was
    answered (waiting for each answer read 15 Mbps on a line that carries 23, PC check 2026-11-19)."""
    import http.client
    import socket
    import ssl
    import urllib.parse

    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    piece = bytes(UPLOAD_PIECE)

    def send(deadline: float, count) -> None:
        while time.monotonic() < deadline:
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(parts.netloc, timeout=5, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(parts.netloc, timeout=5)
            try:
                conn.connect()
                # A fixed, small send buffer: one that grows during the test would count as speed.
                conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, UPLOAD_BUFFER)
                conn.putrequest("POST", path)
                for name, value in headers.items():
                    conn.putheader(name, value)
                conn.putheader("Content-Length", str(UPLOAD_BODY))
                conn.endheaders()
                left = UPLOAD_BODY
                while left > 0 and time.monotonic() < deadline:
                    conn.send(piece[:left])
                    count(min(left, len(piece)))
                    left -= len(piece)
                if left > 0:
                    return  # time's up mid-request: dropped, never awaited
                try:
                    conn.getresponse().read()  # a fast line finished one: its answer, then the next
                except OSError:
                    pass  # a slow answer takes nothing away from what was sent
            finally:
                conn.close()

    return send


def upload_speed(url: str = UPLOAD_URL, seconds: float = UPLOAD_SECONDS, sender=None,
                 connections: int = UPLOAD_CONNECTIONS) -> int | None:
    """Upload speed in kbps: a few connections send at once for `seconds`, every piece counted as it leaves.

    What the line carries is every connection's bytes together over the time after the warm-up. One
    connection alone often tops out below the line (11.4 Mbps measured on a line that had carried three
    streams of 23 Mbps for an hour, 2026-11-19), and several streams are several connections.
    `sender(deadline, count)` sends until the deadline and calls count(bytes) for each piece.
    """
    import threading

    if sender is None:
        headers = _upload_shape(url)
        if headers is None:
            return None
        sender = _upload_sender(url, headers)
    began = time.monotonic()
    deadline = began + seconds
    pieces: list[tuple[float, int]] = []  # when, bytes (list.append is safe across threads)

    def count(size: int) -> None:
        pieces.append((time.monotonic(), size))

    def send() -> None:
        try:
            sender(deadline, count)
        except Exception as exc:
            log.info("upload test stopped: %s", exc)

    senders = [threading.Thread(target=send, name="upload-test", daemon=True) for _ in range(max(1, connections))]
    for one in senders:
        one.start()
    for one in senders:
        one.join(seconds + 15)
    counted = list(pieces)
    if not counted:
        return None
    # The steady part: after the warm-up (connecting, slow start, the send buffer filling), up to the last piece.
    first = min(when for when, _ in counted)
    last = min(max(when for when, _ in counted), deadline + 1.0)
    window_start = first + min(UPLOAD_WARMUP_S, seconds / 4)
    if last - window_start < min(1.0, seconds / 4):  # hardly anything after the warm-up: all of it counts
        window_start = began
    sent = sum(size for when, size in counted if window_start < when <= last)
    took = last - window_start
    return int(sent * 8 / 1000 / took) if took > 0 and sent else None


MEASURED = "upload/measured_kbps"  # QSettings: the upload Optimize's test measured (the Stream doctor checks with it)


def remember_measured(kbps: int | None) -> None:
    if kbps:
        from PySide6.QtCore import QSettings

        QSettings().setValue(MEASURED, int(kbps))  # QSettings is safe on any thread with its own instance


def _check_upload(p: PcProfile) -> str:
    """Twitch's bandwidth test when a Twitch key is set (a real stream, never shown), else Cloudflare's."""
    text = _measure_upload(p)
    remember_measured(p.upload_kbps)
    return text


def _measure_upload(p: PcProfile) -> str:
    from sprigcast.output import upload_test

    collection = p.extra.get("collection")
    key = upload_test.twitch_key(collection) if collection is not None else None
    twitch = upload_test.twitch_upload(key) if key else None
    if twitch and twitch >= upload_test.CEILING_KBPS * 0.9:  # as fast as the test sends: plenty
        p.upload_kbps = twitch
        p.extra["upload_source"] = "Twitch"
        return f"{upload_test.CEILING_KBPS / 1000:.0f}+ Mbps (Twitch test stream)"
    # Below that, the test stream may have found what one connection to Twitch carries, not the line: the
    # places to stream each get their own connection, so the line is measured too and the higher one counts.
    line = upload_speed()
    if twitch and twitch >= (line or 0):
        p.upload_kbps = twitch
        p.extra["upload_source"] = "Twitch"
        return f"{twitch / 1000:.1f} Mbps (Twitch test stream)"
    p.upload_kbps = line
    p.extra["upload_source"] = "Cloudflare" if line else None
    return f"{line / 1000:.1f} Mbps" if line else "Couldn't test: type it in on the next page"


for _check in (
    Check("cpu", "CPU", _check_cpu),
    Check("memory", "Memory", _check_memory),
    Check("graphics", "Graphics", _check_graphics),
    Check("encoder", "Video encoder", _check_encoder, background=False),
    Check("x264", "CPU encoding speed", _check_x264, when=lambda p: not p.hardware),
    Check("upload", "Upload speed", _check_upload, when=lambda p: p.wants_stream and p.extra.get("test_upload", True)),
):
    register_check(_check)


# Rules ------------------------------------------------------------------------------------


@dataclass
class Target:
    """The quality the PC can carry: the best ladder step, the encoder preset and whether it's a light setup."""

    step: tuple[int, int]
    preset: str
    light: bool


def _cpu_cost(ms_720: float, step: tuple[int, int]) -> float:
    """Share of the whole CPU one x264 encode at this step takes."""
    short, fps = step
    return ms_720 * (short / 720) ** 2 * fps / 1000


PROVEN = "upload/proven_kbps"  # QSettings: the upload (bitrates + audio) the last clean stream carried
PROVEN_MIN_S = 60  # a stream must run this long, untroubled, to count


def proven_kbps(streamers, audio_kbps: int, seconds: float) -> int | None:
    """What a finished stream proved the upload carries: the sum of its bitrates when every destination ran
    clean (no drops, no reconnects, auto-pilot never stepped down). 0 when the network struggled, None when
    it proves nothing either way (too short, or nothing connected)."""
    if seconds < PROVEN_MIN_S or not streamers:
        return None
    sent = [st for st in streamers if st.frames]  # a place that never connected proves nothing, the others still do
    if not sent:
        return None
    total = 0
    for st in sent:
        pilot = getattr(st, "autopilot", None)
        if st.dropped > st.frames * 0.001 or st.reconnects or (pilot is not None and pilot.steps_down):
            return 0
        total += int(st.bitrate_kbps) + audio_kbps
    return total


def remember_proven(settings, value: int | None) -> None:
    """Keep the most a clean stream has carried: a lighter clean stream proves nothing new, trouble forgets it."""
    if value is None:
        return
    if value:
        settings.setValue(PROVEN, max(value, load_proven(settings)))
    else:
        settings.remove(PROVEN)


def load_proven(settings) -> int:
    try:
        return int(settings.value(PROVEN, 0) or 0)
    except (TypeError, ValueError):
        return 0


def usable_upload(p: PcProfile) -> int | None:
    """The upload to plan with: the test's result, or more when a past stream carried more without trouble.

    A clean stream of X kbps proves X fits inside the UPLOAD_SHARE headroom, so it counts as X / UPLOAD_SHARE.
    """
    proven = int(p.extra.get("proven_kbps") or 0)
    if not p.upload_kbps and not proven:
        return None
    return max(p.upload_kbps or 0, math.ceil(proven / UPLOAD_SHARE))

QUALITY = "quality"  # PcProfile.extra key: the quality picked by hand in the wizard, overriding the suggestion


def target(p: PcProfile, encodes: int = 1) -> Target:
    """The quality to use: picked by hand (PcProfile.extra["quality"]), else what the PC can carry."""
    t = pc_target(p, encodes)
    chosen = p.extra.get(QUALITY)
    return Target(tuple(chosen), t.preset, t.light) if chosen else t


def pc_target(p: PcProfile, encodes: int = 1) -> Target:
    """What one PC can carry for `encodes` encodes at once (streams plus recordings)."""
    from sprigcast.app.lowend import CPU_PRESET, LIGHT_PRESET

    weak = bool(p.weak_reasons)
    encodes = max(1, encodes)
    if p.hardware:
        return Target((720, 30), LIGHT_PRESET, True) if weak else Target(LADDER[0], STRONG_PRESET, False)
    if p.x264_ms_720 is None:
        return Target((720, 30), CPU_PRESET, True)
    budget = CPU_BUDGET_WEAK if weak else CPU_BUDGET
    preset = CPU_PRESET if weak else "p3"  # superfast or veryfast
    speed = p.x264_ms_720 * (0.7 if weak else 1.0)  # superfast is about a third less work than veryfast
    for step in LADDER:
        if _cpu_cost(speed, step) * encodes <= budget:
            return Target(step, preset, weak or step[0] < 1080 or step[1] < 60)
    return Target(LADDER[-1], CPU_PRESET, True)


def _size_text(size: tuple[int, int]) -> str:
    return f"{size[0]}×{size[1]}"


def _stream_outputs(c: SceneCollection) -> list:
    streams = [o for o in c.outputs if o.type == "stream" and not o.enhanced]
    enabled = [o for o in streams if o.enabled]
    return enabled or streams


def _encodes(p: PcProfile, c: SceneCollection) -> int:
    """Encoders running at once. Places on one canvas that a site doesn't hold lower share one encoder
    (output/shared_stream.py), so Twitch, YouTube and Kick together are one, not three."""
    from sprigcast.output.stream import SERVICES

    n = 0
    if p.wants_stream:
        kinds = set()
        for o in _stream_outputs(c):
            service = SERVICES.get(o.service or "custom", SERVICES["custom"])
            kinds.add((o.canvas, service.fps, min(service.long_side, 1920)))
        n = len(kinds)
    if p.wants_record:
        n += sum(1 for cv in c.canvases if cv.enabled and cv.id in c.recording.canvases)
    return n


def _budget(p: PcProfile, c: SceneCollection, honour_choice: bool = True) -> float:
    """Upload per stream for video, in kbps. Unlimited when untested, or when a quality was picked by hand."""
    outputs = _stream_outputs(c)
    upload = usable_upload(p)
    if not upload or not outputs or (honour_choice and p.extra.get(QUALITY)):
        return float("inf")
    return upload * UPLOAD_SHARE / len(outputs) - c.stream_encoder.audio_kbps


class _Canvas1080:
    width, height = 1920, 1080


def recommended_step(p: PcProfile, c: SceneCollection) -> tuple[int, int]:
    """The one quality that fits this PC and upload, before any site's own limit."""
    from sprigcast.output.stream import SERVICES

    t = pc_target(p, _encodes(p, c))
    budget = _budget(p, c, honour_choice=False) if p.wants_stream else float("inf")
    return _best_step(t, SERVICES["custom"], _Canvas1080, budget)


def pc_step(p: PcProfile, c: SceneCollection) -> tuple[int, int]:
    """What this PC could carry with all the upload in the world."""
    return pc_target(p, _encodes(p, c)).step


def upload_is_the_limit(p: PcProfile, c: SceneCollection) -> bool:
    """The suggestion sits below what the PC can do because of the upload, not the PC."""
    return p.wants_stream and LADDER.index(recommended_step(p, c)) > LADDER.index(pc_step(p, c))


def upload_note(p: PcProfile, c: SceneCollection) -> str:
    """With the upload as the limit: what it carries in all, shared by how many places, and what fewer
    places would get. The sites' own numbers (Twitch 6000 kbps at 1080p60, 4500 at 720p60, 3000 at 720p30;
    YouTube at least 6000, 5000 and 3000; Kick up to 8000) each take their own share of the upload, and the
    usual advice is to send no more than 70 to 80% of it."""
    from sprigcast.output.stream import SERVICES

    places = len(_stream_outputs(c))
    upload = usable_upload(p)
    if not upload or not places or not upload_is_the_limit(p, c):
        return ""
    video = int(upload * UPLOAD_SHARE)
    t = pc_target(p, _encodes(p, c))
    text = f"Your upload ({upload / 1000:.1f} Mbps) is the limit, not your PC: it safely carries about {video} kbps"
    if places == 1:
        return text + "."
    text += f", shared by {places} places."
    for fewer in range(places - 1, 0, -1):
        step = _best_step(t, SERVICES["custom"], _Canvas1080, video / fewer - c.stream_encoder.audio_kbps)
        if LADDER.index(step) < LADDER.index(recommended_step(p, c)):
            what = "One place alone" if fewer == 1 else f"{fewer} places"
            text += f" {what} would get {step_text(step)}."
            if step == pc_step(p, c):
                break
    return text


def _best_step(t: Target, service, canvas, budget_kbps: float) -> tuple[int, int]:
    service_short = min(service.long_side * 9 // 16, min(canvas.width, canvas.height))
    for step in LADDER:
        if LADDER.index(step) < LADDER.index(t.step):
            continue  # more than the PC can carry
        if step[1] > service.fps or (step[0] > service_short and step[0] > 480):
            continue  # more than the service takes (TikTok: 720p30)
        if STEP_KBPS[step] * 0.65 > min(budget_kbps, service.max_kbps):
            continue  # more than the upload carries
        return step
    return LADDER[-1]


def stream_rule(p: PcProfile, c: SceneCollection) -> list[Change]:
    from sprigcast.output.recorder import shrink_to
    from sprigcast.output.stream import SERVICES

    if not p.wants_stream:
        return []
    outputs = _stream_outputs(c)
    if not outputs:
        return []
    t = target(p, _encodes(p, c))
    canvases = {cv.id: cv for cv in c.canvases}
    changes = []
    for o, step, kbps in _plan_streams(p, c, t, _budget(p, c)):
        canvas = canvases[o.canvas]
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        size = shrink_to(canvas.width, canvas.height, step[0])
        fps = step[1]
        now_size = o.size or (canvas.width, canvas.height)
        new_size = size or (canvas.width, canvas.height)
        now_fps = o.fps or p.extra.get("waiting_fps", {}).get(canvas.id, canvas.fps)
        if (now_size, now_fps, o.bitrate_kbps) == (new_size, fps, kbps) and o.auto_bitrate:
            continue

        def apply(o=o, size=size, fps=fps, kbps=kbps):
            o.size, o.bitrate_kbps, o.auto_bitrate = size, kbps, True
            # Always the place's own: "same as the canvas" drifted when the canvas change queued with it was
            # lost or another garden opened, 720p at 60 fps on 2600 kbps (PC, 2026-11-19). Never above the canvas.
            o.fps = fps

        name = service.label if o.service in SERVICES else (o.service or "Stream")
        changes.append(Change("Stream", f"{name}: {_size_text(new_size)}, {fps} fps, {kbps} kbps",
                              f"{_size_text(now_size)}, {now_fps} fps, {o.bitrate_kbps} kbps", apply))
    return changes


def _canvas_needs(p: PcProfile, c: SceneCollection, canvas) -> int | None:
    """The frame rate this canvas's own streams and recordings ask for; None when it has neither."""
    from sprigcast.output.stream import SERVICES

    t = target(p, _encodes(p, c))
    streams = [o for o in _stream_outputs(c) if o.canvas == canvas.id] if p.wants_stream else []
    budget = _budget(p, c)
    steps = [_best_step(t, SERVICES.get(o.service or "custom", SERVICES["custom"]), canvas, budget) for o in streams]
    if p.wants_record and canvas.id in c.recording.canvases:
        steps.append(t.step)
    return max(fps for _, fps in steps) if steps else None


def _canvas_fps(p: PcProfile, c: SceneCollection, canvas_id: str) -> int:
    """One frame rate for every canvas that's on: 60 when anything streams or records at 60, else 30.

    Never one canvas at 30 and the other at 60 (a canvas with nothing to send kept its own 60 next to a
    horizontal one set to 30, 2026-11-19): a part shown on both would be drawn at two rates.
    """
    needs = [n for n in (_canvas_needs(p, c, cv) for cv in c.canvases if cv.enabled) if n]
    if needs:
        return max(needs)
    return next(cv for cv in c.canvases if cv.id == canvas_id).fps


def video_rule(p: PcProfile, c: SceneCollection, waiting: dict[str, int] | None = None) -> list[Change]:
    waiting = waiting or {}
    changes = []
    for canvas in c.canvases:
        if not canvas.enabled:
            continue
        fps = _canvas_fps(p, c, canvas.id)
        now = waiting.get(canvas.id, canvas.fps)
        if fps != now:
            changes.append(Change("Video", f"{canvas.id.capitalize()} canvas: {fps} fps (after a restart)",
                                  f"{now} fps", lambda: None, fps={canvas.id: fps}))
    return changes


def recording_rule(p: PcProfile, c: SceneCollection) -> list[Change]:
    from sprigcast.app.lowend import LIGHT_RECORDING_KBPS

    if not p.wants_record:
        return []
    t = target(p, _encodes(p, c))
    r = c.recording
    recorded = [cv for cv in c.canvases if cv.enabled and cv.id in r.canvases]
    if not recorded:
        return []
    canvas_short = max(min(cv.width, cv.height) for cv in recorded)
    short = None if t.step[0] >= canvas_short else t.step[0]
    shown_short = short or canvas_short
    kbps = LIGHT_RECORDING_KBPS if t.light else RECORDING_KBPS.get(min(shown_short, 1080), 12000)
    if short == r.short_side and kbps == r.bitrate_kbps:
        return []

    def apply():
        r.short_side, r.bitrate_kbps = short, kbps

    now = f"{r.short_side or canvas_short}p, {r.bitrate_kbps} kbps"
    return [Change("Recording", f"Recordings and replays: {shown_short}p, {kbps} kbps", now, apply)]


def _encoder_wanted(p: PcProfile, t: Target, enc: EncoderSettings) -> dict:
    wanted: dict = {"b_frames": 0, "lookahead": False}  # SprigCast's own lighter NVENC path needs both off
    if t.light:
        wanted["multipass"] = "disabled"
    wanted["preset"] = t.preset  # on x264: p2 superfast, p3 veryfast
    return {k: v for k, v in wanted.items() if getattr(enc, k) != v}


def encoder_rule(p: PcProfile, c: SceneCollection) -> list[Change]:
    from sprigcast.output.recorder import X264_PRESETS, codec_short

    t = target(p, _encodes(p, c))
    names = {"p1": "fastest", "p2": "faster", "p3": "fast", "p4": "medium", "p5": "quality",
             "p6": "higher quality", "p7": "best quality"}

    def preset_name(preset: str) -> str:
        return X264_PRESETS[preset] if not p.hardware else names[preset]

    changes = []
    for wanted_for, enc, on in (("Stream", c.stream_encoder, p.wants_stream),
                                ("Recording", c.recording.encoder, p.wants_record)):
        wanted = _encoder_wanted(p, t, enc) if on else {}
        if not wanted:
            continue
        bits = [f"{preset_name(t.preset)} preset"]
        if "b_frames" in wanted or "lookahead" in wanted:
            bits.append("no B-frames or look-ahead")
        if "multipass" in wanted:
            bits.append("no multipass")

        def apply(enc=enc, wanted=wanted):
            for k, v in wanted.items():
                setattr(enc, k, v)

        changes.append(Change("Encoder", f"{wanted_for} encoder ({codec_short(p.codec)}): {', '.join(bits)}",
                              f"{preset_name(enc.preset)} preset, {enc.b_frames} B-frames", apply))
    return changes


def recording_drive_is_hdd(p: PcProfile, c: SceneCollection) -> bool:
    """Whether recordings land on a spinning hard drive (asked once, kept in PcProfile.extra)."""
    from sprigcast.app import lowend

    if "hdd" not in p.extra:
        try:
            p.extra["hdd"] = lowend.on_hard_drive(lowend.recordings_folder(c))
        except Exception:
            p.extra["hdd"] = None
    return bool(p.extra["hdd"])


def older_rule(p: PcProfile, c: SceneCollection) -> list[Change]:
    """An older PC (a 2013 all-in-one, say): one canvas, clips only when pressed, lighter browser
    sources; recordings on a hard drive: one MKV file."""
    from sprigcast.app import lowend

    hdd = p.wants_record and recording_drive_is_hdd(p, c)
    if not p.older and not hdd:
        return []
    return [Change(group, line, now, change)
            for group, line, now, change in lowend.older_changes(c, hdd=hdd, older=p.older)]


Rule = Callable[[PcProfile, SceneCollection], list[Change]]
RULES: list[Rule] = []


def register_rule(rule: Rule) -> None:
    if rule not in RULES:
        RULES.append(rule)


for _rule in (video_rule, stream_rule, recording_rule, encoder_rule, older_rule):
    register_rule(_rule)


def headline(p: PcProfile, c: SceneCollection) -> str:
    from sprigcast.output.recorder import codec_short

    short, fps = recommended_step(p, c)
    what = f"{short}p at {fps} fps on {codec_short(p.codec)}"
    if p.weak_reasons:
        return f"This PC is on the light side ({', '.join(p.weak_reasons)}). Best fit: {what}."
    return f"This PC can handle {what}."


def plan(p: PcProfile, c: SceneCollection, waiting_fps: dict[str, int] | None = None) -> Plan:
    p.extra["waiting_fps"] = waiting_fps or {}  # canvas frame rates already queued for the next start
    changes: list[Change] = []
    for rule in RULES:
        try:
            changes += video_rule(p, c, waiting_fps) if rule is video_rule else rule(p, c)
        except Exception:
            log.exception("optimize rule %s failed", getattr(rule, "__name__", rule))
    notes = []
    limit = upload_note(p, c) if not p.extra.get(QUALITY) else ""
    if limit:
        notes.append(limit)
    if p.wants_stream and not p.upload_kbps:
        notes.append("Upload not tested: stream bitrates use each site's usual value. Auto bitrate lowers it if needed.")
    if p.wants_stream and not _stream_outputs(c):
        notes.append("No stream set up yet. Add one in Settings → Stream, then run this again.")
    if any(o.enhanced for o in c.outputs if o.type == "stream"):
        notes.append("Enhanced Broadcasting streams are left alone: Twitch picks their settings.")
    recommended = recommended_step(p, c)
    quality = tuple(p.extra.get(QUALITY) or recommended)
    if LADDER.index(quality) < LADDER.index(recommended):
        notes.insert(0, "That's more than this PC or upload suits: watch for dropped frames.")
    return Plan(headline(p, c), changes, notes, quality, recommended, site_limits(p, c, quality),
                target(p, _encodes(p, c)).step)


def step_text(step: tuple[int, int]) -> str:
    return f"{step[0]}p · {step[1]} fps"


def site_limits(p: PcProfile, c: SceneCollection, quality: tuple[int, int]) -> list[str]:
    """Streams that a site keeps below the chosen quality, e.g. "TikTok: 720p · 30 fps (its limit)"."""
    from sprigcast.output.stream import SERVICES

    if not p.wants_stream:
        return []
    canvases = {cv.id: cv for cv in c.canvases}
    t = target(p, _encodes(p, c))
    lines = []
    for o in _stream_outputs(c):
        if o.canvas not in canvases:
            continue
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        step = _best_step(t, service, canvases[o.canvas], _budget(p, c))
        if step != quality:
            lines.append(f"{service.label}: {step_text(step)} (its limit)")
    return list(dict.fromkeys(lines))


# Stages and room -------------------------------------------------------------------------------

# Quality steps by name, lightest first, for the wizard's Seed-to-Bloom row.
STAGES: list[tuple[str, tuple[int, int]]] = [
    ("Seed", (480, 30)), ("Sprout", (720, 30)), ("Leaf", (1080, 30)), ("Sprig", (720, 60)), ("Bloom", (1080, 60)),
]
STAGE_NAME = {step: name for name, step in STAGES}
# Graphics-card encoders: how many pixels a second count as "full", leaving room for a game. An estimate
# from the soak tests (stream, recording and replay at 1080p60 on NVENC used a small share of the chip).
_PX_1080P60 = 1920 * 1080 * 60
_PX_720P30 = 1280 * 720 * 30
HW_ROOM = 6 * _PX_1080P60
HW_ROOM_WEAK = 3 * _PX_720P30


def _px(step: tuple[int, int]) -> float:
    short, fps = step
    return short * short * 16 / 9 * fps


def _plan_streams(p: PcProfile, c: SceneCollection, t: Target, budget: float) -> list[tuple[object, tuple[int, int], int]]:
    """Each stream's (output, step, kbps) for target `t` within `budget` kbps of video per stream.

    Streams on the same canvas at the same step get one bitrate, the lowest of theirs, so they share one
    encoder (output/shared_stream.py): YouTube follows Twitch's 6000 instead of costing a second encode at 9000.
    """
    from sprigcast.output.stream import SERVICES

    canvases = {cv.id: cv for cv in c.canvases}
    optimize_top = Target(LADDER[0], t.preset, False)
    out = []
    for o in _stream_outputs(c):
        canvas = canvases.get(o.canvas)
        if canvas is None:
            continue
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        step = _best_step(t, service, canvas, budget)
        ideal = STEP_KBPS[step]
        if step == _best_step(optimize_top, service, canvas, float("inf")):
            ideal = max(ideal, service.kbps)  # the service's own best: its usual bitrate (YouTube 9000)
        kbps = max(800, int(min(ideal, budget, service.max_kbps) // 100 * 100))
        out.append((o, step, kbps))
    shared: dict[tuple, int] = {}
    for o, step, kbps in out:
        if not getattr(o, "enhanced", False):
            key = (o.canvas, step)
            shared[key] = min(kbps, shared.get(key, kbps))
    return [(o, step, shared.get((o.canvas, step), kbps)) for o, step, kbps in out]


def stream_steps(p: PcProfile, c: SceneCollection, step: tuple[int, int],
                 budget: float = float("inf")) -> list[tuple[object, tuple[int, int], int]]:
    """Each stream at `step` (or its site's limit): (output, its step, kbps), the same as Apply would set."""
    if not p.wants_stream:
        return []
    return _plan_streams(p, c, Target(step, "p5", False), budget)


def room(p: PcProfile, c: SceneCollection, step: tuple[int, int]) -> tuple[int, int | None]:
    """How full this quality makes the PC and the upload, in percent (upload None when untested).

    100% on the PC means the most SprigCast should take next to a game: on x264 the CPU share the
    wizard allows, on a graphics-card encoder an estimate of its pixel rate.
    """
    streams = stream_steps(p, c, step, _budget(p, c))
    # Places on one canvas at one step and bitrate share an encoder: they cost the PC once (the gauge read
    # three places as three encoders, 83% on a PC that was carrying them at a few percent, 2026-11-19).
    steps = [s for _, s, _ in {(o.canvas, s, k): (o, s, k) for o, s, k in streams}.values()]
    if p.wants_record:
        steps += [step] * sum(1 for cv in c.canvases if cv.enabled and cv.id in c.recording.canvases)
    if p.hardware:
        full = HW_ROOM_WEAK if p.weak_reasons else HW_ROOM
        pc = sum(_px(s) for s in steps) / full
    else:
        weak = bool(p.weak_reasons)
        ms = (p.x264_ms_720 or 12.0) * (0.7 if weak else 1.0)
        pc = sum(_cpu_cost(ms, s) for s in steps) / (CPU_BUDGET_WEAK if weak else CPU_BUDGET)
    upload = None
    usable = usable_upload(p)
    if usable and p.wants_stream:
        sent = sum(k + c.stream_encoder.audio_kbps for _, _, k in streams)
        upload = round(100 * sent / usable)
    return round(100 * pc), upload
