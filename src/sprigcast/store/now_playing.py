"""Now playing: the song you're listening to, on stream, with its cover.

Add Now playing (+ → Plugins → Now playing): it gets a sprig of its own with a small card in the bottom left
corner of your bloom, in every shape. Move or resize it like anything else.

It reads what Windows already knows is playing (the media keys and the volume pop-up use the same thing),
so it works with Spotify, YouTube Music, Apple Music, Tidal, browsers and most players, with no keys, no
sign-in and nothing to set up. When the music pauses or stops, the card fades away; it comes back with the
next song.

Settings: Show art (the cover), Show album, Only from (app names, e.g. "Spotify, Chrome": leave it empty
for any app), Text colour and Card colour (a colour ending in 00 is see-through: no card).

It's light: it listens for Windows saying the song changed instead of asking over and over, and it only
listens while the card is on screen. The card is drawn again only when the song changes.

Apps can ask with {"do": "nowplaying.get"} and hear "nowplaying.changed" (when the card is on screen).

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core import painted
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "now_playing", "name": "Now playing", "version": 1, "adds": "Source",
          "about": "The song you're playing in Spotify, YouTube Music or a browser, with its cover. No keys needed."}

KIND = "now_playing"
W, H = 760, 200  # the card's box; scale it on the canvas like anything else
ACCENT = "#4fd88f"
HIDE_AFTER = 2.0  # seconds paused before the card goes (skipping songs doesn't flicker it)
FADE = 0.3  # seconds to fade in or out
SETTLE = 0.15  # Windows sends a few changes at once when a song starts: read once they've landed
MOST_ART = 8 * 1024 * 1024
PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
NEEDS = "Now playing needs Windows 10 (1809 or newer)."


@dataclass(frozen=True)
class Track:
    title: str
    artist: str = ""
    album: str = ""
    app: str = ""  # the app playing it, as Windows names it ("Spotify.exe", "Chrome")
    playing: bool = True
    art: bytes | None = None  # the cover, as the app gave it (PNG or JPEG)


def app_matches(app: str, only_from: str) -> bool:
    """Is this app one of Only from ("Spotify, Chrome")? Empty means any app."""
    wanted = [w.strip().lower() for w in str(only_from or "").replace(";", ",").split(",") if w.strip()]
    if not wanted:
        return True
    app = (app or "").lower()
    return any(w in app or w.replace(" ", "") in app for w in wanted)


def pick(tracks: list[Track], current_app: str, only_from: str) -> Track | None:
    """The one to show: Windows' current one if it's playing (and allowed), else any that's playing, else the
    current one paused (so the card can fade out on it)."""
    allowed = [t for t in tracks if t.title and app_matches(t.app, only_from)]
    current = next((t for t in allowed if t.app == current_app), None)
    if current is not None and current.playing:
        return current
    return next((t for t in allowed if t.playing), None) or current


# Listening to Windows --------------------------------------------------------------------------------


class MediaWatcher:
    """Listens to Windows' media sessions on a thread of its own and calls on_track(Track or None) when the
    song (or play / pause) changes. Waits on Windows' events: no asking over and over."""

    def __init__(self, on_track: Callable[[Track | None], None], only_from: str = "",
                 on_problem: Callable[[str], None] | None = None):
        self.on_track = on_track
        self.on_problem = on_problem or (lambda text: None)
        self.only_from = only_from
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._poke: asyncio.Event | None = None
        self._art_cache: tuple[tuple, bytes | None] | None = None
        self._quit = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="now-playing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # already finished
        self._thread = None  # it unhooks from Windows and ends on its own: the render thread never waits on it

    def nudge(self, *_args) -> None:
        """A Windows event (on one of its threads): look again."""
        loop, poke = self._loop, self._poke
        if loop is not None and poke is not None:
            try:
                loop.call_soon_threadsafe(poke.set)
            except RuntimeError:
                pass

    def _run(self) -> None:
        joined = False
        try:
            from winrt.runtime import ApartmentType, init_apartment, uninit_apartment

            try:
                init_apartment(ApartmentType.MULTI_THREADED)  # this thread talks to Windows' media service
                joined = True
            except Exception:
                pass  # already set up on this thread
            asyncio.run(self._main())
        except ImportError:
            log.info("now playing: the Windows media session isn't available here")
            self.on_problem(NEEDS)
        except Exception as exc:
            log.warning("now playing stopped: %s", exc, exc_info=True)
            self.on_problem("Now playing couldn't read what's playing (it's in the log).")
        finally:
            if joined:
                try:
                    uninit_apartment()
                except Exception:
                    pass

    async def _main(self) -> None:
        from winrt.windows.media.control import (  # noqa: PLC0415 - Windows only, loaded when on screen
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )

        self._loop = asyncio.get_running_loop()
        self._stop, self._poke = asyncio.Event(), asyncio.Event()
        if self._quit.is_set():  # stopped while it was starting
            return
        manager = await Manager.request_async()
        hooked: list[tuple[Any, str, Any]] = []  # (thing, event name, token) to unhook at the end

        def hook(thing, name: str) -> None:
            try:
                hooked.append((thing, name, getattr(thing, f"add_{name}")(self.nudge)))
            except Exception:
                log.debug("now playing: couldn't listen to %s", name, exc_info=True)

        def unhook(keep_manager: bool) -> None:
            for thing, name, token in list(hooked):
                if keep_manager and thing is manager:
                    continue
                try:
                    getattr(thing, f"remove_{name}")(token)
                except Exception:
                    pass
                hooked.remove((thing, name, token))

        hook(manager, "current_session_changed")
        hook(manager, "sessions_changed")
        sessions_key: tuple = ()
        shown: tuple | None = ()
        try:
            while not self._stop.is_set():
                self._poke.clear()
                sessions = list(manager.get_sessions() or [])
                key = tuple(str(s.source_app_user_model_id or "") for s in sessions)
                if key != sessions_key:  # apps came or went: listen to each one's songs and play / pause
                    unhook(keep_manager=True)
                    for session in sessions:
                        hook(session, "media_properties_changed")
                        hook(session, "playback_info_changed")
                    sessions_key = key
                current = manager.get_current_session()
                current_app = str(current.source_app_user_model_id or "") if current is not None else ""
                tracks = [t for t in [await self._read(s) for s in sessions] if t is not None]
                track = pick(tracks, current_app, self.only_from)
                what = None if track is None else (track.title, track.artist, track.album, track.app, track.playing,
                                                   len(track.art or b""))
                if what != shown and not self._quit.is_set():
                    shown = what
                    self.on_track(track)
                await self._wait()
        finally:
            unhook(keep_manager=False)
            self._loop = None

    async def _wait(self) -> None:
        """Until Windows says something changed (or stop), then a moment more for the rest of it to land."""
        stop = asyncio.ensure_future(self._stop.wait())
        poke = asyncio.ensure_future(self._poke.wait())
        try:
            await asyncio.wait({stop, poke}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (stop, poke):
                task.cancel()
        if not self._stop.is_set():
            await asyncio.sleep(SETTLE)

    async def _read(self, session) -> Track | None:
        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            log.debug("now playing: couldn't read a song", exc_info=True)
            return None
        if props is None or not (props.title or "").strip():
            return None
        try:
            playing = int(session.get_playback_info().playback_status) == PLAYING
        except Exception:
            playing = True
        title, artist, album = (props.title or "").strip(), (props.artist or "").strip(), (props.album_title or "").strip()
        app = str(session.source_app_user_model_id or "")
        return Track(title, artist, album, app, playing, await self._art(props, (app, title, artist, album)))

    async def _art(self, props, key: tuple) -> bytes | None:
        """The cover's bytes: read once per song."""
        if self._art_cache is not None and self._art_cache[0] == key:
            return self._art_cache[1]
        data = None
        ref = getattr(props, "thumbnail", None)
        if ref is not None:
            try:
                from winrt.windows.storage.streams import DataReader

                stream = await ref.open_read_async()
                try:
                    size = int(stream.size)
                    if 0 < size <= MOST_ART:
                        reader = DataReader(stream)
                        size = int(await reader.load_async(size))
                        raw = bytearray(size)
                        reader.read_bytes(raw)
                        data = bytes(raw)
                finally:
                    try:
                        stream.close()
                    except Exception:
                        pass
            except Exception:
                log.debug("now playing: no cover", exc_info=True)
        self._art_cache = (key, data)
        return data


# Drawing ---------------------------------------------------------------------------------------------


def _colour(text: str, fallback: str):
    from PySide6.QtGui import QColor

    text = str(text or "").strip()
    if text.startswith("#") and len(text) == 9:  # #rrggbbaa
        c = QColor(text[:7])
        c.setAlpha(int(text[7:], 16))
        return c if c.isValid() else QColor(fallback)
    c = QColor(text)
    return c if c.isValid() else QColor(fallback)


def lines(track: Track, settings: dict[str, Any]) -> tuple[str, str]:
    """(the title, the line under it: artist, and the album with Show album)."""
    under = [track.artist] if track.artist else []
    if settings.get("show_album", False) and track.album and track.album != track.title:
        under.append(track.album)
    return track.title, "  ·  ".join(under)


def paint(track: Track, settings: dict[str, Any]):
    """The card as a QImage (premultiplied), W x H: the cover at the left, the title, the artist under it.
    The card hugs the words and sits at the left of the box, so it grows to the right."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QPainter, QPainterPath

    image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    title, under = lines(track, settings)
    big = QFont()
    big.setFamilies(["Nunito", "Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"])
    big.setWeight(QFont.Weight.Bold)
    big.setPixelSize(54 if under else 60)
    small = QFont(big)
    small.setPixelSize(36)
    small.setWeight(QFont.Weight.DemiBold)
    pad = 14
    card_h = H - 2 * pad
    inner = 22  # the space around the cover inside the card
    cover = None
    if settings.get("show_art", True) and track.art:
        cover = QImage.fromData(track.art)
        if cover.isNull():
            cover = None
    art_side = card_h - 2 * inner
    lead = (inner + art_side + 28) if cover is not None else 70  # where the words start
    right = 40
    room = W - 2 * pad - lead - right
    big_m, small_m = QFontMetricsF(big), QFontMetricsF(small)
    title = big_m.elidedText(title, Qt.TextElideMode.ElideRight, room)
    under = small_m.elidedText(under, Qt.TextElideMode.ElideRight, room) if under else ""
    text_w = max(big_m.horizontalAdvance(title), small_m.horizontalAdvance(under) + 4 if under else 0)
    card_w = min(W - 2 * pad, lead + text_w + right)
    x0, y0 = pad, pad
    card = _colour(settings.get("card_color"), "#151c19e0")
    text_c = _colour(settings.get("text_color"), "#f2f7f3")
    if card.alpha():
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(card)
        p.drawRoundedRect(QRectF(x0, y0, card_w, card_h), 40, 40)
    if cover is not None:
        box = QRectF(x0 + inner, y0 + inner, art_side, art_side)
        side = min(cover.width(), cover.height())  # square it from the middle (videos give wide pictures)
        source = QRectF((cover.width() - side) / 2, (cover.height() - side) / 2, side, side)
        clip = QPainterPath()
        clip.addRoundedRect(box, 22, 22)
        p.save()
        p.setClipPath(clip)
        p.drawImage(box, cover, source)
        p.restore()
    elif card.alpha():
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(ACCENT))  # the little sprig dot, like the clock's
        p.drawEllipse(QRectF(x0 + 34, y0 + card_h / 2 - 9, 18, 18))
    left = x0 + lead
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    p.setFont(big)
    p.setPen(text_c)
    if under:
        p.drawText(QRectF(left, y0 + 20, W, 74), flags, title)
        p.setFont(small)
        muted = QColor(text_c)
        muted.setAlphaF(muted.alphaF() * 0.62)
        p.setPen(muted)
        p.drawText(QRectF(left + 2, y0 + 92, W, 56), flags, under)
    else:
        p.drawText(QRectF(left, y0, W, card_h), flags, title)
    p.end()
    return image


# The source ------------------------------------------------------------------------------------------

make_watcher = MediaWatcher  # tests swap in a stand-in
_LATEST: dict[str, Track | None] = {}  # source id -> what it shows, for nowplaying.get


@register(KIND)
class NowPlayingSource(Source):
    label = "Now playing"
    defaults = {"show_art": True, "show_album": False, "only_from": "",
                "text_color": "#f2f7f3", "card_color": "#151c19e0"}
    sleeps = True  # off screen it stops listening to Windows altogether
    own_sprig = (W, H)
    own_sprig_corner = "bottom_left"
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._lock = threading.Lock()
        self._track: Track | None = None  # the newest from Windows
        self._card: tuple | None = None  # (what it shows, its picture) drawn for the newest song
        self._shown: tuple | None = None  # what the last frame was
        self._fade = (0.0, 0.0, 0.0)  # (from alpha, to alpha, start time)
        self._paused_at = 0.0
        self._watcher: MediaWatcher | None = None
        self._hidden = False
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    # Listening only while on screen -------------------------------------------------------------

    def start(self) -> None:
        self._listen()

    def wake(self) -> None:
        self._shown = None
        if not self._hidden:
            self._listen()

    def sleep(self) -> None:
        self._quiet()
        self._frame.release()
        self._card = None
        self._shown = None

    def stop(self) -> None:
        self._quiet()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._quiet()
        else:
            self._listen()

    def update(self, settings: dict[str, Any]) -> None:
        only_before = self.settings.get("only_from", "")
        super().update(settings)
        self._card = None  # a new look: draw it again
        self._shown = None
        if self.settings.get("only_from", "") != only_before and self._watcher is not None:
            self._quiet()
            self._listen()

    def _listen(self) -> None:
        if self._watcher is not None:
            return
        self._watcher = make_watcher(self._heard, str(self.settings.get("only_from", "") or ""), self._problem)
        self._watcher.start()

    def _quiet(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.stop()
        with self._lock:
            self._track = None  # back on screen it shows the song as it is then
        _LATEST.pop(self.id, None)
        self._fade = (0.0, 0.0, 0.0)
        self._paused_at = 0.0

    def _heard(self, track: Track | None) -> None:
        """From the watcher's thread: the song (or play / pause) changed."""
        with self._lock:
            self._track = track
        _LATEST[self.id] = track
        self.status = "ok"
        _tell(self.id, track)

    def _problem(self, text: str) -> None:
        self.status = text

    # Frames (render thread) ------------------------------------------------------------------------

    def _alpha(self, now: float) -> float:
        a, b, t0 = self._fade
        if b == a:
            return b
        f = min(1.0, (now - t0) / FADE)
        return a + (b - a) * f

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        with self._lock:
            track = self._track
        showing = track is not None and track.playing
        if track is not None and not track.playing:
            if not self._paused_at:
                self._paused_at = now
            showing = now - self._paused_at < HIDE_AFTER and self._card is not None  # stays up a moment
        else:
            self._paused_at = 0.0
        if track is not None:
            what = (track.title, track.artist, track.album, track.art, self.settings.get("show_art"),
                    self.settings.get("show_album"), self.settings.get("text_color"), self.settings.get("card_color"))
            if self._card is None or self._card[0] != what:
                self._card = (what, paint(track, self.settings))
        alpha = self._alpha(now)
        target = 1.0 if showing else 0.0
        if self._fade[1] != target:
            self._fade = (alpha, target, now)
        alpha = self._alpha(now)
        key = (round(alpha, 3), self._card[0] if self._card is not None else None)
        if key == self._shown:
            return None  # standing still: nothing drawn, nothing uploaded
        self._shown = key
        if alpha <= 0.001 or self._card is None:
            if not showing and track is None:
                self._card = None
            self._frame.release()
            return painted.clear_frame(W, H)
        if alpha >= 0.999:
            return self._frame(self._card[1])
        return self._frame(_faded(self._card[1], alpha))


def _faded(card, alpha: float):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setOpacity(alpha)
    p.drawImage(0, 0, card)
    p.end()
    return image


# Telling apps (the watcher's thread hands it to the UI thread) ----------------------------------------


def _answer(track: Track | None) -> dict[str, Any]:
    if track is None:
        return {"playing": False, "title": "", "artist": "", "album": "", "app": ""}
    return {"playing": track.playing, "title": track.title, "artist": track.artist, "album": track.album,
            "app": track.app}


def _window_api():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    for widget in app.topLevelWidgets() if app is not None else []:
        if isinstance(getattr(type(widget), "api", None), property) and getattr(widget, "control", None) is not None:
            try:
                return widget.api
            except Exception:
                return None
    return None


def _make_ui():
    from PySide6.QtCore import QObject, Signal, Slot

    class Ui(QObject):
        changed = Signal(str, object)

        def __init__(self):
            super().__init__()
            self.changed.connect(self.on_changed)

        @Slot(str, object)
        def on_changed(self, source_id: str, track) -> None:
            api = _window_api()
            if api is None:
                return
            try:
                send_event(api, "nowplaying.changed", _answer(track))
            except Exception:
                log.debug("couldn't tell apps about the song", exc_info=True)

    return Ui()


_UI = None
try:  # made now, on the UI thread (plugins load there), so it belongs to it
    from PySide6.QtWidgets import QApplication as _App

    if _App.instance() is not None:
        _UI = _make_ui()
except Exception:  # pragma: no cover
    log.debug("now playing: no UI thread helper", exc_info=True)


def _tell(source_id: str, track: Track | None) -> None:
    if _UI is not None:
        _UI.changed.emit(source_id, track)


def _get(api, data):
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add Now playing first (+ → Plugins → Now playing).")
    track = next((_LATEST.get(s.id) for s in found if _LATEST.get(s.id) is not None), None)
    return _answer(track)


register_action("nowplaying.get", "The song Now playing shows (while its card is on screen).", {}, _get)
