"""Entry point: `uv run sprigcast`."""

from __future__ import annotations

import faulthandler
import logging
import sys
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QStandardPaths
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
            return SceneCollection.load(path)
        except Exception as exc:
            backup = path.with_suffix(".broken.json")
            path.replace(backup)
            logging.warning("could not read %s (%s); moved it to %s and started fresh", path, exc, backup)
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    collection = SceneCollection.from_json(text)
    collection.save(path)
    return collection


def run() -> None:
    faulthandler.enable()  # if a native library crashes, print where instead of closing silently
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("SprigCast")
    app.setOrganizationName("Slothbloom")
    app.setApplicationVersion(__version__)

    from sprigcast.app.theme import apply_theme
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

    if apply_pending_video(collection):  # canvas size / fps changed in Settings → Video last time
        collection.save(scenes_path)
    window = MainWindow(collection, scenes_path, plugins_dir)
    window.plugin_report = plugins
    window.show()
    if plugins.failed:
        names = ", ".join(plugins.failed)
        window.statusBar().showMessage(f"Some plugins failed to load: {names} (details in the console)", 20000)
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
