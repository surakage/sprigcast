"""Entry point: `uv run sprigcast`."""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths, Qt
from PySide6.QtWidgets import QApplication

from sprigcast import __version__
from sprigcast.core.scene import SceneCollection


def config_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(base)


SET_ASIDE: list[Path] = []  # garden files load_collection couldn't read and moved aside (this run)
RESTORED: list[Path] = []  # the backup copy it opened instead, if any (this run)


def _from_backup(path: Path) -> SceneCollection | None:
    """The newest backup of this garden that can be read (core/backups.py), put back in its place."""
    from sprigcast.core import backups

    for copy in backups.candidates(path):
        try:
            collection = SceneCollection.load(copy)
        except Exception as exc:
            logging.warning("backup %s can't be read either: %s", copy.name, exc)
            continue
        collection.save(path)
        RESTORED.append(copy)
        logging.warning("opened %s instead", copy)
        return collection
    return None


def load_collection(path: Path) -> SceneCollection:
    """Load the user's scene collection, or start from the bundled example on first run. If it can't be
    read, it's moved aside and its newest readable backup opens instead; with none, it starts fresh."""
    if path.exists():
        try:
            collection = SceneCollection.load(path)
        except Exception as exc:
            backup = path.with_suffix(".broken.json")
            path.replace(backup)
            SET_ASIDE.append(backup)
            logging.warning("could not read %s (%s); moved it to %s", path, exc, backup)
            collection = _from_backup(path)
            if collection is not None:
                collection.wrap_blooms()
                return collection
            logging.warning("no backup of %s could be read: starting fresh", path.name)
        else:
            if collection.wrap_blooms():  # blooms hold sprigs only: older blooms' sources move into one each
                backup = path.with_suffix(".before-sprigs.json")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())
                collection.save(path)
            return collection
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    collection = SceneCollection.from_json(text)
    collection.wrap_blooms()
    collection.save(path)
    return collection


def app_icon():
    """The SprigCast S on transparency (data/logo.svg) as the window and taskbar icon."""
    from PySide6.QtGui import QIcon

    if sys.platform == "win32":
        try:  # without its own ID Windows groups SprigCast under python.exe and shows Python's icon
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Slothbloom.SprigCast")
        except Exception:
            pass
    with resources.as_file(resources.files("sprigcast.data").joinpath("logo.svg")) as path:
        return QIcon(str(path))


def brand_mark():
    """The bare S (data/mark.svg, mint with the pink live dot) for the header, where a tile would look boxed in."""
    from PySide6.QtGui import QIcon

    with resources.as_file(resources.files("sprigcast.data").joinpath("mark.svg")) as path:
        return QIcon(str(path))


LEAN_RECORDING = "migrations/recording_no_b_frames"


def lean_recording(collection: SceneCollection, remember: bool = True) -> bool:
    """Once: switch recordings from the old default of 2 B-frames to none (saves ~50 MB per canvas).

    Only the old default is changed; a value you picked yourself in Settings → Stream → Quality is kept, and
    so is anything you set after this ran. True if the collection changed.
    """
    from PySide6.QtCore import QSettings

    settings = QSettings()
    if remember and settings.value(LEAN_RECORDING, False, type=bool):
        return False
    changed = collection.recording.encoder.b_frames == 2
    if changed:
        collection.recording.encoder.b_frames = 0
    if remember:
        settings.setValue(LEAN_RECORDING, True)
    return changed


LEAN_STREAM = "migrations/stream_no_b_frames"


def lean_stream(collection: SceneCollection, remember: bool = True) -> bool:
    """Once: switch streams from the old default of 2 B-frames to none.

    Without B-frames a stream runs on SprigCast's own NVENC encoder instead of FFmpeg's, about 150 MB
    less while live (soak test, 2026-11-13). A value picked by hand in Settings → Stream → Quality is kept.
    """
    from PySide6.QtCore import QSettings

    settings = QSettings()
    if remember and settings.value(LEAN_STREAM, False, type=bool):
        return False
    changed = collection.stream_encoder.b_frames == 2
    if changed:
        collection.stream_encoder.b_frames = 0
    if remember:
        settings.setValue(LEAN_STREAM, True)
    return changed


def _quiet_start_log() -> None:
    """Started without a console (pythonw, e.g. from "Start with Windows"): keep messages in a file."""
    if sys.stderr is not None:
        return
    try:
        folder = config_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = open(folder / "sprigcast.log", "w", encoding="utf-8", buffering=1)  # noqa: SIM115
        from sprigcast.core.privacy import ScrubbingStream

        sys.stdout = sys.stderr = ScrubbingStream(log)  # nothing personal in it either (core/privacy.py)
    except OSError:
        pass


