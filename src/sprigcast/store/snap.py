"""Snap: a polaroid camera pops up, flashes, and prints a photo of your camera (or the stream).

Put a Snap in a bloom (+ → Plugins → Snap), above everything else. When it's triggered, the
camera pops in, the button goes down, the flash goes off and a polaroid slides out of the slot.
The photo develops, grows to the middle, holds for a few seconds and slides away. Each one is
saved as "snap YYYY-MM-DD HH-MM-SS.png" (your recordings folder → Snaps, or a folder you pick).

A trigger can bring a line of text, written at the bottom of the polaroid, so a redeem's message
or a raider's name ends up on the photo:
    {"do": "snap.take"}                                         (every Snap: the first one)
    {"do": "snap.take", "text": "Raid from SlothFriend!", "of": "Facecam"}
    {"do": "snap.take", "snap": "Raid snap", "text": "%rawInput%"}   (Streamer.bot redeem)

Settings: Photo of (a source's name; empty is what's on air), Caption (used when a trigger
brings no text), Frame (vintage or white), Seconds (how long it stays up), Flash, Save, Folder.
Snaps triggered while one is up wait their turn (up to 10).

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import register_hotkeys, register_item_menu

log = logging.getLogger(__name__)

PLUGIN = {"id": "snap", "name": "Snap", "version": 1, "adds": "Source",
          "about": "A polaroid camera flashes and prints a photo of your camera, with a viewer's message on it. "
                   "Each snap is saved too."}

W, H = 800, 1000  # the Snap's box
CAM_W, CAM_TOP = 420, 70  # the camera, drawn this wide, its picture's top this far down
MOST_WAITING = 10
CAPTION_MAX = 120
INK = (43, 38, 32)
HANDWRITING = ["Segoe Print", "Ink Free", "Bradley Hand", "Marker Felt", "Comic Sans MS", "Comic Neue"]
VIDEO = ("camera", "display_capture", "window_capture", "game_capture", "media", "browser", "image", "scene")

# The art: vectors, drawn at whatever size they're shown. Where things are, in each SVG's own units.
ASSETS = Path(__file__).resolve().parent / "assets" / "snap"
CAM_SIZE = 256  # camera.svg and button.svg (the shutter button, drawn on its own so it can go down)
CAM_SLOT_Y, CAM_SLOT_X = 200, (52, 208)
CAM_FLASH = (213, 77)
FRAMES = {
    # file, size, photo window (x, y, w, h), caption area
    "vintage": ("frame_vintage.svg", (387, 470), (22, 30, 348, 340), (26, 392, 340, 68)),
    "white": ("frame_white.svg", (1064, 776), (67, 33, 933, 583), (80, 628, 907, 128)),
}

# The show, in seconds from the trigger.
POP = (0.0, 0.35)
PRESS = (0.45, 0.7)
FLASH = (0.55, 1.0)
PRINT = (0.8, 2.0)
CAMERA_OUT = (2.1, 2.5)
GROW = (2.1, 2.7)
DEVELOP = (1.0, 3.5)
CAPTION = (2.6, 3.0)
LEAVE = 0.5


def _assets_dir() -> Path:
    if (ASSETS / "camera.svg").exists():
        return ASSETS
    from sprigcast import plugin_store  # installed in the plugins folder: the art stays with SprigCast

    return plugin_store.STORE_DIR / "assets" / "snap"


def _phase(t: float, span: tuple[float, float]) -> float:
    a, b = span
    return 0.0 if t <= a else 1.0 if t >= b else (t - a) / (b - a)


def _smooth(x: float) -> float:
    return x * x * (3 - 2 * x)


def _back(x: float) -> float:
    """Eases out past 1 and settles back: a pop."""
    c = 1.70158
    x -= 1
    return 1 + (c + 1) * x * x * x + c * x * x


_ART = threading.local()  # SVG renderers are Qt objects: one set per thread (render, saving)


def draw_art(p, name: str, rect) -> None:
    """Draw one of the SVGs into rect (in the painter's units), sharp at any size."""
    from PySide6.QtSvg import QSvgRenderer

    cache = getattr(_ART, "renderers", None)
    if cache is None:
        cache = _ART.renderers = {}
    renderer = cache.get(name)
    if renderer is None:
        renderer = cache[name] = QSvgRenderer(str(_assets_dir() / name))
    renderer.render(p, rect)


def frame_art(frame: str):
    """(SVG file, (w, h), window QRect, caption QRect) for "vintage" or "white"."""
    from PySide6.QtCore import QRect

    name, size, window, caption = FRAMES.get(frame, FRAMES["vintage"])
    return name, size, QRect(*window), QRect(*caption)


def to_qimage(picture: np.ndarray):
    from PySide6.QtGui import QImage

    picture = np.ascontiguousarray(picture)
    h, w = picture.shape[:2]
    return QImage(picture.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


def to_array(image) -> np.ndarray:
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = image.width(), image.height()
    rows = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(h, image.bytesPerLine())[:, : w * 4]
    return rows.reshape(h, w, 4).copy()


def paint_caption(p, area, text: str) -> None:
    """Text written in the caption area, as big as fits on at most two lines."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF

    text = " ".join(text.split())[:CAPTION_MAX]
    if not text:
        return
    area = QRectF(area).adjusted(area.width() * 0.04, area.height() * 0.08, -area.width() * 0.04,
                                 -area.height() * 0.08)
    font = QFont()
    font.setFamilies(HANDWRITING)
    flags = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
    size = area.height() * 0.62
    while True:
        font.setPixelSize(max(6, int(size)))
        metrics = QFontMetricsF(font)
        bound = metrics.boundingRect(area, int(flags), text)
        if (bound.height() <= area.height() and bound.width() <= area.width()) or size <= 8:
            break
        size *= 0.92
    if bound.height() > area.height():  # still too long at the smallest size: cut it
        while len(text) > 1 and metrics.boundingRect(area, int(flags), text + "…").height() > area.height():
            text = text[:-1]
        text = text.rstrip() + "…"
    p.save()
    p.setFont(font)
    p.setPen(QColor(*INK))
    p.drawText(area, int(flags), text)
    p.restore()


def compose(picture: np.ndarray | None, frame: str, text: str = "", width: int | None = None):
    """The polaroid: the photo cover-cropped into the frame's window, the frame on top, then the caption.

    width scales the whole card (None: at least 1000 px across the photo). Returns (QImage, window
    QRectF, caption QRectF), the rects in the returned image's pixels.
    """
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    art, (art_w, art_h), window, caption = frame_art(frame)
    scale = (width / art_w) if width else max(1.0, 1000 / window.width())
    out_w, out_h = max(1, round(art_w * scale)), max(1, round(art_h * scale))
    card = QImage(out_w, out_h, QImage.Format.Format_ARGB32_Premultiplied)
    card.fill(Qt.GlobalColor.transparent)
    win = QRectF(window.x() * scale, window.y() * scale, window.width() * scale, window.height() * scale)
    cap = QRectF(caption.x() * scale, caption.y() * scale, caption.width() * scale, caption.height() * scale)
    p = QPainter(card)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.fillRect(win.adjusted(-2, -2, 2, 2), QColor(24, 22, 20))
    if picture is not None and picture.size:
        photo = to_qimage(picture)
        cover = max(win.width() / photo.width(), win.height() / photo.height())
        sw, sh = win.width() / cover, win.height() / cover
        source = QRectF((photo.width() - sw) / 2, (photo.height() - sh) / 2, sw, sh)
        p.drawImage(win, photo, source)
    draw_art(p, art, QRectF(0, 0, out_w, out_h))
    if text:
        paint_caption(p, cap, text)
    p.end()
    return card, win, cap


_SAVING = threading.Lock()
_TAKEN: set[Path] = set()  # names given out, so two snaps in the same second get different ones


def snap_name(folder: Path, when: datetime | None = None) -> Path:
    """Where a snap taken now goes: "snap 2026-11-14 20-03-05.png", with (2), (3)... in the same second."""
    when = when or datetime.now()
    with _SAVING:
        path = folder / when.strftime("snap %Y-%m-%d %H-%M-%S.png")
        n = 2
        while path.exists() or path in _TAKEN:
            path = folder / when.strftime(f"snap %Y-%m-%d %H-%M-%S ({n}).png")
            n += 1
        _TAKEN.add(path)
        return path


def save(picture: np.ndarray | None, frame: str, text: str, path: Path) -> bool:
    try:
        card, _win, _cap = compose(picture, frame, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _SAVING:
            part = path.with_name(path.name + ".part")  # written whole, then named: never a half-saved snap
            if not card.save(str(part), "PNG"):
                raise OSError(f"couldn't write {path}")
            part.replace(path)
        log.info("snap saved to %s", path)
        return True
    except Exception:
        log.exception("saving a snap failed")
        return False


class _Job:
    def __init__(self, text: str, photo_of: str | None, path: Path | None, grab):
        self.text, self.photo_of, self.path, self.grab = text, photo_of, path, grab
        self.picture: np.ndarray | None = None
        self.got = False
        self.card = None  # (QImage, window, caption) once the picture is in
        self.started = 0.0
        self.saved = False


@register("snap")
class SnapSource(Source):
    label = "Snap"
    defaults = {"photo_of": "", "caption": "", "frame": "vintage", "seconds": 8.0, "flash": True, "save": True,
                "folder": ""}
    sleeps = False  # a trigger can come while it's hidden; it costs nothing until then
    own_sprig = (W, H)  # added from + → Plugins, it gets its own sprig filling the canvas, on top

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._queue: deque[_Job] = deque()
        self._job: _Job | None = None
        self._clear = True  # send one see-through frame
        self.status = "ready"
        self.last_saved: Path | None = None

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    def wake(self) -> None:
        self._clear = True

    # Triggers (UI thread) ------------------------------------------------------------------------

    def take(self, text: str = "", photo_of: str | None = None, path: Path | None = None, grab=None) -> int:
        """Queue a snap, saved to path (None: not saved). Returns how many are waiting before it (-1: full)."""
        with self._lock:
            ahead = len(self._queue) + (self._job is not None)
            if ahead >= MOST_WAITING:
                return -1
            self._queue.append(_Job(text, photo_of, path, grab))
            return ahead

    def set_text(self, text: str) -> bool:
        """Change the words on the photo that's up now (saved again with them). False if none is up."""
        with self._lock:
            job = self._job
            if job is None:
                return False
            job.text = text
        if job.card is not None:
            self._save(job)
        return True

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._job = None
            self._clear = True

    @property
    def busy(self) -> bool:
        return self._job is not None or bool(self._queue)

    # The show (render thread) --------------------------------------------------------------------

    def _hold(self) -> float:
        return max(1.0, float(self.settings.get("seconds", 8.0) or 8.0))

    def _length(self) -> float:
        return GROW[1] + self._hold() + LEAVE

    def _start(self, job: _Job) -> None:
        job.started = time.monotonic()

        def got(picture, job=job):
            job.picture = picture
            job.got = True

        if job.grab is not None:
            job.grab(job.photo_of, got)
        else:
            job.got = True

    def _picture_in(self, job: _Job) -> None:
        frame = str(self.settings.get("frame", "vintage"))
        _art, (art_w, art_h), _w, _c = frame_art(frame)
        width = min(W * 0.86, art_w * (H * 0.80) / art_h)
        job.card = compose(job.picture, frame, "", int(width))
        self._save(job)

    def _save(self, job: _Job) -> None:
        if job.path is None:
            return
        args = (job.picture, str(self.settings.get("frame", "vintage")), job.text, job.path)

        def run():
            if save(*args):
                job.saved = True
                self.last_saved = job.path

        threading.Thread(target=run, name="snap-save", daemon=True).start()

    def video_frame(self) -> np.ndarray | None:
        with self._lock:
            if self._job is None and self._queue:
                self._job = self._queue.popleft()
                self._start(self._job)
            job = self._job
        if job is None:
            if self._clear:
                self._clear = False
                return np.zeros((H, W, 4), dtype=np.uint8)
            return None
        if job.got and job.card is None:
            self._picture_in(job)
        t = time.monotonic() - job.started
        if t >= self._length():
            with self._lock:
                if self._job is job:
                    self._job = None
            self.status = f"saved {job.path.name}" if job.saved and job.path else "ready"
            return np.zeros((H, W, 4), dtype=np.uint8)
        self.status = "snapping"
        return to_array(self.paint(job, t))

    def paint(self, job: _Job, t: float):
        """The Snap at t seconds into this snap, as a QImage."""
        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        p = QPainter(image)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        k = CAM_W / CAM_SIZE
        cam_x = (W - CAM_W) / 2
        lift = _smooth(_phase(t, CAMERA_OUT))
        cam_y = CAM_TOP - 70 * lift
        cam_alpha = min(1.0, _phase(t, (0.0, 0.12)) * 1.0) * (1 - lift)
        pop = _back(_phase(t, POP)) if t < POP[1] else 1.0
        pop = 0.6 + 0.4 * pop

        # The photo: printing out of the slot, then growing to the middle.
        card = job.card
        hold_end = GROW[1] + self._hold()
        leave = _smooth(_phase(t, (hold_end, hold_end + LEAVE)))
        slot_y = cam_y + CAM_SLOT_Y * k
        if t >= PRINT[0]:
            if card is not None:
                img, win, cap = card
            else:  # the picture is a tick late: a dark card for now
                img, win, cap = compose(None, str(self.settings.get("frame", "vintage")), "", 400)
            small_w = (CAM_SLOT_X[1] - CAM_SLOT_X[0]) * k * 0.92
            small_h = small_w * img.height() / img.width()
            out = _smooth(_phase(t, PRINT))
            small = QRectF(W / 2 - small_w / 2, CAM_TOP + CAM_SLOT_Y * k - small_h + small_h * out, small_w, small_h)
            final = QRectF(W / 2 - img.width() / 2, H / 2 - img.height() / 2 + 30, img.width(), img.height())
            g = _smooth(_phase(t, GROW))
            rect = QRectF(small.x() + (final.x() - small.x()) * g, small.y() + (final.y() - small.y()) * g,
                          small.width() + (final.width() - small.width()) * g,
                          small.height() + (final.height() - small.height()) * g)
            rect.translate(0, leave * 160)
            p.save()
            p.setOpacity(1 - leave)
            if g <= 0:
                p.setClipRect(QRectF(0, slot_y, W, H))  # only what's come out of the slot
            centre = rect.center()
            p.translate(centre)
            p.rotate(-2.5 * g)
            sx, sy = rect.width() / img.width(), rect.height() / img.height()
            p.scale(sx, sy)
            p.translate(-img.width() / 2, -img.height() / 2)
            # a soft shadow, then the card
            shadow = QColor(0, 0, 0, int(70 * g))
            if g > 0:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(shadow)
                p.drawRoundedRect(QRectF(6 / sx, 10 / sy, img.width(), img.height()), 4 / sx, 4 / sy)
            p.drawImage(QPointF(0, 0), img)
            develop = 1 - _smooth(_phase(t, DEVELOP))
            if develop > 0:
                p.fillRect(win, QColor(38, 30, 26, int(240 * develop)))
            if job.text and t >= CAPTION[0]:
                p.setOpacity((1 - leave) * _smooth(_phase(t, CAPTION)))
                paint_caption(p, cap, job.text)
            p.restore()

        # The camera, in front of the photo coming out of it.
        if cam_alpha > 0:
            p.save()
            p.setOpacity(cam_alpha)
            p.translate(QPointF(W / 2, cam_y + CAM_SIZE * k / 2))
            p.scale(pop * k, pop * k)
            p.translate(-CAM_SIZE / 2, -CAM_SIZE / 2)
            press = _phase(t, PRESS)
            dip = 5 * (1 - abs(2 * press - 1)) if 0 < press < 1 else 0
            draw_art(p, "button.svg", QRectF(0, dip, CAM_SIZE, CAM_SIZE))
            draw_art(p, "camera.svg", QRectF(0, 0, CAM_SIZE, CAM_SIZE))
            p.restore()

        # The flash.
        if self.settings.get("flash", True) and FLASH[0] < t < FLASH[1]:
            f = _phase(t, FLASH)
            fx, fy = cam_x + CAM_FLASH[0] * k, cam_y + CAM_FLASH[1] * k
            room = 2 * min(fx, W - fx, fy, H - fy)  # a glow cut off by the box's edge looks wrong
            size = 40 + (room - 40) * _smooth(min(1.0, f * 2.2))
            alpha = min(1.0, f * 10) * (1 - _smooth(f))
            p.save()
            p.setOpacity(alpha)
            draw_art(p, "flash.svg", QRectF(fx - size / 2, fy - size / 2, size, size))
            draw_art(p, "flash.svg", QRectF(fx - size / 4, fy - size / 4, size / 2, size / 2))  # a hot centre
            p.restore()
        p.end()
        return image


# Triggers ----------------------------------------------------------------------------------------


def snaps(api) -> list:
    return [s for s in api.col.sources if s.kind == "snap"]


def _folder(api, source) -> Path:
    chosen = str(source.settings.get("folder", "") or "").strip()
    if chosen:
        return Path(chosen)
    try:
        return Path(api.host.stats()["recordDirectory"]) / "Snaps"
    except Exception:
        return Path.home() / "Videos" / "Snaps"


def take(api, snap=None, text: str | None = None, of=None) -> dict:
    """Take a snap with this Snap source (default: the first one). of is a source config or None."""
    if snap is None:
        found = snaps(api)
        if not found:
            raise ApiError("not_allowed", "Add a Snap to a bloom first (+ → Plugins → Snap).")
        snap = found[0]
    instance = api.host.source_instance(snap.id)
    if instance is None or not hasattr(instance, "take"):
        raise ApiError("not_allowed", f"{snap.name} isn't running yet.")
    if text is None:
        text = str(snap.settings.get("caption", "") or "")
    if of is None:
        wanted = str(snap.settings.get("photo_of", "") or "").strip()
        if wanted:
            of = next((s for s in api.col.sources if s.id == wanted), None) or next(
                (s for s in api.col.sources if s.name == wanted), None)
            if of is None:
                raise ApiError("not_found", f"{snap.name} takes photos of {wanted!r}, which isn't there any more.")
    path = snap_name(_folder(api, snap)) if snap.settings.get("save", True) else None
    ahead = instance.take(text, of.id if of is not None else None, path, api.host.grab_picture)
    if ahead < 0:
        raise ApiError("busy", f"{MOST_WAITING} snaps are already waiting.")
    out = {"snap": snap.name, "of": of.name if of is not None else "on air", "text": text or None,
           "waiting": ahead, "file": str(path) if path else None}
    send_event(api, "snap.taken", out)
    return out


def set_text(api, snap, text: str) -> dict:
    """New words on the photo that's up now; with none up, they become the Snap's usual caption."""
    instance = api.host.source_instance(snap.id)
    if instance is not None and hasattr(instance, "set_text") and instance.set_text(text):
        return {"snap": snap.name, "text": text, "on": "photo"}
    snap.settings = {**snap.settings, "caption": text}
    api.host.commit("sources")
    return {"snap": snap.name, "text": text, "on": "caption"}


def _snap_by(api, data):
    if data.get("snap") is None:
        return None
    wanted = data["snap"]
    if not isinstance(wanted, str):
        raise ApiError("bad_request", "snap must be a name or id.")
    found = snaps(api)
    snap = next((s for s in found if s.id == wanted), None) or next((s for s in found if s.name == wanted), None)
    if snap is None:
        raise ApiError("not_found", f"No Snap called {wanted!r}.")
    return snap


def _take_action(api, data):
    text = data.get("text")
    if text is not None and not isinstance(text, str):
        raise ApiError("bad_request", "text must be a string.")
    of = api._source(data, "of") if data.get("of") not in (None, "") else None
    return take(api, _snap_by(api, data), text, of)


def _text_action(api, data):
    text = data.get("text")
    if not isinstance(text, str):
        raise ApiError("bad_request", "text must be a string.")
    snap = _snap_by(api, data)
    if snap is None:
        found = snaps(api)
        if not found:
            raise ApiError("not_allowed", "Add a Snap to a bloom first (+ → Plugins → Snap).")
        snap = found[0]
    return set_text(api, snap, text)


def _clear_action(api, data):
    targets = [_snap_by(api, data)] if data.get("snap") is not None else snaps(api)
    for snap in targets:
        instance = api.host.source_instance(snap.id)
        if instance is not None and hasattr(instance, "clear"):
            instance.clear()
    return {"cleared": [s.name for s in targets]}


register_action("snap.take", "Snap a polaroid: the camera flashes and prints a photo, with text on it if given.",
                {"snap": "?which Snap (name or id; default the first)", "text": "?written on the photo",
                 "of": "?source to photograph (default: the Snap's setting, or what's on air)"}, _take_action)
register_action("snap.text", "Change the words on the photo that's up now (or, with none up, the usual caption).",
                {"snap": "?which Snap (default the first)", "text": "the words"}, _text_action)
register_action("snap.clear", "Put the Snap away now and drop any waiting snaps.",
                {"snap": "?which Snap (default all)"}, _clear_action)


def _hotkeys(collection):
    return [(f"snap:{s.id}", f"Take a snap: {s.name}") for s in collection.sources if s.kind == "snap"]


def _run_hotkey(api, hid):
    snap_id = hid.split(":", 1)[1]
    snap = next((s for s in snaps(api) if s.id == snap_id), None)
    if snap is not None:
        take(api, snap)


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None:
        return
    if source.kind == "snap":
        add("Take a snap", lambda: take(api, source))
    elif source.kind in VIDEO and source.kind != "scene" and snaps(api):
        add("Take a snap of this", lambda: take(api, None, None, source))


register_item_menu(_menu)
