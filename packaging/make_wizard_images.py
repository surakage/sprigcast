"""Draw the installer's brand art as BMPs, at each size Inno Setup picks from for the screen's scaling.

side-*.bmp: the tall picture on the Welcome and Finish pages (the mascot on Night Canopy).
top-*.bmp: the small square at the top right of every other page (the app icon on the header colour).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MASCOT = ROOT / "brand" / "sprigcast-sloth.svg"
ICON = ROOT / "src" / "sprigcast" / "data" / "logo.svg"
MARK = ROOT / "brand" / "sprigcast-s-white-pink.svg"

# Night Canopy (app/theme.py). HEADER must match the header colour set in sprigcast.iss.
HEADER = "#0e1311"
DEEP = "#0b0f0d"
CANOPY = "#1d3428"
TEXT = "#f2f7f3"
MUTED = "#8b9a92"
ACCENT = "#4fd88f"

# The sizes Inno Setup 6 lists for 100% to 250% scaling.
SIDE_SIZES = [(164, 314), (192, 386), (246, 459), (273, 556), (328, 604), (355, 700), (410, 797)]
TOP_SIZES = [(55, 55), (64, 68), (83, 80), (92, 97), (110, 106), (119, 123), (138, 140)]


def _side(w: int, h: int):
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QImage, QLinearGradient, QPainter, QRadialGradient
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    fall = QLinearGradient(0, 0, 0, h)
    fall.setColorAt(0.0, QColor(DEEP))
    fall.setColorAt(1.0, QColor(CANOPY))
    p.fillRect(0, 0, w, h, fall)
    # A soft glow behind the mascot.
    glow = QRadialGradient(QPointF(w / 2, h * 0.70), w * 0.62)
    glow.setColorAt(0.0, QColor(79, 216, 143, 46))
    glow.setColorAt(1.0, QColor(79, 216, 143, 0))
    p.fillRect(0, 0, w, h, glow)

    s = w / 164  # 1 at 100% scaling
    mark = w * 0.30
    QSvgRenderer(str(MARK)).render(p, QRectF((w - mark) / 2, 30 * s, mark, mark))
    font = QFont("Segoe UI Semibold")
    font.setPixelSize(round(20 * s))
    p.setFont(font)
    p.setPen(QColor(TEXT))
    top = 30 * s + mark + 8 * s
    p.drawText(QRectF(0, top, w, 28 * s), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "SprigCast")
    small = QFont("Segoe UI")
    small.setPixelSize(round(11 * s))
    p.setFont(small)
    p.setPen(QColor(MUTED))
    p.drawText(QRectF(0, top + 26 * s, w, 18 * s), Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, "Open-source streaming")

    sloth = w * 0.84
    QSvgRenderer(str(MASCOT)).render(p, QRectF((w - sloth) / 2, h - sloth - 22 * s, sloth, sloth))
    p.end()
    return image


def _top(w: int, h: int):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor(HEADER))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = min(w, h) * 0.86
    QSvgRenderer(str(ICON)).render(p, QRectF((w - side) / 2, (h - side) / 2, side, side))
    p.end()
    return image


def _save_bmp(image, path: Path) -> None:
    from PIL import Image
    from PySide6.QtGui import QImage

    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    data = bytes(rgb.constBits())[: rgb.sizeInBytes()]
    Image.frombuffer("RGB", (rgb.width(), rgb.height()), data, "raw", "RGB", rgb.bytesPerLine(), 1).save(path, "BMP")


def main(out: str) -> None:
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - Qt needs one for painting
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    for w, h in SIDE_SIZES:
        _save_bmp(_side(w, h), folder / f"side-{w}x{h}.bmp")
    for w, h in TOP_SIZES:
        _save_bmp(_top(w, h), folder / f"top-{w}x{h}.bmp")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "wizard")
