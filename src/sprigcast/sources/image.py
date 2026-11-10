"""A still image from disk (PNG, JPEG, WebP, ...). Settings: path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from sprigcast.core.source import Source, register

_MISSING = (64, 64)


@register("image")
class ImageSource(Source):
    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame: np.ndarray | None = None
        self._pending = True
        self._load()

    def update(self, settings: dict[str, Any]) -> None:
        old = self.settings.get("path")
        super().update(settings)
        if settings.get("path") != old:
            self._load()

    def _load(self) -> None:
        path = Path(str(self.settings.get("path", "")))
        try:
            with Image.open(path) as img:
                self._frame = np.asarray(img.convert("RGBA"), dtype=np.uint8).copy()
        except (OSError, ValueError):
            # Missing or unreadable file: show a magenta checkerboard so the problem is visible.
            w, h = _MISSING
            yy, xx = np.mgrid[0:h, 0:w]
            checker = ((xx // 8 + yy // 8) % 2).astype(bool)
            frame = np.zeros((h, w, 4), dtype=np.uint8)
            frame[..., 3] = 255
            frame[checker] = (255, 0, 255, 255)
            self._frame = frame
        self._pending = True

    @property
    def size(self) -> tuple[int, int]:
        if self._frame is None:
            return _MISSING
        h, w = self._frame.shape[:2]
        return w, h

    def video_frame(self) -> np.ndarray | None:
        if not self._pending:
            return None
        self._pending = False
        return self._frame
