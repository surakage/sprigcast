"""Screenshots: one PNG of what's on air in each shape, saved next to recordings (Screenshots folder).

The render thread hands over the next finished frame of each canvas; we wait for it here (a frame
comes every 16-33 ms, so this is quick) and save it as a PNG.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QImage

log = logging.getLogger(__name__)

WAIT_S = 0.5  # long enough for a frame even at a slow 10 fps


def grab(engine, canvas_ids: list[str], timeout: float = WAIT_S) -> dict[str, QImage]:
    """The next frame of each canvas as a QImage. A canvas that isn't drawing is left out."""
    got: dict[str, QImage] = {}
    done = threading.Event()
    lock = threading.Lock()
    taps = {}

    for canvas_id in canvas_ids:
        def tap(frame, canvas_id=canvas_id):
            if frame.fmt != "rgba":
                return
            image = QImage(frame.data, frame.width, frame.height, frame.width * 4,
                           QImage.Format.Format_RGBA8888_Premultiplied).copy()
            with lock:
                if canvas_id in got:
                    return
                got[canvas_id] = image
                if len(got) == len(canvas_ids):
                    done.set()

        taps[canvas_id] = tap
        engine.add_frame_tap(canvas_id, tap)
    try:
        done.wait(timeout)
    finally:
        for canvas_id, tap in taps.items():
            engine.remove_frame_tap(canvas_id, tap)
    with lock:
        return dict(got)


def shrink(image: QImage, width: int | None) -> QImage:
    if width and 0 < width < image.width():
        return image.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation)
    return image


def path_for(folder: Path, canvas_id: str, when: datetime | None = None) -> Path:
    when = when or datetime.now()
    base = f"screenshot {canvas_id} {when:%Y-%m-%d %H-%M-%S}"
    path, n = folder / f"{base}.png", 2
    while path.exists():
        path, n = folder / f"{base} ({n}).png", n + 1
    return path


def save(image: QImage, folder: Path, canvas_id: str) -> Path:
    """Into folder's folder for today (Screenshots/2026-11-15/...)."""
    from sprigcast.output.storage import day_folder

    path = path_for(day_folder(folder), canvas_id)
    if not image.convertToFormat(QImage.Format.Format_RGBA8888).save(str(path), "PNG"):
        raise OSError(f"Couldn't save {path}")
    return path


def png_base64(image: QImage) -> str:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.convertToFormat(QImage.Format.Format_RGBA8888).save(buffer, "PNG")
    return base64.b64encode(bytes(data)).decode("ascii")


def take(engine, canvas_ids: list[str], folder: Path | None, width: int | None = None,
         as_data: bool = False, images: dict | None = None) -> list[dict]:
    """Grab, then save each shape to folder (None: don't save) and/or return it as base64 PNG.
    images, if given, gets each shape's full-size QImage (for plugins like Snap)."""
    started = time.monotonic()
    out = []
    for canvas_id, image in grab(engine, canvas_ids).items():
        if images is not None:
            images[canvas_id] = image
        image = shrink(image, width)
        shot: dict = {"shape": canvas_id, "width": image.width(), "height": image.height()}
        if folder is not None:
            shot["path"] = str(save(image, folder, canvas_id))
        if as_data:
            shot["png"] = png_base64(image)
        out.append(shot)
    out.sort(key=lambda s: canvas_ids.index(s["shape"]))
    if out:
        log.info("screenshot %s in %.0f ms", ", ".join(s["shape"] for s in out),
                 (time.monotonic() - started) * 1000)
    return out
