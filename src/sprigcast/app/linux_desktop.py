"""SprigCast's name, icon and launcher entry on a Linux desktop.

Without them GNOME called the app "python3" (its "is ready" notice, the dock and the window switcher) and showed
no icon, because the download is a private Python that runs SprigCast (sim PC, 2026-11-28). Two parts:

- the app tells the desktop which entry it is (`name_app`, before any window shows);
- the download's launcher puts that entry into the user's own applications folder (`keep_entry`), pointing at
  wherever the folder was unpacked, and corrects it when the folder moves. Nothing is written when SprigCast
  runs from source, and nothing outside the user's own folder is touched.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# One plain word, no dots: on Wayland the window's app id is this name, and on X11 Qt derives the window class from
# it (some Qt versions keep only the part before the first dot), so "sprigcast" matches the entry either way.
APP_ID = "sprigcast"
LAUNCHER_ENV = "SPRIGCAST_LAUNCHER"  # set by the download's launcher (packaging/build_unix.sh): its own path


def name_app() -> bool:
    """Tell the desktop which entry this app is. True when it was set (Linux only)."""
    from sprigcast.core.systems import is_linux

    if not is_linux():
        return False
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.setDesktopFileName(APP_ID)
    return True


def applications_dir(environ=None) -> Path:
    env = os.environ if environ is None else environ
    base = env.get("XDG_DATA_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "share")
    return Path(base) / "applications"


def _quoted(path: str) -> str:
    """A path as the Exec line wants it (quoted, with its reserved characters escaped)."""
    for ch in ("\\", '"', "`", "$"):
        path = path.replace(ch, "\\" + ch)
    return '"' + path.replace("%", "%%") + '"'


def entry_text(launcher: str, icon: str) -> str:
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=SprigCast\n"
            "Comment=Stream and record\n"
            f"Exec={_quoted(launcher)}\n"
            f"Icon={icon}\n"
            "Terminal=false\n"
            "Categories=AudioVideo;Video;Recorder;\n"
            f"StartupWMClass={APP_ID}\n"
            "StartupNotify=true\n")


def keep_entry(icon: str, environ=None) -> Path | None:
    """Write the launcher entry if it is missing or points somewhere else. Returns its path when written."""
    from sprigcast.core.systems import is_linux

    env = os.environ if environ is None else environ
    launcher = (env.get(LAUNCHER_ENV) or "").strip()
    if not is_linux() or not launcher or not os.path.isfile(launcher):
        return None
    path = applications_dir(env) / f"{APP_ID}.desktop"
    text = entry_text(launcher, icon)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        aside = path.with_suffix(".desktop.new")
        aside.write_text(text, encoding="utf-8")
        os.replace(aside, path)
    except OSError as exc:
        log.info("launcher entry not written: %s", exc.__class__.__name__)
        return None
    log.info("launcher entry written (SprigCast now shows in the desktop's apps list)")
    return path
