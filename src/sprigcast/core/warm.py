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
    "sprigcast.output.encoder_load",
    # What the pop-ups load the first time they open. On the PC's pop-up sweep (2026-11-18, live, Heavy test)
    # loading them was most of the first open: the Audio page 256 ms, the plugin store 138 ms, Report a problem
    # 84 ms. None makes a Qt object as it loads, so a worker can load them.
    "sprigcast.app.audio_page", "sprigcast.app.hotkeys_dialog", "sprigcast.plugin_store", "sprigcast.plugins",
    "sprigcast.app.plugin_store_page", "sprigcast.app.problem_report", "sprigcast.app.tester_report",
    "sprigcast.app.health_page", "sprigcast.app.about", "sprigcast.app.support", "sprigcast.app.busy_pc",
    "sprigcast.app.lowend", "sprigcast.app.optimize", "sprigcast.app.connections_page",
    "sprigcast.app.getting_started", "sprigcast.app.media_dialog", "sprigcast.app.game_dialog",
    "sprigcast.app.camera_popup", "sprigcast.app.web_page_dialog", "sprigcast.app.browser_quick",
    "sprigcast.app.source_record_box", "sprigcast.app.signin_dialog", "sprigcast.app.settings_file",
    "sprigcast.app.projector",
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


def warm_encoders(recording=None, stream=None) -> None:
    """Find out, in the background, which video encoder recording and streaming will use (each check opens an
    encoder and encodes a test frame; the answers are kept for the run). Done at record start instead, the
    HEVC check froze the window 507 ms on the PC (2026-11-16). Never raises."""
    try:
        from sprigcast.output import recorder

        began = time.perf_counter()
        picked = recorder.pick_codec()
        chosen = [recorder.resolve_codec(recording)] if recording is not None else []
        if stream is not None:
            chosen.append(recorder.resolve_codec(stream, streaming=True))
        if "hevc_nvenc" in chosen:
            recorder.direct_hevc_ok()
        log.info("warm-up: encoders checked in %.0f ms (best %s; using %s)", 1000 * (time.perf_counter() - began),
                 picked, ", ".join(chosen) or picked)
    except Exception as exc:  # noqa: BLE001 (checked again when used)
        log.info("warm-up: encoder check failed: %s", exc)


def warm_keyring() -> None:
    """Load the saved-password store (keyring and its Windows backend) in the background: the first stream
    key read loaded it on the window's thread, which froze 2.9 s on the PC (2026-11-16). Reads nothing."""
    try:
        began = time.perf_counter()
        import keyring

        backend = keyring.get_keyring()
        log.info("warm-up: password store ready in %.0f ms (%s)", 1000 * (time.perf_counter() - began),
                 type(backend).__name__)
    except Exception as exc:  # noqa: BLE001 (no store here: reading a key says so when it happens)
        log.info("warm-up: password store unavailable: %s", exc)