APP_MUTEX = "SprigCastAppMutex"  # the installer's AppMutex: Setup and Uninstall wait while this exists
_mutex = None


def _announce_running() -> None:
    """Windows: hold a named mutex while SprigCast runs, so Setup and Uninstall can ask you to close it."""
    global _mutex
    if sys.platform != "win32":
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, APP_MUTEX)  # released by Windows when the process ends


def _trust_windows_certificates() -> None:
    """Windows: Python's web requests check certificates the way Windows does, so an older PC fetches a root
    certificate it doesn't have yet instead of failing. The update check, Twitch sign-in and stream titles all
    failed with "certificate verify failed" on a tester's donated Windows 10 PC (2026-11-22)."""
    if sys.platform != "win32":
        return
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception as exc:  # noqa: BLE001 - Python's own check stays, as before
        logging.getLogger(__name__).info("certificates: Python's own list (%s)", exc)


def inside_package() -> str:
    """The Windows app package this process runs inside, or "" (the normal case).

    A program started by a Microsoft Store app (Store Python, for one) can inherit that app's package: Windows then
    keeps its registry and AppData writes in the package's private copy, so this SprigCast reads your settings but
    whatever it saves (paired devices, settings) is gone for SprigCast started any other way."""
    if sys.platform != "win32":
        return ""
    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        length = ctypes.c_uint32(0)
        result = kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        if result == 15700:  # APPMODEL_ERROR_NO_PACKAGE
            return ""
        buffer = ctypes.create_unicode_buffer(max(1, length.value))
        if kernel32.GetCurrentPackageFullName(ctypes.byref(length), buffer) == 0:
            return buffer.value or "a package"
    except (AttributeError, OSError):
        return ""
    return ""


# The parts of PyAV the media, camera, recording, replay and stream threads use. Imported here, on the main
# thread, before any of them starts: when two threads import PyAV at the same moment, Python hands one of them the
# half-made module ("partially initialized module 'av.video.reformatter'", "no attribute 'formats_available'",
# "Cannot convert AudioFrame to Frame") and everything that records or plays breaks until a restart.
AV_MODULES = ("av", "av.logging", "av.audio.frame", "av.video.frame", "av.video.reformatter", "av.codec.hwaccel")


def say_libav_lines_when_they_happen() -> None:
    """PyAV holds back a line FFmpeg says twice in a row and writes "(repeated N more times)" only when a
    different line comes, even when the line was below the level being logged. So a camera's refused modes from
    going live ("Could not set video options", which SprigCast reports itself) showed up as errors minutes later,
    in the millisecond a place stopped and FFmpeg said something else (live 2026-11-21). With this off, each
    line is written when it is said, or not at all."""
    import av.logging

    av.logging.set_skip_repeated(False)


def import_av() -> bool:
    import importlib

    try:
        for name in AV_MODULES:
            importlib.import_module(name)
        say_libav_lines_when_they_happen()
    except Exception as exc:  # noqa: BLE001 - a broken PyAV: each user of it says so in its own words
        logging.error("PyAV could not be loaded (%s): videos, cameras, recording and streaming won't work", exc)
        return False
    return True


ALREADY_OPEN_MS = 5000  # the "already open" notice closes by itself after this


def say_already_open(app) -> None:
    """A second launch: say SprigCast is already open (its window comes to the front), then this copy ends."""
    from PySide6.QtCore import QTimer

    from sprigcast.app.starting import StartingCard

    card = StartingCard(brand_mark(), "SprigCast is already open", "It only runs once. Its window is in front.",
                        dots=False, ok=True)
    card.destroyed.connect(app.quit)
    QTimer.singleShot(ALREADY_OPEN_MS, card.done)
    card.show()
    card.activateWindow()
    app.exec()


