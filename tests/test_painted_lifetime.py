"""A painted source's picture keeps its own bytes alive (core/painted.Frame).

PC run 2026-11-18 (build 44205709484): on the native engine, Python's compositor gets pictures held back for it
later; a Stream timer's held-back picture was a view of a QImage the next paint had already let go, and
glBufferData read freed memory (an access violation every frame, then the graphics restarted)."""

import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from sprigcast.core import painted  # noqa: E402


def _image(colour: str) -> QImage:
    image = QImage(40, 10, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(colour))
    return image


def test_a_held_back_picture_outlives_the_next_paint_and_release():
    frame = painted.Frame()
    first = frame(_image("#112233"))
    held = first[2:, 3:]  # a view of a view, as the render thread may keep
    second = frame(_image("#445566"))
    frame.release()
    del second
    gc.collect()
    for _ in range(50):  # churn the allocator: freed memory would be reused here
        _image("#ffffff").fill(0)
        np.full(40 * 10 * 4, 7, dtype=np.uint8)
    assert first.shape == (10, 40, 4)
    assert tuple(held[0, 0]) == (0x33, 0x22, 0x11, 0xFF)  # B, G, R, A: still the first picture
    assert np.ascontiguousarray(held).sum() > 0
