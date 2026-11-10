"""Messages between the UI and the engine. Both directions travel over queue.Queue."""

from __future__ import annotations

from dataclasses import dataclass

from sprigcast.core.scene import SceneCollection


# UI -> engine ---------------------------------------------------------------

@dataclass
class ReplaceCollection:
    """Send the whole scene collection after any edit. Simple, and cheap at this size."""

    collection: SceneCollection


@dataclass
class Shutdown:
    pass


# Engine -> UI ---------------------------------------------------------------

@dataclass
class Stats:
    fps: float
    render_ms: float  # average time to draw and read back all canvases in one tick
    dropped_ticks: int  # ticks that started late because the previous one ran long


@dataclass
class EngineError:
    message: str
