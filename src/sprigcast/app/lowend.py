"""Light settings for a slower PC, and noticing when this is one.

A bare-minimum PC (four CPU threads, 8 GB of memory, or no graphics-card encoder) streams well at
720p and 30 fps with a fast encoder preset; it struggles at 1080p60. ``light_changes`` lists what
differs from that and can apply it. Canvas sizes stay as they are, so no scene has to be laid out
again: streams are scaled down as they are encoded, and only the frame rate changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sprigcast.core.scene import EncoderSettings, SceneCollection

LIGHT_FPS = 30
LIGHT_SHORT_SIDE = 720  # 1280x720 for a horizontal canvas, 720x1280 for a vertical one
LIGHT_STREAM_KBPS = 4000
LIGHT_RECORDING_KBPS = 8000
LIGHT_PRESET = "p3"
CPU_PRESET = "p2"  # x264 on the CPU: superfast, a third less work than veryfast


@dataclass
class PcFacts:
    threads: int
    memory_gb: float
    codec: str


def pc_facts() -> PcFacts:
    import os

    from sprigcast.output.recorder import pick_codec

    try:
        import psutil

        memory_gb = psutil.virtual_memory().total / 2**30
    except Exception:
        memory_gb = 0.0  # unknown: not counted against the PC
    return PcFacts(os.cpu_count() or 1, memory_gb, pick_codec())


def weak_reasons(facts: PcFacts) -> list[str]:
    """Why this PC should use light settings; empty when it has room to spare."""
    reasons = []
    if facts.threads <= 4:
        reasons.append(f"{facts.threads} CPU thread{'s' if facts.threads != 1 else ''}")
    if 0 < facts.memory_gb < 9:
        reasons.append(f"{round(facts.memory_gb)} GB of memory")
    if facts.codec == "libx264":
        reasons.append("no graphics-card encoder, so the CPU encodes")
    return reasons


def light_size(width: int, height: int) -> tuple[int, int] | None:
    """The size to encode at, or None when it is already 720p or smaller."""
    from sprigcast.output.recorder import shrink_to

    return shrink_to(width, height, LIGHT_SHORT_SIDE)


def _light_encoder(enc: EncoderSettings, apply: bool, preset: str = LIGHT_PRESET) -> bool:
    wanted = {"multipass": "disabled", "lookahead": False, "b_frames": 0}
    if int(enc.preset[1:]) > int(preset[1:]):
        wanted["preset"] = preset
    differs = any(getattr(enc, name) != value for name, value in wanted.items())
    if differs and apply:
        for name, value in wanted.items():
            setattr(enc, name, value)
    return differs


def light_changes(collection: SceneCollection, apply: bool = False, pending_fps: dict[str, int] | None = None,
                  cpu_encoding: bool = False) -> tuple[list[str], dict[str, int]]:
    """What light settings would change, as short lines, and the canvas frame rates to switch to.

    With ``apply`` the collection is changed too, except the canvas frame rates: those change at the
    next start (Settings → Video), so the caller stores them. ``pending_fps`` is what is already waiting.
    """
    lines: list[str] = []
    waiting = pending_fps or {}
    fps = {c.id: LIGHT_FPS for c in collection.canvases if c.enabled and waiting.get(c.id, c.fps) > LIGHT_FPS}
    if fps:
        lines.append(f"{LIGHT_FPS} fps instead of {max(c.fps for c in collection.canvases if c.id in fps)} "
                     "(after a restart)")
    canvases = {c.id: c for c in collection.canvases}
    sized, capped = [], False
    for output in collection.outputs:
        canvas = canvases.get(output.canvas)
        if output.type != "stream" or canvas is None:
            continue
        width, height = output.size or (canvas.width, canvas.height)
        smaller = light_size(width, height)
        if smaller:
            sized.append(f"{smaller[0]}×{smaller[1]}")
            if apply:
                output.size = smaller
        if apply and output.fps and output.fps > LIGHT_FPS:
            output.fps = None  # follow the canvas
        if output.bitrate_kbps > LIGHT_STREAM_KBPS:
            capped = True
            if apply:
                output.bitrate_kbps = LIGHT_STREAM_KBPS
    if sized:
        lines.append(f"Streams sent at {' and '.join(dict.fromkeys(sized))} (your scenes stay as they are)")
    if capped:
        lines.append(f"Stream bitrate {LIGHT_STREAM_KBPS} kbps")
    recorded = [c for c in collection.canvases if c.enabled and c.id in collection.recording.canvases]
    if any(light_size(c.width, c.height) for c in recorded) and (
            not collection.recording.short_side or collection.recording.short_side > LIGHT_SHORT_SIDE):
        lines.append(f"Recordings and replays saved at {LIGHT_SHORT_SIDE}p")
        if apply:
            collection.recording.short_side = LIGHT_SHORT_SIDE
    if collection.recording.bitrate_kbps > LIGHT_RECORDING_KBPS:
        lines.append(f"Recording bitrate {LIGHT_RECORDING_KBPS} kbps")
        if apply:
            collection.recording.bitrate_kbps = LIGHT_RECORDING_KBPS
    preset = CPU_PRESET if cpu_encoding else LIGHT_PRESET
    stream = _light_encoder(collection.stream_encoder, apply, preset)
    recording = _light_encoder(collection.recording.encoder, apply, preset)
    if stream or recording:
        lines.append("Fastest encoder settings: " + ("superfast preset" if cpu_encoding else "fast preset")
                     + ", no look-ahead, multipass or B-frames")
    return lines, fps


def store_fps(fps: dict[str, int]) -> None:
    """Queue canvas frame rates for the next start, next to anything Settings → Video already queued."""
    import json

    from PySide6.QtCore import QSettings

    from sprigcast.app.settings_window import PENDING_VIDEO, pending_video

    if not fps:
        return
    pending = pending_video()
    for canvas_id, value in fps.items():
        pending.setdefault(canvas_id, {})["fps"] = value
    QSettings().setValue(PENDING_VIDEO, json.dumps(pending))


def waiting_fps() -> dict[str, int]:
    from sprigcast.app.settings_window import pending_video

    return {cid: int(change["fps"]) for cid, change in pending_video().items() if "fps" in change}