def second_launch(app) -> bool:
    """Another copy holds the single-copy mutex. While it's still starting, say so (and wait); once it answers,
    say it's already open. True: this copy ends. False: the other copy closed (a restart), so this one opens."""
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import single_instance
    from sprigcast.app.starting import StartingCard

    notice = None

    def waiting() -> None:
        nonlocal notice
        if notice is None:
            notice = StartingCard(brand_mark(), "SprigCast is still starting", "It opens by itself. Please wait")
            notice.show()
        QApplication.processEvents()  # only this notice is on screen yet: keeps its dots moving

    answered = single_instance.wait_for_first_copy(waiting=waiting)
    if notice is not None:
        notice.done()
    if answered:
        # Said in the log: two copies started 8 s apart after an update and the second one's four lines ended
        # with no reason (CRGPlays, 2026-11-27).
        logging.info("another copy of SprigCast is open (or was starting): this one closes, that window comes "
                     "forward")
        say_already_open(app)
    else:
        logging.info("the other copy of SprigCast closed while this one waited: this one opens")
    return answered


def run() -> None:
    import time

    began = time.perf_counter()
    QCoreApplication.setApplicationName("SprigCast")
    QCoreApplication.setOrganizationName("Slothbloom")
    from sprigcast.core import systems

    systems.mend_general_settings()  # Linux: settings saved under "general" read back under their own name
    from sprigcast.app import single_instance

    first_copy = single_instance.claim()  # at once: a slow start mustn't let a second click open a second copy
    _announce_running()
    _trust_windows_certificates()
    _quiet_start_log()
    from sprigcast.core import thread_names

    thread_names.install()  # crash dumps name the thread (Windows)
    if sys.stderr is not None:
        from sprigcast.app.logbook import crash_dumps_all_threads

        # if a native library crashes, print where instead of closing silently (only its own thread on Windows)
        faulthandler.enable(all_threads=crash_dumps_all_threads())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from sprigcast.core.quiet import quiet_libraries

    quiet_libraries()
    from sprigcast.app import logbook

    logbook.setup()  # Documents\SprigCast\Logs: today's log, one report per stream, crashes
    logbook.catch_unhandled()  # errors nobody caught reach the log, not a console that isn't there
    from sprigcast.app import crash_dump

    crash_dump.install(logbook.log_dir())  # a native crash leaves a small dump beside crash.log (Windows)
    from sprigcast import sprigcore

    if sprigcore.engine_choice() == "native":  # the default on Windows (docs/native-engine.md)
        cards = sprigcore.adapters()
        logging.info("native engine: %s, cards %s",
                     "loaded" if sprigcore.available() else "not available, using the Python engine",
                     [c["name"] + (f" ({c['screens']} screen{'s' if c['screens'] != 1 else ''})"
                                   if c.get("screens") else "") for c in cards])
        split = sprigcore.hybrid_split(cards)
        if split is not None:  # (app/main_window.py _hybrid_previews)
            sprigcore.HYBRID = split
            logging.info("hybrid laptop: SprigCast draws on %s, the screen is on %s: previews at 15 a second, "
                         "at most 1280x720", *split)
    else:
        logging.info("Python engine (%s)", sprigcore.why_python())
        from sprigcast.core import systems

        if systems.is_linux() or systems.is_mac():
            logging.info(systems.start_line())
    import_av()
    package = inside_package()
    if package:
        logging.warning("started inside the Windows app package %s (by a Microsoft Store app): settings and "
                        "pairings saved now are kept in that package's own copy, not SprigCast's", package)
    # Web docks (app/custom_docks.py) need these set before the app exists. Qt WebEngine is only
    # loaded when you have custom docks, so without any it costs no memory.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    import os

    from sprigcast.app.browser_host import chromium_flags

    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = chromium_flags(os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", ""))
    from sprigcast.app import custom_docks

    if custom_docks.load():
        custom_docks.import_web_engine()  # Qt WebEngine imported before the QApplication
    from sprigcast.app.theme import use_light_font_engine

    use_light_font_engine()  # 16 MB less than Qt's default on Windows
    # The renderer and the window share Python's lock. By default a thread busy in Python keeps it up to
    # 5 ms before handing it over, and the renderer asks for it several times a frame: while the window
    # rebuilt itself after a bloom switch, that added up to a dropped frame. 1 ms keeps it on time.
    sys.setswitchinterval(0.001)
    app = QApplication(sys.argv)
    app.setApplicationName("SprigCast")
    app.setOrganizationName("Slothbloom")
    app.setApplicationVersion(__version__)
    from sprigcast.app import settings_keeper

    settings_keeper.restore(config_dir())  # any setting gone missing comes back from the copy (see there)
    app.aboutToQuit.connect(lambda: settings_keeper.keep(config_dir()))
    from PySide6.QtCore import QTimer

    # A copy even if closing is cut short; made aside, so the window and the show don't wait for the file
    QTimer.singleShot(30_000, lambda: settings_keeper.keep_aside_soon(config_dir()))
    app.setWindowIcon(app_icon())
    app_at = time.perf_counter() - began
    from sprigcast.app import autostart

    starting = None
    if first_copy and not autostart.start_minimized(sys.argv):
        from sprigcast.app.starting import show_starting

        starting = show_starting(brand_mark())  # on screen now: a slow PC's start can take most of a minute

    def stop_starting() -> None:
        nonlocal starting
        if starting is not None:
            starting.done()
            starting = None

    from sprigcast.core.painted import brand_fonts

    brand_fonts()  # Nunito for the text plugins (timer, clock, Now playing)
    from sprigcast.app import crash_report

    if first_copy:
        if single_instance.ask_running_copy():  # already open (no mutex: not Windows): that window comes forward
            logging.info("another copy of SprigCast is open: this one closes, that window comes forward")
            say_already_open(app)
            sys.exit(0)
    elif second_launch(app):  # open or still starting: that window comes forward instead
        sys.exit(0)
    elif not autostart.start_minimized(sys.argv):  # the other copy closed (a restart): this one is the start now
        from sprigcast.app.starting import show_starting

        starting = show_starting(brand_mark())
    shown: list = []  # the window, once it exists (a second launch before then just waits)
    server = single_instance.listen(lambda: shown and single_instance.bring_forward(shown[0]), app)
    from sprigcast.app import control_server

    control_server.open_early(app)  # Streamer.bot and friends sign in while SprigCast starts (answered once it's up)
    crashed = crash_report.check(config_dir(), logbook.crash_log())  # did the last run close cleanly?
    app.aboutToQuit.connect(lambda: crash_report.closed_cleanly(config_dir()))
    # Windows' Restart Manager (the installer) and signing out ask through the session: the close log line says so
    app.commitDataRequest.connect(lambda _manager: app.setProperty("sprigcast_session_end", True))
    from sprigcast.app import safe_mode

    def ask_safe(*args, **kwargs):
        stop_starting()  # never over the question
        return safe_mode.ask(*args, **kwargs)

    safe = safe_mode.choose(sys.argv, crashed, autostart.start_minimized(sys.argv), ask_safe)
    if safe and sprigcore.engine_choice() == "native":
        sprigcore.use_python_engine()  # nothing has started yet: this start draws, captures and mixes the old way
        logging.warning("safe mode: the Python engine for this start")

    from sprigcast.app import priority
    from sprigcast.app.theme import apply_theme

    priority.apply()  # stay smooth next to games: CPU priority from Settings, GPU work scheduled first
    from sprigcast.plugins import ensure_example, load_plugins

    apply_theme(app)
    from sprigcast.app import wheel_guard

    wheel_guard.install(app)  # the wheel scrolls the page, not a drop-down you didn't click
    plugins_dir = config_dir() / "plugins"
    if safe:
        from sprigcast.plugins import PluginReport

        plugins = PluginReport()  # none this time (app/safe_mode.py)
    else:
        try:
            ensure_example(plugins_dir)
        except OSError as exc:
            logging.warning("could not create %s: %s", plugins_dir, exc)
        try:
            from sprigcast.plugin_store import refresh_installed

            refresh_installed(plugins_dir)  # store plugins you added get this version's fixes
        except OSError as exc:
            logging.warning("could not update store plugins: %s", exc)
    try:
        from sprigcast.app.leftovers import clean_up

        clean_up(config_dir())  # what features taken out of SprigCast left on this PC, once
    except Exception as exc:  # noqa: BLE001 (never stops SprigCast from opening)
        logging.warning("could not tidy up after removed features: %s", exc)
    if not safe:
        plugins = load_plugins(plugins_dir)

    from sprigcast.app.main_window import MainWindow

    from sprigcast.core.gardens import Shelf

    shelf = Shelf(config_dir())  # Gardens and Paths; the first run with them keeps your setup as "Main"
    scenes_path = shelf.garden_file()
    had_garden = scenes_path.exists()
    try:
        collection = load_collection(scenes_path)
    except Exception as exc:
        from sprigcast.app import message as msgbox

        stop_starting()
        msgbox.critical(None, "SprigCast", f"Could not load scenes:\n{exc}")
        sys.exit(1)

    from sprigcast.app.settings_window import apply_pending_video

    changed = apply_pending_video(collection)  # canvas size / fps changed in Settings → Video last time
    changed = lean_recording(collection) or changed
    changed = lean_stream(collection) or changed
    if changed:
        collection.save(scenes_path)
    broken = scenes_path.with_suffix(".broken.json") in SET_ASIDE  # unreadable: a backup or a fresh start
    restored = RESTORED[-1] if broken and RESTORED else None
    fresh = not had_garden or (broken and restored is None)
    if fresh and shelf.path_file().exists():
        try:  # a new or rebuilt garden: your path's settings go into it, not the other way round
            shelf.load_path(shelf.path()).apply_to(collection)
            collection.save(scenes_path)
        except (OSError, ValueError) as exc:
            logging.warning("could not read the path: %s", exc)
    try:
        shelf.sync_path(collection)  # the garden holds the newest copy of the path in use
    except OSError as exc:
        logging.warning("could not save the path: %s", exc)
    garden_at = time.perf_counter() - began
    window = MainWindow(collection, scenes_path, plugins_dir, shelf=shelf)
    window_at = time.perf_counter() - began
    window._disk_free_gb(window._recordings_folder())  # measured now on a worker, ready for Garden check
    window.start_in_live()  # always opens on Live, not the screen it closed on
    try:
        from sprigcast.app.leftovers import clean_up_recordings

        clean_up_recordings(window._recordings_folder())  # Auto clip's unkept suggestions, once
    except Exception as exc:  # noqa: BLE001 (never stops SprigCast from opening)
        logging.warning("could not tidy up Auto clip's suggestions: %s", exc)
    shown.append(window)
    window._single_copy = server  # kept alive with the window
    if broken:  # its file couldn't be read: say so once, what opened instead, and where the old one is kept
        from PySide6.QtCore import QTimer

        from sprigcast.core import backups

        opened = f"so SprigCast opened {backups.when(restored)}" if restored else "so it started fresh"
        from sprigcast.app import message as msgbox

        QTimer.singleShot(1500, lambda: msgbox.warning(
            window, "SprigCast", f"{shelf.garden().name} couldn't be read, {opened}.\n"
                                 f"The old file is kept as:\n{scenes_path.with_suffix('.broken.json')}"))
    window.plugin_report = plugins
    if safe:
        window.setWindowTitle(f"{window.windowTitle()} (safe mode)")
    autostart.refresh()
    # Said each start: the tester's PC took close to a minute and nothing told where (2026-11-22)
    logging.info("start-up: window up after %.1f s (app made at %.1f s, garden loaded at %.1f s, window made at "
                 "%.1f s)", time.perf_counter() - began, app_at, garden_at, window_at)
    if autostart.start_minimized(sys.argv):
        window.show_minimized_drawn()  # drawn once first: opening it later shows it at once
    else:
        window.show()
        stop_starting()  # the window takes its place
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, window.first_run_optimizer)  # once: Optimize for this PC
        if crashed is not None:
            from sprigcast.app import problem_report, share_reports

            if safe_mode.unattended():  # nobody answered the safe mode question: no card waiting over the window
                logging.info("report: crash card not shown: nobody answered the safe mode question (it's in the "
                             "log; Help → Report a problem sends it)")
                window.statusBar().showMessage("SprigCast closed unexpectedly last time. Help → Report a problem "
                                               "sends a report.", 60_000)
            elif share_reports.covers("crash"):  # Share logs automatically: send it, no card (user 2026-11-27)
                logging.info("report: crash report sent automatically (Share logs automatically is on)")
                problem_report.send_report(window, "", True, crashed, show_result=False)
                window.statusBar().showMessage("SprigCast closed unexpectedly last time. Its log was sent to help "
                                               "fix it.", 30_000)
            else:
                QTimer.singleShot(1200, lambda: problem_report.ask(window, crashed))  # what happened? send it?
    from PySide6.QtCore import QTimer

    from sprigcast.core import pacing

    pacing.collect_on_main_thread()  # Qt objects in cycles are never freed on another thread (crashed tests)
    pacing.watch_gc()  # logs slow garbage collections (they made the late frames in long streams)
    beat_ms, watch_s = pacing.watch_window()  # SPRIGCAST_UI_STALL_MS=50: every window stall from 50 ms, with where
    pacing.watch_stalls(interval=watch_s)  # names the thread whenever something holds everything up for 200 ms
    beat = QTimer(window)  # and whenever the window itself stops answering, where it was (pacing.ui_beat)
    if beat_ms < pacing.UI_BEAT_MS:
        beat.setTimerType(Qt.PreciseTimer)
    beat.timeout.connect(pacing.ui_beat)
    beat.start(beat_ms)
    QTimer.singleShot(8000, pacing.tame_gc)  # once start-up is done: freeze what it made
    collect = QTimer(window)  # then a small collection every few seconds instead of a long one every ten minutes
    collect.timeout.connect(lambda: pacing.housekeeping("tidy", pacing.tidy))
    collect.start(pacing.TIDY_EVERY_MS)
    tidy = QTimer(window)  # and the minute's housekeeping
    # Each piece waits for the end of a frame and they run seconds apart (see pacing.housekeeping): together, at
    # once, they made a late frame on the same second of the minute on the PC (2026-11-17).
    tidy.timeout.connect(lambda: QTimer.singleShot(  # which threads come and go, for the next long test
        20_000, lambda: workers.run(pacing.watch_threads, name="thread count")))
    # (on a shared worker: asking Windows for the process's thread count held the window about 50 ms, 2026-11-19)
    tidy.timeout.connect(lambda: QTimer.singleShot(  # and where memory goes, every 10 minutes
        40_000, lambda: pacing.housekeeping("memory line", pacing.watch_memory)))
    tidy.timeout.connect(lambda: QTimer.singleShot(  # and which threads use the CPU, every 5 minutes
        50_000, lambda: threading.Thread(target=pacing.watch_cpu, name="CPU by thread", daemon=True).start()))
    # (on its own thread: naming ~100 native threads held the window 340 ms on the PC, 2026-11-17)
    tidy.start(pacing.MINUTE_MS)
    from sprigcast.core import warm, workers

    def _warm_art():  # the sloth pictures pop-ups show (app/empty.py)
        from sprigcast.app.empty import warm_art

        warm_art()

    def warm_up():  # see core/warm.py
        encoders = (window.collection.recording.encoder.model_copy(), window.collection.stream_encoder.model_copy())
        workers.run(lambda: (warm.warm(), warm.warm_encoders(*encoders), _warm_art(), warm.keep_aside()),
                    name="warm-up")

    from sprigcast.sources import android_screen

    # Where Google's adb goes if Android Screen downloads it for you (sources/android_screen.py)
    android_screen.ADB_HOME = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation)) / "adb"
    warm_after = warm.warm_after_ms()  # later on a PC with little memory (core/warm.py)
    QTimer.singleShot(warm_after, warm_up)
    from sprigcast.app.theme import warm_card_shadow

    QTimer.singleShot(warm_after + 1_000, lambda: warm_card_shadow(window))  # the shadow pop-ups share
    QTimer.singleShot(warm_after + 3_000, window.warm_settings)  # Settings opens faster the first time
    if warm_after != warm.WARM_AFTER_MS:
        logging.getLogger(__name__).info("warm-up: in %d s, slowly (this PC has little memory)", warm_after // 1000)
    workers.run(warm.warm_keyring, name="warm-keyring")  # now: a stream key or chat sign-in may want it soon
    QTimer.singleShot(20_000, pacing.trim_working_set_later)  # start-up's one-time pages (imports, shaders) go back
    from sprigcast.app import engine_warning

    engine_warning.install(window)  # says when the old engine draws on a PC that could run the new one
    if plugins.failed:
        names = ", ".join(plugins.failed)
        window.statusBar().showMessage(f"Some plugins failed to load: {names} (details in the console)", 20000)
    pacing.use_exit_guard()  # closing can't hang for ever (pacing.exit_guard)
    from sprigcast.core import crash_test

    crash_test.schedule(QTimer.singleShot)  # SPRIGCAST_CRASH_TEST: crash on purpose, for testing the crash guard
    code = app.exec()
    window.engine.finish_stopping()  # the window is gone: now wait for drawing and sound to stop
    if app.property("sprigcast_restart"):  # Settings' Restart now
        if server is not None:
            server.close()  # let go first, or the new copy would only bring this closing one forward
        relaunch()
    sys.exit(code)


def restart_command(argv: list[str] | None = None, frozen: bool | None = None) -> list[str]:
    """How to start SprigCast again: the same program and arguments, without --safe-mode."""
    argv = list(sys.argv if argv is None else argv)
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    args = [a for a in (argv[1:] if frozen else argv) if a != "--safe-mode"]
    return [sys.executable, *args]


def relaunch() -> None:
    from PySide6.QtCore import QProcess

    program, *args = restart_command()
    if not QProcess.startDetached(program, args):
        logging.getLogger(__name__).warning("restart: could not start %s", program)


if __name__ == "__main__":
    run()
