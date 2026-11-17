"""Tidy up after features taken out of SprigCast (docs/REVISIT.md), once per PC.

What they left behind on a PC that ran an earlier build:

* the models folder (<settings>/models): speech and background models that Live captions and the
  Background remover downloaded, 100 MB or more. Nothing else used it.
* settings: Live captions' (captions/..., including a Google key), Wave Link sync's (wavelink/...),
  Kick chat's (kick/pusher_key, and the Kick name in Chat's channel names), Auto clip's (auto_clip.json).
* Auto clip's unkept suggestions: the clips listed in <recordings>/Suggested clips/suggested.json
  (clean_up_recordings, once the recordings folder is known). Kept ones had moved to Clips already.

Added store plugin copies are deleted by plugin_store.refresh_installed (RETIRED), and garden filters of
removed kinds are dropped as gardens load (core/scene.py RETIRED_FILTER_KINDS).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

DONE = "cleanup/removed_features"  # bump VERSION when another feature is taken out
VERSION = 2  # 2: Auto clip
FILES = ("auto_clip.json",)  # in the settings folder
SUGGESTED_DONE = "cleanup/suggested_clips"
GROUPS = ("captions", "wavelink")
KEYS = ("kick/pusher_key",)
CHAT_NAMES = "chat/names"


def clean_up(config: Path, settings=None) -> list[str]:
    """Remove what removed features left behind, once. Returns what was removed (for the log and tests)."""
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    try:
        if int(settings.value(DONE, 0) or 0) >= VERSION:
            return []
    except (TypeError, ValueError):
        pass
    removed = []
    models = Path(config) / "models"
    if models.is_dir():
        shutil.rmtree(models, ignore_errors=True)
        if not models.exists():
            removed.append("models folder")
    for group in GROUPS:
        keys = [k for k in settings.allKeys() if k.startswith(f"{group}/")]
        for key in keys:
            settings.remove(key)
        if keys:
            removed.append(f"{group} settings")
    for name in FILES:
        path = Path(config) / name
        if path.is_file():
            try:
                path.unlink()
                removed.append(name)
            except OSError as exc:
                log.info("couldn't delete %s: %s", path, exc)
    for key in KEYS:
        if settings.contains(key):
            settings.remove(key)
            removed.append(key)
    try:
        names = json.loads(str(settings.value(CHAT_NAMES, "") or "{}"))
    except ValueError:
        names = {}
    if isinstance(names, dict) and "kick" in names:
        settings.setValue(CHAT_NAMES, json.dumps({k: v for k, v in names.items() if k != "kick"}))
        removed.append("Kick chat name")
    settings.setValue(DONE, VERSION)
    if removed:
        log.info("tidied up after removed features: %s", ", ".join(removed))
    return removed


def clean_up_recordings(recordings: Path, settings=None) -> list[Path]:
    """Delete Auto clip's unkept suggestions, once: only the clips its suggested.json lists, the list itself,
    and folders left empty. Anything else someone put in "Suggested clips" stays. Returns what was deleted."""
    if settings is None:
        from PySide6.QtCore import QSettings

        settings = QSettings()
    if settings.value(SUGGESTED_DONE, False, type=bool):
        return []
    folder = Path(recordings) / "Suggested clips"
    index = folder / "suggested.json"
    gone: list[Path] = []
    try:
        data = json.loads(index.read_text(encoding="utf-8"))
        items = data.get("clips", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        items = []
    root = folder.resolve()
    for item in items:
        for name in (item.get("files", []) if isinstance(item, dict) else []):
            path = Path(str(name))
            try:
                if path.resolve().is_relative_to(root) and path.is_file():  # never anything outside the folder
                    path.unlink()
                    gone.append(path)
            except OSError as exc:  # open in a player: left where it is
                log.info("couldn't delete %s: %s", path, exc)
    if index.is_file():
        try:
            index.unlink()
            gone.append(index)
        except OSError:
            pass
    if folder.is_dir():
        for sub in sorted((p for p in folder.rglob("*") if p.is_dir()), reverse=True):
            try:
                sub.rmdir()  # only when empty
            except OSError:
                pass
        try:
            folder.rmdir()
        except OSError:
            pass
    settings.setValue(SUGGESTED_DONE, True)
    if gone:
        log.info("tidied up after Auto clip: %d files", len(gone))
    return gone
