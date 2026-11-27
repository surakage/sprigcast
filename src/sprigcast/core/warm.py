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
import threading
import time

log = logging.getLogger(__name__)

WARM_AFTER_MS = 12_000  # after startup's own work (main.py)
# On a PC with little memory (core/pacing.py low_memory) the warm-up waits longer and goes slower: loading every
# module at once from a hard drive with 1 GB free froze the window 125 s just after start (Mikedog151, E2-7110,
# 4 GB, 2026-11-25), each module pushing the window's own pages out to the disk.
LOW_MEMORY_WARM_AFTER_MS = 90_000
LOW_MEMORY_PAUSE_S = 0.25


def warm_after_ms() -> int:
    from sprigcast.core import pacing

    return LOW_MEMORY_WARM_AFTER_MS if pacing.low_memory() else WARM_AFTER_MS


def pause_between() -> float:
    from sprigcast.core import pacing

    return LOW_MEMORY_PAUSE_S if pacing.low_memory() else 0.02
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
    "sprigcast.app.projector", "sprigcast.app.stream_details_dialog",
    # The 10-minute memory line's map (core/pacing.py watch_memory): its first load on the window's thread held the
    # window 268 ms on the PC (2026-11-19 camera test).
    "sprigcast.core.memory_map",
]
WINDOWS_ONLY = ["sprigcast.output.nvenc_d3d11"]


def warm(modules: list[str] | None = None, pause_s: float | None = None) -> list[str]:
    """Import each module not loaded yet, pause_s apart (None: pause_between()). Returns the ones it loaded. Never
    raises."""
    if pause_s is None:
        pause_s = pause_between()
    names = list(MODULES if modules is None else modules)
    if modules is None and sys.platform == "win32":
        names += WINDOWS_ONLY
    loaded, began = [], time.perf_counter()
    took: list[tuple[float, str]] = []
    for name in names:
        if name in sys.modules:
            continue
        try:
            at = time.perf_counter()
            importlib.import_module(name)
            took.append((1000 * (time.perf_counter() - at), name))
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 (a missing optional part: it loads, or fails, when used)
            log.debug("warm-up: %s didn't load: %s", name, exc)
        time.sleep(pause_s)  # let the window and the renderer have Python between modules
    if modules is None and sys.platform == "win32":
        try:
            from sprigcast.sources import camera

            lister = camera.warm_lister()
            if lister != "light":
                log.info("warm-up: cameras are listed by %s", lister or "nothing (no lister loads)")
        except Exception as exc:  # noqa: BLE001
            log.debug("warm-up: the camera lister didn't load: %s", exc)
    if modules is None:
        warm_ffmpeg_files()
    if loaded:
        total = 1000 * (time.perf_counter() - began)
        log.info("warm-up: loaded %d modules in %.0f ms%s", len(loaded), total, slowest(took, total))
    return loaded


def warm_ffmpeg_files() -> None:
    """Find PyAV's FFmpeg files now (sprigcore keeps the answer): found for the first time at go-live, the folder
    look held the engine 28 frames on a hard drive (Mikedog151, 2026-11-25). Never raises."""
    try:
        from sprigcast import sprigcore

        sprigcore.ffmpeg_libraries()
        for name in ("avfilter", "avdevice"):
            import av

            sprigcore._ffmpeg_file(av, name)
    except Exception as exc:  # noqa: BLE001 (looked for again when needed)
        log.debug("warm-up: FFmpeg files not found: %s", exc)


SLOW_MS = 1500.0  # a warm-up over this says which modules took it


def slowest(took: list[tuple[float, str]], total_ms: float) -> str:
    """"; slowest: output.recorder 2100 ms, app.doctor 600 ms" for a slow warm-up: the first start of a new build
    took 3,698 ms against 566 to 890 ms (PC 2026-11-21) and the log couldn't say which load it was, or whether it
    was every file being read for the first time."""
    if total_ms < SLOW_MS:
        return ""
    top = sorted(took, reverse=True)[:3]
    return "; slowest: " + ", ".join(f"{name.removeprefix('sprigcast.')} {ms:.0f} ms" for ms, name in top)


def keep_aside() -> None:
    """Freeze what the warm-up imported (modules, classes, functions: kept for the whole session) without a
    collection: the next collections skip it instead of walking it (a full one over it held the window 44 ms
    on the PC just after start, 2026-11-18). Freezing is quick; what isn't kept after all is freed at rest."""
    import gc

    gc.freeze()


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


WINDOWS_STORE = "keyring.backends.Windows.WinVaultKeyring"


def windows_store_only() -> None:
    """Point keyring straight at Windows Credential Manager, before it's first used.

    Left to itself, keyring finds its store by importing every backend it has. Its macOS one fails part way
    on Windows and is taken back out of sys.modules. Done on the warm-up worker, that raced Qt's signature
    code on the window's thread, which was walking sys.modules at that moment: KeyError
    'keyring.backends.macOS.api', then a fail-fast crash (test build 6 start, PC 2026-11-27). Naming the store
    imports only the Windows backend. Someone who set PYTHON_KEYRING_BACKEND themselves keeps theirs."""
    import os

    if sys.platform == "win32":
        os.environ.setdefault("PYTHON_KEYRING_BACKEND", WINDOWS_STORE)


keyring_loading = threading.Event()  # set while warm_keyring is at it: a read from the window would wait for it


def warm_keyring() -> None:
    """Load the saved-password store (keyring and its Windows backend) in the background: the first stream
    key read loaded it on the window's thread, which froze 2.9 s on the PC (2026-11-16). Reads nothing.
    While it loads, the off-screen Settings warm-up waits (it reads a place's key, and would wait here too:
    16.6 s on a cold build machine, 2026-11-19)."""
    keyring_loading.set()
    try:
        began = time.perf_counter()
        windows_store_only()
        import keyring

        backend = keyring.get_keyring()
        log.info("warm-up: password store ready in %.0f ms (%s)", 1000 * (time.perf_counter() - began),
                 type(backend).__name__)
    except Exception as exc:  # noqa: BLE001 (no store here: reading a key says so when it happens)
        log.info("warm-up: password store unavailable: %s", exc)
    finally:
        keyring_loading.clear()
