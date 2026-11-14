"""Words about places you stream to: what goes there, what's missing, and how it's doing while live.

Live's "Where it goes" card (app/live_stage.py) and the stream doctor use these.
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
    return f"{output.canvas} · {w}×{h} · {fps} fps · {output.bitrate_kbps} kbps"


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
        return "warning", st.status.capitalize()
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

