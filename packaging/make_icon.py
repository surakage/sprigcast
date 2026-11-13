"""Render data/logo.svg into a Windows .ico (16 to 256 px) for the installer and shortcuts."""

from __future__ import annotations

import io
import sys
from pathlib import Path


def main(out: str) -> None:
    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - Qt needs one for painting
    svg = Path(__file__).resolve().parents[1] / "src" / "sprigcast" / "data" / "logo.svg"
    renderer = QSvgRenderer(str(svg))
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    big = Image.open(io.BytesIO(bytes(data))).convert("RGBA")
    big.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sprigcast.ico")
