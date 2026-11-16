"""Screenshots: one PNG of what's on air in each shape, saved next to recordings (Screenshots folder).

The render thread hands over the next finished frame of each canvas (its bytes as they are, no copy on
the render thread); we wait for it here (a frame comes every 16-33 ms) and save it as a PNG. From the
buttons, all of that runs on a shared worker thread (take_in_background), so the window never waits on
a PNG encode (50-200 ms at 1080p).
"""

from __future__ import annotations

import base64
from collections.abc import Callable
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, Qt, Signal
from PySide6.QtGui import QImage

from sprigcast.core.quiet import Every

log = logging.getLogger(__name__)

SLOW_SHOT_MS = 250.0
_SHOT_LOG = Every(3600)

WAIT_S = 0.5  # long enough for a frame even at a slow 10 fps
# Qt's PNG "quality" is zlib's effort backwards: 50 is about half the time of the default at 1080p
# (80 ms against 160), for files a little larger.
PNG_QUALITY = 50


def grab(engine, canvas_ids: list[str], timeout: float = WAIT_S) -> dict[str, QImage]:
    """The next frame of each canvas as a QImage. A canvas that isn't drawing is left out. Sources that are
    only for show (Snap's polaroid) are left out of it."""
    got: dict = {}
    done = threading.Event()
    lock = threading.Lock()
    taps = {}

    for canvas_id in canvas_ids:
        def tap(frame, canvas_id=canvas_id):
            if frame.fmt != "rgba":
                return
            with lock:  # the frame's bytes are its own (a fresh read each frame): kept, not copied, here
                if canvas_id in got:
                    return
                got[canvas_id] = frame
                if len(got) == len(canvas_ids):
                    done.set()

        taps[canvas_id] = tap
        engine.add_frame_tap(f"shot:{canvas_id}", tap)  # what's on air, without Snap's polaroid (see Engine)
    try:
        done.wait(timeout)
    finally:
        for canvas_id, tap in taps.items():
            engine.remove_frame_tap(f"shot:{canvas_id}", tap)
    with lock:
        frames = dict(got)
    return {canvas_id: QImage(f.data, f.width, f.height, f.width * 4,
                              QImage.Format.Format_RGBA8888_Premultiplied).copy()
            for canvas_id, f in frames.items()}


def grab_one(engine, source_id: str, timeout: float = WAIT_S * 2) -> QImage | None:
    """The next picture of one source (with its filters) on its own, or None if it isn't drawing."""
    got: list = []
    done = threading.Event()

    def take(picture):
        got.append(picture)
        done.set()

    engine.grab_source(source_id, take)
    done.wait(timeout)
    picture = got[0] if got else None
    if picture is None or getattr(picture, "ndim", 0) != 3 or not picture.shape[0] or not picture.shape[1]:
        return None
    h, w = picture.shape[:2]
    data = picture.tobytes()
    return QImage(data, w, h, w * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy()


def take_one(engine, source_id: str, name: str, folder: Path | None, width: int | None = None,
             as_data: bool = False) -> dict | None:
    """Like take, for one source: saved as "screenshot <name> <time>.png" and/or returned as base64 PNG."""
    image = grab_one(engine, source_id)
    if image is None:
        return None
    image = shrink(image, width)
    shot: dict = {"width": image.width(), "height": image.height()}
    if folder is not None:
        safe = "".join(c if c.isalnum() or c in " -_()" else "_" for c in name).strip() or "source"
        shot["path"] = str(save(image, folder, safe))
    if as_data:
        shot["png"] = png_base64(image)
    return shot


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
    if not image.convertToFormat(QImage.Format.Format_RGBA8888).save(str(path), "PNG", PNG_QUALITY):
        raise OSError(f"Couldn't save {path}")
    return path


def png_base64(image: QImage) -> str:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.convertToFormat(QImage.Format.Format_RGBA8888).save(buffer, "PNG", PNG_QUALITY)
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
        ms = (time.monotonic() - started) * 1000
        if ms >= SLOW_SHOT_MS or _SHOT_LOG.allow():  # one a minute was 618 lines a day: now hourly unless slow
            log.info("screenshot %s in %.0f ms%s", ", ".join(s["shape"] for s in out), ms, _SHOT_LOG.skipped())
    return out


class _Deliver(QObject):
    done = Signal(object, object, object)  # callback, shots, images


_deliver: _Deliver | None = None


def take_in_background(engine, canvas_ids: list[str], folder: Path | None, width: int | None,
                       on_done: Callable[[list[dict], dict], None]) -> None:
    """take() on a shared worker; on_done(shots, images) then runs on the UI thread. Call from the UI thread."""
    global _deliver
    from sprigcast.core import workers

    if _deliver is None:
        _deliver = _Deliver()
        _deliver.done.connect(lambda callback, shots, images: callback(shots, images))
    deliver = _deliver

    def job():
        images: dict = {}
        try:
            shots = take(engine, canvas_ids, folder, width, False, images)
        except OSError as exc:
            log.warning("screenshot failed: %s", exc)
            shots = []
        deliver.done.emit(on_done, shots, images)
    workers.run(job, name="screenshot")
