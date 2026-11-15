"""Helpers for sources that draw their frames with QPainter (store plugins like Snap and Death count).

PySide keeps Python's lock (the GIL) for the whole of every QPainter call, so while a source paints,
no other Python thread runs: not the encoders, not the sound. The 60-minute soak showed what that
costs: a death count drawing its SVGs from scratch every frame missed render ticks and starved the
recording's encoder into dropping frames. So:

- art(): an SVG is drawn to a picture once per size and reused; drawing a picture is cheap.
- Frame: hands the painted QImage to the GPU exactly as QPainter left it (premultiplied BGRA, "pBGRA"),
  without a conversion or a NumPy copy; the compositor draws it like a sprig's texture.
- Stills: once a show stops moving (a held polaroid, a gravestone standing still) a source returns
  None, so nothing is painted or uploaded until it moves again.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

_ART: dict[tuple, object] = {}  # (file, w, h) -> QImage
_SVG: dict[Path, bytes] = {}
MOST_ART = 64


def art(path: Path, width: float, height: float):
    """The SVG at path drawn into a see-through picture of this size (cached)."""
    from PySide6.QtCore import QByteArray, QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    w, h = max(1, round(width)), max(1, round(height))
    key = (path, w, h)
    image = _ART.get(key)
    if image is not None:
        return image
    data = _SVG.get(path)
    if data is None:
        data = _SVG[path] = path.read_bytes()
    image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer = QSvgRenderer(QByteArray(data))
    renderer.render(p, QRectF(0, 0, w, h))
    p.end()
    del renderer
    if len(_ART) >= MOST_ART:
        _ART.pop(next(iter(_ART)))
    _ART[key] = image
    return image


def forget_art() -> None:
    """Let the cached art go (when the show ends); it's drawn again the next time it's needed."""
    _ART.clear()
    _SVG.clear()


def draw(p, path: Path, rect, sharp_at: float = 1.0) -> None:
    """Draw the SVG into rect (painter units). sharp_at: how many pixels one unit covers at most."""
    from PySide6.QtCore import QRectF

    image = art(path, rect.width() * sharp_at, rect.height() * sharp_at)
    p.drawImage(rect, image, QRectF(0, 0, image.width(), image.height()))


class Frame:
    """Turns painted QImages into frames for the GPU, keeping each one alive until the next."""

    # QImage's ARGB32 is B, G, R, A in memory on Windows and Linux PCs; "p": colours already multiplied
    # by alpha, the way QPainter paints. Converting to straight alpha cost a full copy of every frame.
    channel_order = "pBGRA"

    def __init__(self):
        self._held = None

    def __call__(self, image) -> np.ndarray:
        from PySide6.QtGui import QImage

        if image.format() != QImage.Format.Format_ARGB32_Premultiplied:
            image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
        w, h = image.width(), image.height()
        rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())[:, : w * 4]
        self._held = image  # the array is a view of it: it has to outlive the upload
        return rows.reshape(h, w, 4)

    def release(self) -> None:
        self._held = None


def clear_frame(w: int, h: int) -> np.ndarray:
    return np.zeros((h, w, 4), dtype=np.uint8)
