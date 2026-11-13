"""A still image from disk (PNG, JPEG, WebP, ...). Settings: path.

The file is only decoded when the renderer asks for its pixels, and they go straight to the GPU:
nothing is kept in RAM afterwards. An image no scene on screen shows isn't decoded at all (it
sleeps, see Source.sleep); only its size is read from the file's header.

Qt decodes it (already loaded for the window), so Pillow is only loaded for formats Qt can't read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.core.source import Source, register

_MISSING = (64, 64)


def _missing() -> np.ndarray:
    """A magenta checkerboard, so a missing or unreadable file is easy to spot."""
    w, h = _MISSING
    yy, xx = np.mgrid[0:h, 0:w]
    checker = ((xx // 8 + yy // 8) % 2).astype(bool)
    frame = np.zeros((h, w, 4), dtype=np.uint8)
    frame[..., 3] = 255
    frame[checker] = (255, 0, 255, 255)
    return frame


def _reader(path: Path):
    from PySide6.QtGui import QImageReader

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)  # honour a photo's rotation tag
    return reader


def image_size(path: Path) -> tuple[int, int] | None:
    """The picture's size from its header (no decoding), or None if it can't be read."""
    try:
        from PySide6.QtGui import QImageIOHandler

        reader = _reader(path)
        size = reader.size()
        if size.isValid():
            turned = reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90
            return (size.height(), size.width()) if turned else (size.width(), size.height())
    except ImportError:
        pass
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except (ImportError, OSError, ValueError):
        return None


def load_rgba(path: Path) -> np.ndarray | None:
    """The picture as H x W x 4 RGBA bytes, or None if it can't be read."""
    try:
        from PySide6.QtGui import QImage

        image = _reader(path).read()
        if not image.isNull():
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            w, h = image.width(), image.height()
            rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())
            return rows[:, : w * 4].reshape(h, w, 4).copy()
    except ImportError:
        pass
    try:
        from PIL import Image  # formats Qt has no plugin for

        with Image.open(path) as img:
            return np.asarray(img.convert("RGBA"), dtype=np.uint8).copy()
    except (ImportError, OSError, ValueError):
        return None


@register("image")
class ImageSource(Source):
    sleeps = True  # an image no scene on screen shows keeps no texture; it is decoded again when shown

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._size = _MISSING
        self._pending = True  # the compositor needs the pixels (again)
        self._measure()

    def _path(self) -> Path:
        return Path(str(self.settings.get("path", "")))

    def _measure(self) -> None:
        self._size = image_size(self._path()) or _MISSING
        self._pending = True

    def update(self, settings: dict[str, Any]) -> None:
        old = self.settings.get("path")
        super().update(settings)
        if settings.get("path") != old:
            self._measure()

    def wake(self) -> None:
        self._pending = True  # the texture was dropped while nothing showed this image

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def video_frame(self) -> np.ndarray | None:
        if not self._pending:
            return None
        self._pending = False
        frame = load_rgba(self._path())
        if frame is None:
            frame = _missing()
        self._size = (frame.shape[1], frame.shape[0])
        return frame  # the GPU texture keeps the pixels; no copy stays in RAM
