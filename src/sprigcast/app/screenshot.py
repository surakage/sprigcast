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
    only for show (Snap's polaroid) are left out of it.

    Only a frame from a tick that started after this call counts: one already being drawn may have begun
    before a freeze asked for just now (Snap's), and the picture must be the frozen one exactly."""
    native_shot = getattr(engine, "native_shot", None)
    read = {}
    for canvas_id in canvas_ids if native_shot is not None else ():
        shot = native_shot(canvas_id)  # the native engine's own canvas: Python's compositor needn't draw it
        if shot is not None:
            data, w, h = shot
            read[canvas_id] = QImage(data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    canvas_ids = [c for c in canvas_ids if c not in read]
    if not canvas_ids:
        return read
    got: dict = {}
    since = getattr(engine, "_ticks", None)  # taps run inside a tick: frames from ticks after this one count
    done = threading.Event()
    lock = threading.Lock()
    taps = {}

    for canvas_id in canvas_ids:
        def tap(frame, canvas_id=canvas_id):
            if frame.fmt != "rgba" or (since is not None and engine._ticks <= since):
                return
            with lock:  # the frame's bytes are its own (a fresh read each frame): kept, not copied, here
                if canvas_id in got:
                    return
                got[canvas_id] = frame
                if len(got) == len(canvas_ids):
                    done.set()

        taps[canvas_id] = tap
        engine.add_frame_tap(_tap_key(canvas_id), tap)
    try:
        done.wait(timeout)
    finally:
        for canvas_id, tap in taps.items():
            engine.remove_frame_tap(_tap_key(canvas_id), tap)
    with lock:
        frames = dict(got)
    return {**read, **{canvas_id: QImage(f.data, f.width, f.height, f.width * 4,
                                         QImage.Format.Format_RGBA8888_Premultiplied).copy()
                       for canvas_id, f in frames.items()}}


def _tap_key(canvas_id: str) -> str:
    """What to tap for a picture: a canvas as on air without Snap's polaroid ("shot:<id>", see Engine), or a view
    ("view:<name>") as it is. A view tapped as "shot:view:<name>" was never drawn: every bloom pictured off air
    waited out the timeout and answered "Nothing is drawing right now" (PC 2026-11-20, build 44785036541)."""
    return canvas_id if canvas_id.startswith("view:") else f"shot:{canvas_id}"


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


SCENE_WAKE_S = 4.0  # a bloom pictured off air: how long its sleeping sources get to wake first, at most
SCENE_SETTLE_S = 0.3  # ...and how long after the last one woke (its first picture lands)


def _still_asleep(engine, scene) -> bool:
    """Is anything showing in this bloom (however deep in sprigs) still asleep?"""
    collection = getattr(engine, "_collection", None)
    sources = getattr(engine, "_sources", None)
    if collection is None or sources is None:
        return False
    scene = next((s for s in collection.scenes if s.id == scene.id), scene)  # the engine's own copy of it
    stack, seen, ids = [scene], {scene.id}, set()
    while stack:
        shown = stack.pop()
        ids.update(item.source for item in shown.items if item.visible)
        for inner in collection.inner_shapes(shown):
            if inner.id not in seen:
                seen.add(inner.id)
                stack.append(inner)
    return any(getattr(sources.get(sid), "asleep", False) for sid in ids)


def grab_scene(engine, scene, view: str = "apishot") -> QImage | None:
    """A picture of a bloom that isn't on air, on its own canvas, or None if it can't be made. It's drawn through
    an engine view for that moment only (as the bloom tile's Save screenshot is): nothing on air changes, its
    sources wake while the view is there and rest again once it's gone. Waits for them to wake first."""
    return grab_scenes(engine, [scene], view)[0]


def grab_scenes(engine, scenes, view: str = "apishot") -> list[QImage | None]:
    """grab_scene for several blooms at once (a bloom's horizontal and vertical shapes): their views are drawn
    side by side, so they wake together and one wait covers them all, not one wait each."""
    names = [view if i == 0 else f"{view}-{i + 1}" for i in range(len(scenes))]
    asleep = any(scene is not None and _still_asleep(engine, scene) for scene in scenes)
    for name, scene in zip(names, scenes):
        if scene is not None:
            engine.set_view(name, scene.canvas, "bloom", scene.id)
    try:
        if asleep:  # sources to wake first; nothing asleep (all on air anyway): straight to the picture
            until = time.monotonic() + SCENE_WAKE_S
            time.sleep(0.05)  # a tick or two, so the views are seen and their sources are told to wake
            while any(scene is not None and _still_asleep(engine, scene) for scene in scenes) \
                    and time.monotonic() < until:
                time.sleep(0.05)
            time.sleep(SCENE_SETTLE_S)
        ids = [f"view:{name}" for name, scene in zip(names, scenes) if scene is not None]
        got = grab(engine, ids) if ids else {}
        return [got.get(f"view:{name}") if scene is not None else None for name, scene in zip(names, scenes)]
    finally:
        for name, scene in zip(names, scenes):
            if scene is not None:
                engine.set_view(name, None)


def _scene_shot(image: QImage | None, name: str, folder: Path | None, width: int | None, as_data: bool) -> dict | None:
    if image is None:
        return None
    image = shrink(image, width)
    shot: dict = {"width": image.width(), "height": image.height()}
    if folder is not None:
        safe = "".join(c if c.isalnum() or c in " -_()" else "_" for c in name).strip() or "bloom"
        shot["path"] = str(save(image, folder, safe))
    if as_data:
        shot["png"] = png_base64(image)
    return shot


def take_scene(engine, scene, name: str, folder: Path | None, width: int | None = None,
               as_data: bool = False) -> dict | None:
    """Like take_one, for a bloom off air: saved as "screenshot <name> <time>.png" and/or returned as base64."""
    return _scene_shot(grab_scene(engine, scene), name, folder, width, as_data)


def take_scenes(engine, scenes, name: str, folder: Path | None, width: int | None = None,
                as_data: bool = False) -> list[dict | None]:
    """take_scene for several at once (see grab_scenes): one shot (or None) for each, in order."""
    return [_scene_shot(image, name, folder, width, as_data) for image in grab_scenes(engine, scenes)]


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
