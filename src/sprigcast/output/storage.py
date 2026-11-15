"""Disk space and tidy saves: warn before the recordings drive fills up, and keep screenshots and clips in
a folder per day, optionally only the newest N of them."""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

GB = 1024 ** 3
LOW_MINUTES = 30  # warn when a recording has less room than this left
VERY_LOW_MINUTES = 5  # and again here
FLOOR_BYTES = 2 * GB  # always leave Windows this much, whatever the bitrate
SCREENSHOT_TYPES = (".png", ".jpg", ".jpeg")
CLIP_TYPES = (".mkv", ".mp4", ".mov", ".flv")


def free_bytes(folder: Path) -> int | None:
    """Free space on the drive that holds folder (or would, once made). None when it can't be read."""
    path = Path(folder)
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def minutes_left(free: int, kbps: int) -> float:
    """How long a recording at kbps (all files together) can run before only FLOOR_BYTES are left."""
    bytes_per_minute = max(1, kbps) * 1000 / 8 * 60
    return max(0.0, (free - FLOOR_BYTES) / bytes_per_minute)


def size_text(n: int) -> str:
    if n >= GB:
        return f"{n / GB:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{n / 1024:.0f} KB"


def time_text(minutes: float) -> str:
    if minutes >= 120:
        return f"about {minutes / 60:.0f} hours"
    if minutes >= 60:
        return f"about {int(minutes // 60)} h {int(minutes % 60)} min"
    return f"about {max(0, int(minutes))} min"


def space_warning(free: int | None, kbps: int) -> str:
    """One short line when the drive can't hold LOW_MINUTES more of recording, else ''."""
    if free is None:
        return ""
    left = minutes_left(free, kbps)
    if left >= LOW_MINUTES:
        return ""
    if left < 1:
        return f"Your recordings drive is full ({size_text(free)} free). Free up space before recording."
    return f"Low disk space: room for {time_text(left)} of recording ({size_text(free)} free)."


class SpaceWatch:
    """While recording: warns once under LOW_MINUTES and once more under VERY_LOW_MINUTES."""

    def __init__(self, folder: Path, kbps: int, free=free_bytes):
        self.folder, self.kbps, self._free = folder, kbps, free
        self.warned: set[int] = set()
        self.left: float | None = None  # minutes of recording the drive still holds, at the last check

    def check(self) -> str:
        free = self._free(self.folder)
        if free is None:
            return ""
        left = self.left = minutes_left(free, self.kbps)
        for level in (VERY_LOW_MINUTES, LOW_MINUTES):
            if left < level and level not in self.warned:
                self.warned.update(lv for lv in (VERY_LOW_MINUTES, LOW_MINUTES) if lv >= level)
                return space_warning(free, self.kbps)
        return ""


def rec_note(left: float | None) -> str:
    """A few words for the REC line once space is short ('' while there's plenty)."""
    if left is None or left >= LOW_MINUTES:
        return ""
    return f"low disk: {time_text(left).replace('about ', '')} left"


def folder_size(folder: Path) -> int:
    """Bytes in folder and everything under it (slow on huge folders: call it off the UI thread)."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def day_folder(folder: Path, when: datetime | None = None) -> Path:
    """folder/2026-11-15, made if missing."""
    path = Path(folder) / (when or datetime.now()).strftime("%Y-%m-%d")
    path.mkdir(parents=True, exist_ok=True)
    return path


def keep_newest(folder: Path, keep: int, types: tuple[str, ...]) -> list[Path]:
    """Delete all but the newest `keep` files of these types under folder (0 keeps everything), then any
    day folders left empty. Returns what was deleted."""
    if keep <= 0 or not Path(folder).is_dir():
        return []
    files = []
    for path in Path(folder).rglob("*"):
        if path.is_file() and path.suffix.lower() in types:
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                pass
    files.sort(reverse=True)
    gone = []
    for _mtime, path in files[keep:]:
        try:
            path.unlink()
            gone.append(path)
        except OSError as exc:  # open in another app: try again next time
            log.info("couldn't tidy %s: %s", path, exc)
    for sub in sorted((p for p in Path(folder).iterdir() if p.is_dir()), reverse=True):
        try:
            sub.rmdir()  # only works when empty
        except OSError:
            pass
    if gone:
        log.info("tidied %d old files from %s", len(gone), folder)
    return gone
