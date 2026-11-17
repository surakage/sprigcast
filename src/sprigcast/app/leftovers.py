"""Tidy up after features taken out of SprigCast (docs/REVISIT.md), once per PC.

What they left behind on a PC that ran an earlier build:

* the models folder (<settings>/models): speech and background models that Live captions and the
  Background remover downloaded, 100 MB or more. Nothing else used it.
* settings: Live captions' (captions/..., including a Google key), Wave Link sync's (wavelink/...),
  Kick chat's (kick/pusher_key, and the Kick name in Chat's channel names).

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
VERSION = 1
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
