"""Clear all my data: everything SprigCast keeps on this PC goes, then SprigCast closes (the user's rule,
2026-11-17: no data left behind).

What goes:
- secrets: stream keys and sign-ins in Windows Credential Manager, the control API password and paired
  devices, api.json and connection.json (tools/forget_keys.py, which the uninstaller also runs);
- settings: every SprigCast setting (the registry key HKCU\\Software\\Slothbloom\\SprigCast) and Start with
  Windows;
- files: SprigCast's own folders (gardens, paths, plugins, web page storage, downloaded updates, chat
  docks), Documents\\SprigCast (logs, looks, exports) and the camera's log;
- with ``recordings``: the recordings folder too, but only when it's SprigCast's own (its name says
  SprigCast), so a folder like D:\\Videos that also holds other videos is never wiped.

Files in use can't be deleted while SprigCast runs, so the folders and the settings key go in a small
script that waits for SprigCast to close first. Anything SprigCast writes on its way out goes with them.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

SETTINGS_KEY = r"HKCU\Software\Slothbloom\SprigCast"


def ours(path: Path) -> bool:
    """Only folders whose own name says SprigCast are ever deleted (D:\\Videos never is, nor C:\\Users\\SprigCastFan)."""
    return "sprigcast" in Path(path).name.lower()


def data_folders(recordings: Path | None = None) -> list[Path]:
    """SprigCast's folders on this PC (those that exist), plus the recordings folder when given and ours."""
    from PySide6.QtCore import QStandardPaths

    found: list[Path] = []
    for kind in (QStandardPaths.StandardLocation.AppConfigLocation, QStandardPaths.StandardLocation.AppDataLocation,
                 QStandardPaths.StandardLocation.AppLocalDataLocation):
        base = QStandardPaths.writableLocation(kind)
        if base:
            found.append(Path(base))
    docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    found.append(Path(docs or Path.home() / "Documents") / "SprigCast")
    if os.environ.get("APPDATA"):
        found.append(Path(os.environ["APPDATA"]) / "SprigCast")
    if os.environ.get("PROGRAMDATA"):
        found.append(Path(os.environ["PROGRAMDATA"]) / "SprigCast Camera")
    if recordings is not None:
        found.append(Path(recordings))
    unique: list[Path] = []
    for folder in found:
        if ours(folder) and folder.exists() and folder not in unique:
            unique.append(folder)
    return unique


def forget_secrets_and_settings() -> dict[str, int]:
    """Credential Manager, secret settings, key files, every setting, Start with Windows. Counts, for the log."""
    from PySide6.QtCore import QSettings

    from sprigcast.app import autostart
    from sprigcast.tools import forget_keys

    counts = {"credentials": 0, "secrets": 0, "files": 0}
    for name, step in (("credentials", forget_keys.forget), ("secrets", forget_keys.forget_settings),
                       ("files", forget_keys.forget_files)):
        try:
            counts[name] = step()
        except Exception:  # one step failing never stops the rest
            log.exception("clear data: %s", name)
    try:
        autostart.set_enabled(False)
    except OSError as exc:
        log.info("clear data: Start with Windows stays: %s", exc)
    settings = QSettings()
    settings.clear()
    settings.sync()
    return counts


def removal_script(folders: list[Path], pid: int) -> str:
    """A Windows batch file: wait for SprigCast (pid) to close, delete the folders and settings, then itself."""
    # ping waits a second: timeout needs a console to read from, and this one runs without a window
    lines = ["@echo off", "chcp 65001 >nul", ":wait",
             f'tasklist /fi "PID eq {pid}" /nh 2>nul | find " {pid} " >nul && (ping -n 2 127.0.0.1 >nul & goto wait)',
             "ping -n 3 127.0.0.1 >nul"]
    for folder in folders:
        lines.append(f'rmdir /s /q "{folder}" 2>nul')
    lines.append(f'reg delete "{SETTINGS_KEY}" /f >nul 2>nul')
    lines.append('del "%~f0"')
    return "\r\n".join(lines) + "\r\n"


def remove_after_exit(folders: list[Path]) -> None:
    """Delete the folders once SprigCast has closed (Windows), or now (elsewhere: nothing holds them)."""
    if sys.platform != "win32":
        import shutil

        for folder in folders:
            shutil.rmtree(folder, ignore_errors=True)
        return
    script = Path(tempfile.gettempdir()) / f"sprigcast-clear-{os.getpid()}.cmd"
    script.write_text(removal_script(folders, os.getpid()), encoding="utf-8")
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    for extra in (subprocess.CREATE_BREAKAWAY_FROM_JOB, 0):  # outlive SprigCast even when it runs in a job
        try:
            subprocess.Popen(["cmd.exe", "/c", str(script)], creationflags=flags | extra, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError as exc:
            log.info("clear data: couldn't start the clean-up (%s)", exc)


def clear_all(recordings: Path | None = None) -> list[Path]:
    """Forget secrets and settings now, and delete SprigCast's folders once it closes. Returns the folders."""
    folders = data_folders(recordings)
    from sprigcast.app import settings_keeper
    from sprigcast.app.main import config_dir

    settings_keeper.forget(config_dir())  # no copy left to bring settings back, and none made on the way out
    counts = forget_secrets_and_settings()
    log.info("clear data: %d credentials, %d secrets, %d key files gone; %d folders go when SprigCast closes",
             counts["credentials"], counts["secrets"], counts["files"], len(folders))
    remove_after_exit(folders)
    return folders


def ask(parent) -> tuple[bool, bool]:
    """The confirmation: (go ahead, also the recordings folder). Says plainly what goes and that SprigCast closes."""
    from PySide6.QtWidgets import QCheckBox

    from sprigcast.app.message import MessageCard

    box = MessageCard("Clear all my data",
                      "Delete everything SprigCast keeps on this PC?\n\n"
                      "•  Stream keys and sign-ins\n"
                      "•  Paired devices and the Control API password\n"
                      "•  All settings, gardens, paths and plugins\n"
                      "•  Logs, looks and exports in Documents\\SprigCast\n\n"
                      "SprigCast closes when it's done. This can't be undone.", parent, "warn", width=480)
    recordings = QCheckBox("Also delete my recordings, clips and screenshots")
    box.add_widget(recordings)
    go = box.add_button("Delete everything", danger=True)
    box.add_button("Cancel", default=True)  # Enter never deletes
    box.exec()
    return box.clicked is go, recordings.isChecked()
