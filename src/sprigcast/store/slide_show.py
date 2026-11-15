"""Slide show: the pictures in a folder, one after another.

Settings: folder, seconds (each picture's time), shuffle, loop (start over after the last; off stops
on it), width and height (the box every picture is fitted into, so slides of any size don't make
the leaf jump around; the rest of the box is see-through).

Only the picture on screen is decoded, and only when it changes; a hidden slide show sleeps.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register
from sprigcast.sources.image import load_rgba

PLUGIN = {"id": "slide_show", "name": "Slide show", "version": 1, "adds": "Source",
          "about": "Plays every picture in a folder, one after another: seconds per picture, shuffle and loop."}

PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def pictures_in(folder: str) -> list[Path]:
    """The pictures in a folder, by name (case doesn't matter); none if it can't be read."""
    try:
        files = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in PICTURES]
    except OSError:
        return []
    return sorted(files, key=lambda p: p.name.lower())


def fit(picture: np.ndarray, box: tuple[int, int]) -> np.ndarray:
    """The picture scaled to fit the box (no stretching), centred on a see-through box of that size."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    bw, bh = max(1, box[0]), max(1, box[1])
    h, w = picture.shape[:2]
    scale = min(bw / w, bh / h)
    sw, sh = max(1, round(w * scale)), max(1, round(h * scale))
    image = QImage(np.ascontiguousarray(picture).data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    image = image.scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation).convertToFormat(QImage.Format.Format_RGBA8888)
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(sh, image.bytesPerLine())[:, : sw * 4]
    out = np.zeros((bh, bw, 4), dtype=np.uint8)
    x, y = (bw - sw) // 2, (bh - sh) // 2
    out[y:y + sh, x:x + sw] = rows.reshape(sh, sw, 4)
    return out


@register("slideshow")
class SlideShowSource(Source):
    label = "Slide show"
    defaults = {"folder": "", "seconds": 5.0, "shuffle": False, "loop": True, "width": 1920, "height": 1080}
    sleeps = True

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._order: list[Path] = []
        self._index = -1
        self._next_at = 0.0
        self._pending = True
        self.status = "starting"
        self._load_folder()

    def _box(self) -> tuple[int, int]:
        return int(self.settings.get("width", 1920) or 1920), int(self.settings.get("height", 1080) or 1080)

    @property
    def size(self) -> tuple[int, int]:
        return self._box()

    def _load_folder(self) -> None:
        self._order = pictures_in(str(self.settings.get("folder", "")))
        if self.settings.get("shuffle"):
            random.shuffle(self._order)
        self._index = -1
        self._next_at = 0.0
        self._pending = True
        self.status = "ok" if self._order else ("no folder" if not self.settings.get("folder") else "no pictures")

    def update(self, settings: dict[str, Any]) -> None:
        changed = any(settings.get(k) != self.settings.get(k) for k in ("folder", "shuffle", "width", "height"))
        super().update(settings)
        if changed:
            self._load_folder()

    def wake(self) -> None:
        self._pending = True  # the texture went while hidden: show the current slide again

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        if not self._order:
            if self._pending:
                self._pending = False
                return np.zeros((2, 2, 4), dtype=np.uint8)
            return None
        last = self._index >= len(self._order) - 1
        if self._index >= 0 and now < self._next_at and not self._pending:
            return None
        if self._index < 0 or (now >= self._next_at and not (last and not self.settings.get("loop", True))):
            self._index = (self._index + 1) % len(self._order)
            if self._index == 0 and self.settings.get("shuffle") and len(self._order) > 1:
                random.shuffle(self._order)
            self._next_at = now + max(0.5, float(self.settings.get("seconds", 5.0) or 5.0))
        elif not self._pending:
            return None
        self._pending = False
        picture = load_rgba(self._order[self._index])
        if picture is None:
            return None
        return fit(picture, self._box())
