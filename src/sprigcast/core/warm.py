"""Load, in the background, the code that going live, recording, clips and the virtual camera need.

Python loads a module the first time something imports it. Recording, the stream and the clips buffer import
theirs only when they start, on the window's thread, and a first load can be slow on Windows (the antivirus
reads every file first): on the PC (2026-11-16) starting to record froze the window 2.4 s that way. A worker
loads them a while after startup instead, one at a time. They're small (no web views, no AI models), so it
costs a few MB at most.
"""

from __future__ import annotations

import importlib
import logging
import sys
import time

log = logging.getLogger(__name__)

WARM_AFTER_MS = 12_000  # after startup's own work (main.py)
MODULES = [
    "av.codec.hwaccel", "av.video.reformatter", "av.video.frame",
    "sprigcast.output.naming", "sprigcast.output.storage", "sprigcast.output.keys",
    "sprigcast.output.recorder", "sprigcast.output.source_record", "sprigcast.output.replay",
    "sprigcast.output.shared", "sprigcast.output.shared_stream", "sprigcast.output.delay",
    "sprigcast.output.stream", "sprigcast.output.enhanced", "sprigcast.output.enhanced_stream",
    "sprigcast.output.twitch_auth", "sprigcast.output.autopilot", "sprigcast.output.virtualcam",
    "sprigcast.app.health", "sprigcast.app.garden_check", "sprigcast.app.doctor",
]
WINDOWS_ONLY = ["sprigcast.output.nvenc_d3d11"]


def warm(modules: list[str] | None = None, pause_s: float = 0.02) -> list[str]:
    """Import each module not loaded yet. Returns the ones it loaded. Never raises."""
    names = list(MODULES if modules is None else modules)
    if modules is None and sys.platform == "win32":
        names += WINDOWS_ONLY
    loaded, began = [], time.perf_counter()
    for name in names:
        if name in sys.modules:
            continue
        try:
            importlib.import_module(name)
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 (a missing optional part: it loads, or fails, when used)
            log.debug("warm-up: %s didn't load: %s", name, exc)
        time.sleep(pause_s)  # let the window and the renderer have Python between modules
    if loaded:
        log.info("warm-up: loaded %d modules in %.0f ms", len(loaded), 1000 * (time.perf_counter() - began))
    return loaded
