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

        return lowend.weak_reasons(lowend.PcFacts(self.threads, self.memory_gb, self.codec))


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
    import platform
    import sys

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or ""


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
        pictures = [av.VideoFrame.from_ndarray(np.stack([base + noise[i % 8]] * 3, axis=-1), format="rgb24")
                    .reformat(format="yuv420p") for i in range(8)]
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


def upload_speed(url: str = UPLOAD_URL, seconds: float = UPLOAD_SECONDS, post=None) -> int | None:
    """Upload speed in kbps: send growing blocks until `seconds` pass, rate of the biggest blocks."""
    import urllib.request

    def default_post(data: bytes) -> None:
        request = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(request, timeout=seconds + 5) as reply:
            reply.read()

    post = post or default_post
    size, deadline, timings = 256 * 1024, time.monotonic() + seconds, []
    try:
        while time.monotonic() < deadline and size <= 64 * 2**20:
            start = time.perf_counter()
            post(bytes(size))
            took = time.perf_counter() - start
            timings.append((size, took))
            if took < 1.5:
                size *= 2
    except Exception as exc:
        log.info("upload test stopped: %s", exc)
    timings = timings[1:] or timings  # the first block also pays for connecting
    if not timings:
        return None
    biggest = sorted(timings, key=lambda t: t[0])[-2:]  # big blocks: connection setup counts least
    sent, took = sum(s for s, _ in biggest), sum(t for _, t in biggest)
    return int(sent * 8 / 1000 / took) if took > 0 else None


def _check_upload(p: PcProfile) -> str:
    """Twitch's bandwidth test when a Twitch key is set (a real stream, never shown), else Cloudflare's."""
    from sprigcast.output import upload_test

    collection = p.extra.get("collection")
    key = upload_test.twitch_key(collection) if collection is not None else None
    if key:
        p.upload_kbps = upload_test.twitch_upload(key)
        if p.upload_kbps:
            capped = p.upload_kbps >= upload_test.CEILING_KBPS * 0.9
            p.extra["upload_source"] = "Twitch"
            mbps = f"{upload_test.CEILING_KBPS / 1000:.0f}+" if capped else f"{p.upload_kbps / 1000:.1f}"
            return f"{mbps} Mbps (Twitch test stream)"
    p.upload_kbps = upload_speed()
    p.extra["upload_source"] = "Cloudflare" if p.upload_kbps else None
    return f"{p.upload_kbps / 1000:.1f} Mbps" if p.upload_kbps else "Couldn't test: type it in on the next page"


for _check in (
    Check("cpu", "CPU", _check_cpu),
    Check("memory", "Memory", _check_memory),
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
    total = 0
    for st in streamers:
        if not st.frames:
            return None
        pilot = getattr(st, "autopilot", None)
        if st.dropped > st.frames * 0.001 or st.reconnects or (pilot is not None and pilot.steps_down):
            return 0
        total += int(st.bitrate_kbps) + audio_kbps
    return total


def remember_proven(settings, value: int | None) -> None:
    if value is None:
        return
    if value:
        settings.setValue(PROVEN, value)
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
    n = len(_stream_outputs(c)) if p.wants_stream else 0
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
            o.fps = None if fps == _canvas_fps(p, c, o.canvas) else fps

        name = service.label if o.service in SERVICES else (o.service or "Stream")
        changes.append(Change("Stream", f"{name}: {_size_text(new_size)}, {fps} fps, {kbps} kbps",
                              f"{_size_text(now_size)}, {now_fps} fps, {o.bitrate_kbps} kbps", apply))
    return changes


def _canvas_fps(p: PcProfile, c: SceneCollection, canvas_id: str) -> int:
    """60 when something on this canvas runs at 60 (or the PC records at 60), else 30."""
    from sprigcast.output.stream import SERVICES

    t = target(p, _encodes(p, c))
    streams = [o for o in _stream_outputs(c) if o.canvas == canvas_id] if p.wants_stream else []
    canvas = next(cv for cv in c.canvases if cv.id == canvas_id)
    budget = _budget(p, c)
    steps = [_best_step(t, SERVICES.get(o.service or "custom", SERVICES["custom"]), canvas, budget) for o in streams]
    if p.wants_record and canvas_id in c.recording.canvases:
        steps.append(t.step)
    if not steps:
        return canvas.fps
    return max(fps for _, fps in steps)


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


Rule = Callable[[PcProfile, SceneCollection], list[Change]]
RULES: list[Rule] = []


def register_rule(rule: Rule) -> None:
    if rule not in RULES:
        RULES.append(rule)


for _rule in (video_rule, stream_rule, recording_rule, encoder_rule):
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
    steps = [s for _, s, _ in streams]
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
