"""Copies of a garden file, so one that can't be read never costs you your setup.

Every save keeps the file it replaces as ``<name>.bak.json`` (the last good save), and the first save of
each day also keeps a dated copy in ``backups/`` next to it (the newest DAYS of them). When a garden can't
be read, load_collection() opens the newest copy that can, and says which.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)

DAYS = 7  # dated copies kept per garden
FOLDER = "backups"


def last_good(path: Path) -> Path:
    return path.with_name(f"{path.stem}.bak{path.suffix}")


def dated(path: Path, day: date | None = None) -> Path:
    return path.parent / FOLDER / f"{path.stem} {(day or date.today()):%Y-%m-%d}{path.suffix}"


def keep(path: Path, today: date | None = None) -> None:
    """Called just before `path` is replaced: keep what it holds now. Never raises (a backup is a bonus)."""
    if not path.exists():
        return
    try:
        if path.stat().st_size == 0:
            return  # nothing worth keeping (and it would replace a good copy)
        shutil.copyfile(path, last_good(path))
        daily = dated(path, today)
        if not daily.exists():
            daily.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, daily)
            prune(path)
    except OSError as exc:
        log.warning("could not back up %s: %s", path.name, exc)


def copies(path: Path) -> list[Path]:
    """The dated copies of this garden, newest first."""
    folder = path.parent / FOLDER
    if not folder.is_dir():
        return []
    found = [p for p in folder.glob(f"{path.stem} ????-??-??{path.suffix}")]
    return sorted(found, key=lambda p: p.stem[-10:], reverse=True)


def prune(path: Path, keep_days: int = DAYS) -> None:
    for old in copies(path)[keep_days:]:
        try:
            old.unlink()
        except OSError:
            pass


def candidates(path: Path) -> list[Path]:
    """Where to look, best first, when `path` can't be read: the last good save, then each day's copy."""
    first = last_good(path)
    return ([first] if first.exists() else []) + copies(path)


def when(copy: Path) -> str:
    """"yesterday's copy" style words for a pop-up: the time the copy was saved."""
    try:
        stamp = datetime.fromtimestamp(copy.stat().st_mtime)
    except OSError:
        return "an earlier copy"
    return f"the copy saved {stamp:%b} {stamp.day}, {stamp:%I:%M %p}".replace(" 0", " ")
