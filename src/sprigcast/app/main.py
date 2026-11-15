"""Entry point: `uv run sprigcast`."""

from __future__ import annotations

import faulthandler
import logging
import sys
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QStandardPaths, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from sprigcast import __version__
from sprigcast.core.scene import SceneCollection


def config_dir() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    return Path(base)


def load_collection(path: Path) -> SceneCollection:
    """Load the user's scene collection, or start from the bundled example on first run."""
    if path.exists():
        try:
            collection = SceneCollection.load(path)
        except Exception as exc:
            backup = path.with_suffix(".broken.json")
            path.replace(backup)
            logging.warning("could not read %s (%s); moved it to %s and started fresh", path, exc, backup)
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
    """The SprigCast S on its dark green tile (data/logo.svg) as the window and taskbar icon."""
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

    Only the old default is changed; a value you picked yourself in Settings → Output is kept, and
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
    less while live (soak test, 2026-11-13). A value picked by hand in Settings → Output is kept.
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
        sys.stdout = sys.stderr = log
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


def run() -> None:
    QCoreApplication.setApplicationName("SprigCast")
    QCoreApplication.setOrganizationName("Slothbloom")
    _announce_running()
    _quiet_start_log()
    if sys.stderr is not None:
        faulthandler.enable()  # if a native library crashes, print where instead of closing silently
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from sprigcast.app import logbook

    logbook.setup()  # Documents\SprigCast\Logs: today's log, one report per stream, crashes
    # Web docks (app/custom_docks.py) need these set before the app exists. Qt WebEngine is only
    # loaded when you have custom docks, so without any it costs no memory.
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from sprigcast.app import custom_docks

    if custom_docks.load():
        custom_docks.web_engine_available()  # imports Qt WebEngine before the QApplication
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
    app.setWindowIcon(app_icon())

    from sprigcast.app import priority
    from sprigcast.app.theme import apply_theme

    priority.apply()  # stay smooth next to games: CPU priority from Settings, GPU work scheduled first
    from sprigcast.plugins import ensure_example, load_plugins

    apply_theme(app)
    plugins_dir = config_dir() / "plugins"
    try:
        ensure_example(plugins_dir)
    except OSError as exc:
        logging.warning("could not create %s: %s", plugins_dir, exc)
    plugins = load_plugins(plugins_dir)

    from sprigcast.app.main_window import MainWindow

    scenes_path = config_dir() / "scenes.json"
    try:
        collection = load_collection(scenes_path)
    except Exception as exc:
        QMessageBox.critical(None, "SprigCast", f"Could not load scenes:\n{exc}")
        sys.exit(1)

    from sprigcast.app.settings_window import apply_pending_video

    changed = apply_pending_video(collection)  # canvas size / fps changed in Settings → Video last time
    changed = lean_recording(collection) or changed
    changed = lean_stream(collection) or changed
    if changed:
        collection.save(scenes_path)
    window = MainWindow(collection, scenes_path, plugins_dir)
    window.plugin_report = plugins
    from sprigcast.app import autostart

    autostart.refresh()
    if autostart.start_minimized(sys.argv):
        window.showMinimized()
    else:
        window.show()
        from PySide6.QtCore import QTimer

        QTimer.singleShot(1500, window.first_run_optimizer)  # once: Optimize for this PC
    if plugins.failed:
        names = ", ".join(plugins.failed)
        window.statusBar().showMessage(f"Some plugins failed to load: {names} (details in the console)", 20000)
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
