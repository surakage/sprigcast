"""Slide show: the pictures in a folder, one after another.

Settings: folder, seconds (each picture's time), shuffle, loop (start over after the last; off stops
on it), width and height (the box every picture is fitted into, so slides of any size don't make
the leaf jump around; the rest of the box is see-through).

Only the picture on screen and the next one are decoded: the next one in the background, while
the current one shows, so a big photo never holds up the canvas. A hidden slide show sleeps.
Added from + → Plugins it fills the canvas.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register
from sprigcast.sources.image import load_rgba

PLUGIN = {"id": "slide_show", "name": "Slide show", "version": 4, "adds": "Source",
          "about": "Plays every picture in a folder, one after another: seconds per picture, shuffle and loop.",
          "new": "Smoother: even the first picture is made in the background."}

log = logging.getLogger(__name__)

PICTURES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
# SprigCast's own screenshots and Snap polaroids. Played back on the canvas they picture the canvas, which
# held the picture before, so every slide stacked older copies of the stream (soak 2026-11-15): left out.
OWN_SHOTS = re.compile(r"^(screenshot \S+|snap) \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}( \(\d+\))?\.png$", re.IGNORECASE)


def pictures_in(folder: str) -> list[Path]:
    """The pictures in a folder, by name (case doesn't matter); none if it can't be read."""
    try:
        files = [p for p in Path(folder).iterdir()
                 if p.is_file() and p.suffix.lower() in PICTURES and not OWN_SHOTS.match(p.name)]
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


def prepare(path: Path, box: tuple[int, int]) -> np.ndarray | None:
    """Decode and fit one picture (on a worker thread). Pillow lets go of Python's lock while it
    decodes and scales, so the canvas and the encoders keep running. Falls back to Qt."""
    try:
        from PIL import Image, ImageOps

        bw, bh = max(1, box[0]), max(1, box[1])
        with Image.open(path) as img:
            img.draft("RGB", (bw, bh))  # a JPEG decodes straight at a smaller size when it can
            img = ImageOps.exif_transpose(img).convert("RGBA")
            scale = min(bw / img.width, bh / img.height)
            size = max(1, round(img.width * scale)), max(1, round(img.height * scale))
            if size != img.size:
                img = img.resize(size, Image.Resampling.LANCZOS)
            out = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
            out.paste(img, ((bw - size[0]) // 2, (bh - size[1]) // 2))
            return np.asarray(out, dtype=np.uint8)
    except Exception:
        log.debug("slide show: Pillow couldn't read %s", path, exc_info=True)
    picture = load_rgba(path)
    return None if picture is None else fit(picture, box)


@register("slideshow")
class SlideShowSource(Source):
    label = "Slide show"
    defaults = {"folder": "", "seconds": 5.0, "shuffle": False, "loop": True, "width": 1920, "height": 1080}
    sleeps = True
    fills_canvas = True  # added from + → Plugins it starts at 0, 0 the size of the canvas

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._order: list[Path] = []
        self._index = -1
        self._next_at = 0.0
        self._pending = True
        self.status = "starting"
        self._lock = threading.Lock()
        self._ready: dict[tuple, np.ndarray | None] = {}  # (path, box) -> fitted picture, made ahead
        self._making: set[tuple] = set()
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
        self._first_shown = False
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
        want = (self._order[self._index], self._box())
        picture = self._take(want)
        if picture is None:
            return None  # still being made: keep showing the last one a moment longer
        self._pending = False
        if len(self._order) > 1:  # make the next one while this one shows
            self._make((self._order[(self._index + 1) % len(self._order)], want[1]))
        return picture

    def _take(self, want: tuple) -> np.ndarray | None:
        with self._lock:
            if want in self._ready:
                picture = self._ready.pop(want)
                self._ready.clear()
                if picture is None:
                    self.status = f"can't read {want[0].name}"
                    return np.zeros((2, 2, 4), dtype=np.uint8)  # see-through for its turn, then the next
                self.status = "ok"
                return picture
            # Even the very first picture is made on a worker: made here it held the canvas up (149 ms for a
            # big photo on 2026-11-16). The slide shows a moment later instead.
            self._first_shown = True
            self._make_locked(want)
            return None

    def _make(self, want: tuple) -> None:
        with self._lock:
            self._make_locked(want)

    def _make_locked(self, want: tuple) -> None:
        if want in self._making or want in self._ready:
            return
        self._making.add(want)

        def run():
            picture = prepare(*want)
            with self._lock:
                self._making.discard(want)
                self._ready[want] = picture

        from sprigcast.core import workers

        workers.run(run, name="slide-show-next")
