"""Words about places you stream to: what goes there, what's missing, and how it's doing while live.

Live's "Destinations" card (app/live_stage.py) and the stream doctor use these.
"""

from __future__ import annotations

from collections.abc import Callable

from sprigcast.app import theme
from sprigcast.core.scene import Output, SceneCollection
from sprigcast.output.stream import SERVICES

HEALTH_COLORS = {"good": theme.ACCENT, "warning": theme.WARN, "bad": theme.BAD, "idle": "#5d6b64", "off": theme.EDGE_STRONG}


def what_goes(output: Output, collection: SceneCollection) -> str:
    """"horizontal 1920×1080 · 60 fps" or "Enhanced · horizontal + vertical"."""
    if output.service == "twitch" and output.enhanced:
        return "Enhanced · " + ("horizontal + vertical" if output.enhanced_vertical else output.canvas)
    canvas = next((c for c in collection.canvases if c.id == output.canvas), None)
    w, h = output.size or ((canvas.width, canvas.height) if canvas else (0, 0))
    fps = output.fps or (canvas.fps if canvas else 0)
    return f"{output.canvas.capitalize()} · {w}×{h} · {fps} fps · {output.bitrate_kbps} kbps"


def problem(output: Output, collection: SceneCollection, has_key: Callable[[str | None], bool]) -> str | None:
    """Why this destination can't go live, in a few words, or None when it's ready."""
    service = SERVICES.get(output.service or "custom", SERVICES["custom"])
    if not (service.server or output.server):
        return "Needs a server URL"
    if not (has_key(output.key_ref) or (output.service == "twitch" and output.twitch_account)):
        return "Needs a stream key"
    canvas = next((c for c in collection.canvases if c.id == output.canvas), None)
    if canvas is None or not canvas.enabled:
        return f"The {output.canvas} canvas is off"
    return None


def live_line(st) -> tuple[str, str]:
    """(level, text) for one streamer, for its card."""
    level, reason = st.health()
    if not st.connected:
        waiting = getattr(st, "delay_left", lambda: 0.0)() > 0  # sitting out the stream delay: all is well
        return ("good" if waiting else "warning"), getattr(st, "waiting_text", lambda: st.status)().capitalize()
    text = f"{st.measure_kbps():.0f} kbps · {st.out_width}×{st.out_height} {st.fps} fps"
    if reason != "live":
        text += f" · {reason}"
    pilot = getattr(st, "autopilot", None)
    if pilot is not None and pilot.lowered:
        text += f" · auto-pilot {pilot.current_kbps} of {pilot.target_kbps} kbps"
    if st.dropped:
        text += f" · {st.dropped} dropped"
    if st.reconnects:
        text += f" · {st.reconnects} reconnects"
    return level, text



ENCODER_FULL = 85.0  # % of the video encoder we plan to use at most, leaving room for spikes


def encoder_room(load_pct: float | None, sessions: int) -> int | None:
    """How many more encodes like the running ones fit on the video encoder, or None when unknown.

    Each running encode is taken to cost an equal share of the measured load. Shared encodes count
    once: destinations that share one add nothing."""
    if load_pct is None or sessions <= 0:
        return None
    each = max(load_pct / sessions, 5.0)  # a reading of 0-2% is noise, not a free encoder
    return max(0, int((ENCODER_FULL - load_pct) // each))


def room_line(load_pct: float | None, sessions: int) -> tuple[bool, str] | None:
    """(has room, "Encoder 30% busy · room for about 2 more") for the Garden check, or None."""
    room = encoder_room(load_pct, sessions)
    if room is None:
        return None
    more = "no room for more" if room == 0 else f"room for about {room} more"
    return room > 0, f"Encoder {load_pct:.0f}% busy · {more}"
