"""Render data/logo.svg into a Windows .ico for the installer and shortcuts.

Every size is drawn straight from the vector (never scaled up from another size), on a
transparent background, and the small sizes get a light edge sharpen so the S stays crisp on
the desktop and in the taskbar.
"""

from __future__ import annotations

import sys
from pathlib import Path

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
SVG = Path(__file__).resolve().parents[1] / "src" / "sprigcast" / "data" / "logo.svg"


def render(size: int):
    """One icon size as a PIL RGBA image, drawn from the vector at that size."""
    from PIL import Image, ImageFilter
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    QGuiApplication.instance() or QGuiApplication([])  # Qt needs one for painting
    image = QImage(size, size, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(SVG)).render(painter, QRectF(0, 0, size, size))
    painter.end()
    data = bytes(image.constBits())[: image.sizeInBytes()]
    out = Image.frombuffer("RGBA", (size, size), data, "raw", "RGBA", image.bytesPerLine(), 1).copy()
    if size <= 48:  # tighten the edge only; sharpening the colour would leave a dark fringe on dark wallpaper
        r, g, b, a = out.split()
        out = Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.UnsharpMask(radius=0.5, percent=60, threshold=0))))
    return out


def main(out: str) -> None:
    images = [render(size) for size in SIZES]
    big = images[-1]
    big.save(out, format="ICO", sizes=[(s, s) for s in SIZES], append_images=images[:-1])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sprigcast.ico")
