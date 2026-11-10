"""Entry point: `uv run sprigcast`."""

from __future__ import annotations

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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("SprigCast")
    app.setOrganizationName("Slothbloom")
    app.setApplicationVersion(__version__)

    from sprigcast.app.main_window import MainWindow

    scenes_path = config_dir() / "scenes.json"
    try:
        collection = load_collection(scenes_path)
    except Exception as exc:
        QMessageBox.critical(None, "SprigCast", f"Could not load scenes:\n{exc}")
        sys.exit(1)

    window = MainWindow(collection, scenes_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run()
