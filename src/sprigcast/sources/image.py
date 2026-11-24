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
ART_PREFIX = "art:"  # "art:sloth-ending.png" is a picture that ships with SprigCast (app/art), wherever it's installed
ART_FOLDER = Path(__file__).resolve().parents[1] / "app" / "art"


def resolve(path: str) -> Path:
    """The file a `path` setting means: a SprigCast picture for "art:<name>", else the path itself."""
    if path.startswith(ART_PREFIX):
        return ART_FOLDER / Path(path[len(ART_PREFIX):]).name  # a bare name: never outside the art folder
    return Path(path)


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


DECODE_ASIDE = True  # a picture shown again is decoded on a worker (tests turn it off)


@register("image")
class ImageSource(Source):
    sleeps = True  # an image no scene on screen shows keeps no texture; it is decoded again when shown
    slow_wake = True  # a bloom switch waits the tick it takes to decode it again, so it never pops in late

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._size = _MISSING
        self._pending = True  # the compositor needs the pixels (again)
        self._decoding = None  # a Future: the picture being decoded on a worker after a wake (DECODE_ASIDE)
        self._measure()

    def _path(self) -> Path:
        return resolve(str(self.settings.get("path", "")))

    def _measure(self) -> None:
        self._size = image_size(self._path()) or _MISSING
        self._pending = True
        self._decoding = None  # another picture now: a decode of the old one is let go

    def update(self, settings: dict[str, Any]) -> None:
        old = self.settings.get("path")
        super().update(settings)
        if settings.get("path") != old:
            self._measure()
            if DECODE_ASIDE and not self.asleep:  # another picture by bloom: decoded off the render thread
                from sprigcast.core import workers

                self._decoding = workers.picture(load_rgba, self._path(), name="picture decode")

    def wake(self) -> None:
        self._pending = True  # the texture was dropped while nothing showed this image
        # Decoded again on a worker: a switch to a bloom full of pictures decoded them all in one frame on the PC
        # (2026-11-17 heavy run, "sources" 75-88 ms). Until it's there the switch keeps showing the old bloom
        # (slow_wake: Engine._still_waking waits for its texture), so it never pops in late.
        if DECODE_ASIDE:
            from sprigcast.core import workers

            self._decoding = workers.picture(load_rgba, self._path(), name="picture decode")

    def sleep(self) -> None:
        self._decoding = None  # a decode still running is let go when it ends

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    def video_frame(self) -> np.ndarray | None:
        if not self._pending:
            return None
        decoding = self._decoding
        if decoding is not None:
            if not decoding.done():
                return None
            self._decoding = None
            try:
                frame = decoding.result()
            except Exception:  # noqa: BLE001 (logged by the worker)
                frame = None
        elif DECODE_ASIDE:
            # Its first picture on a worker too (asked here, when something first shows it, so pictures nothing shows
            # are never decoded): decoded on the render thread, one picture held a frame 215.9 ms on a light PC
            # (Mikedog151, 2026-11-23).
            from sprigcast.core import workers

            self._decoding = workers.picture(load_rgba, self._path(), name="picture decode")
            return None
        else:
            frame = load_rgba(self._path())
        self._pending = False
        if frame is None:
            frame = _missing()
        self._size = (frame.shape[1], frame.shape[0])
        return frame  # the GPU texture keeps the pixels; no copy stays in RAM
