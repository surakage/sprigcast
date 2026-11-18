"""Now playing: the song you're listening to, on stream, with its cover, how far in it is, and bars that move
with the music.

Add Now playing (+ → Plugins → Now playing): it gets a sprig of its own with a card in the bottom left
corner of your bloom, in every shape. Move or resize it like anything else.

It reads what Windows already knows is playing (the media keys and the volume pop-up use the same thing),
so it works with Spotify, YouTube Music, Apple Music, Tidal, browsers and most players, with no keys, no
sign-in and nothing to set up. When the music pauses or stops, the card fades away; it comes back with the
next song.

Settings: Style (Canopy card, Cover glow, Slim band, Sprig ring), Show art (the cover), Show album, Show
time (a time bar with how far in the song is and how long it is, when the app tells Windows), Sound bars
(and the Sound they move with), Slide speed (a title too long for the card slides by, 1 slow to 5 fast),
Only from (app names, e.g. "Spotify, Chrome": leave it empty for any app).

Style tab (core/style.py, the same on every styled plugin): Accent, Text and Card colour (a colour ending in
00 is see-through: no card), Font, Corners (Round, Leaf, Square), Show dot, and Custom look.

It's light: it listens for Windows saying the song changed instead of asking over and over, and only while
the card is on screen. The card is drawn once per song; each frame only adds what moves, on a worker,
never on the render thread. The time moves once a second; a sliding title and the bars move up to 30
times a second, and only while they move (in silence the bars settle and nothing is drawn).

Apps can ask with {"do": "nowplaying.get"} and hear "nowplaying.changed" (when the card is on screen).

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from sprigcast.audio import listen
from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core import drawn, painted, style, workers
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

PLUGIN = {"id": "now_playing", "name": "Now playing", "version": 6, "adds": "Source",
          "about": "The song you're playing in Spotify, YouTube Music or a browser, with its cover, a time bar and "
                   "sound bars. No keys needed.",
          "new": "Lighter: its moving parts are drawn by SprigCast's engine."}

KIND = "now_playing"
W, H = 1100, 200  # the card's box (the card hugs the words, from the left); scale it on the canvas like anything else
ACCENT = "#4fd88f"
LOOK_BASE = {"text_color": "#f2f7f3", "card_color": "#151c19e0", "accent_color": ACCENT}
HIDE_AFTER = 2.0  # seconds paused before the card goes (skipping songs doesn't flicker it)
FADE = 0.3  # seconds to fade in or out
SETTLE = 0.15  # Windows sends a few changes at once when a song starts: read once they've landed
MOST_ART = 8 * 1024 * 1024
PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
NEEDS = "Now playing needs Windows 10 (1809 or newer)."
STYLES = (("canopy", "Canopy card"), ("glow", "Cover glow"), ("band", "Slim band"), ("ring", "Sprig ring"))  # A-Z
SPEEDS = tuple((n, str(n)) for n in range(1, 6))
SLIDE_PX = 40  # box units a second for each step of Slide speed (2: 80, about two letters a second)
SLIDE_GAP = 140  # box units between the end of a sliding title and its start coming round again
SLIDE_REST = 1.5  # seconds a sliding title rests at its start each time round
LINGER_S = 120.0  # asleep this long, it stops listening to Windows (see NowPlayingSource.sleep)
BAR_FPS = 30  # at most this many frames a second while something moves
RATE = 48_000
N = 2048  # the bars' FFT size
STILL = 0.004  # every bar below this: silence


@dataclass(frozen=True)
class Track:
    title: str
    artist: str = ""
    album: str = ""
    app: str = ""  # the app playing it, as Windows names it ("Spotify.exe", "Chrome")
    playing: bool = True
    art: bytes | None = None  # the cover, as the app gave it (PNG or JPEG)
    length: float = 0.0  # seconds; 0 when the app doesn't say
    position: float = 0.0  # seconds in, when Windows last heard (at)
    at: float = 0.0  # time.time() then

    def now(self, wall: float) -> float:
        """How far in it is at this wall-clock time: it moves on while playing."""
        moved = max(0.0, wall - self.at) if self.playing and self.at else 0.0
        return min(self.length, self.position + moved) if self.length else self.position + moved


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


def _seconds(value) -> float:
    """A WinRT TimeSpan (a timedelta, or 100 ns ticks) in seconds."""
    if value is None:
        return 0.0
    if hasattr(value, "total_seconds"):
        return float(value.total_seconds())
    duration = getattr(value, "duration", None)
    return float(duration if duration is not None else value) / 10_000_000


def timeline(session) -> tuple[float, float, float]:
    """(length, position, when) from Windows' timeline for this app's song: seconds, seconds, time.time() it
    was true. (0, 0, now) when the app doesn't say (some browsers and players)."""
    wall = time.time()
    try:
        props = session.get_timeline_properties()
        start = _seconds(props.start_time)
        length = _seconds(props.end_time) - start
        position = _seconds(props.position) - start
        updated = getattr(props, "last_updated_time", None)
        when = updated.timestamp() if hasattr(updated, "timestamp") else 0.0
    except Exception:
        return 0.0, 0.0, wall
    if not 1.0 < length < 24 * 3600:
        return 0.0, 0.0, wall
    if not wall - 24 * 3600 < when <= wall + 5:  # a time from 1601, or none: as of now
        when = wall
    return length, min(max(0.0, position), length), when


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
            import asyncio  # here, on its own thread: loading it froze the window 0.66 s when added (PC test)

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

        import asyncio

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
                        hook(session, "timeline_properties_changed")  # a jump to another part of the song
                    sessions_key = key
                current = manager.get_current_session()
                current_app = str(current.source_app_user_model_id or "") if current is not None else ""
                tracks = [t for t in [await self._read(s) for s in sessions] if t is not None]
                track = pick(tracks, current_app, self.only_from)
                what = None if track is None else (track.title, track.artist, track.album, track.app, track.playing,
                                                   len(track.art or b""), round(track.length),
                                                   round(track.at - track.position))  # moves only on a jump
                if what != shown and not self._quit.is_set():
                    if track is None:
                        log.info("now playing: nothing playing")
                    elif shown is None or not shown or shown[:4] != what[:4]:  # a new song (not a pause or a jump)
                        log.info("now playing: %s - %s (%s, %s)", track.title, track.artist or "?", track.app or "?",
                                 clock(track.length) if track.length else "no length")
                    shown = what
                    self.on_track(track)
                await self._wait()
        finally:
            unhook(keep_manager=False)
            self._loop = None

    async def _wait(self) -> None:
        """Until Windows says something changed (or stop), then a moment more for the rest of it to land."""
        import asyncio

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
        length, position, at = timeline(session)
        return Track(title, artist, album, app, playing, await self._art(props, (app, title, artist, album)),
                     length, position, at)

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
#
# A card is drawn in two parts. The still part (the card, the cover, the label, the time bar's track) is
# drawn once per song, look and size. Each frame then adds only what moves on top of it: the words (from
# pictures made once, so a sliding title is one picture drawn at an offset), the sound bars, the time bar
# and the times. Frames are made on a shared worker, never on the render thread (see the source).


def lines(track: Track, settings: dict[str, Any]) -> tuple[str, str]:
    """(the title, the line under it: artist, and the album with Show album)."""
    under = [track.artist] if track.artist else []
    if settings.get("show_album", False) and track.album and track.album != track.title:
        under.append(track.album)
    return track.title, "  ·  ".join(under)


def clock(seconds: float) -> str:
    """1:05, or 1:02:05 past an hour."""
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def style_of(settings: dict[str, Any]) -> str:
    style = str(settings.get("style") or "canopy")
    return style if style in dict(STYLES) else "canopy"


def slide_speed(settings: dict[str, Any]) -> float:
    """Box units a second a long title slides (Slide speed 1-5)."""
    try:
        step = int(settings.get("slide_speed", 2))
    except (TypeError, ValueError):
        step = 2
    return SLIDE_PX * min(5, max(1, step))


def slid(t: float, width: float, room: float, speed: float) -> float:
    """How far a line `width` wide slides left in `room` after t seconds: not at all if it fits; else it
    rests at the start for SLIDE_REST, then slides by at `speed`, round and round (a copy follows SLIDE_GAP
    behind, so the start comes back in from the right)."""
    if width <= room or speed <= 0:
        return 0.0
    loop = width + SLIDE_GAP
    turn = SLIDE_REST + loop / speed
    phase = t % turn
    return 0.0 if phase < SLIDE_REST else (phase - SLIDE_REST) * speed


@dataclass
class Look:
    """Where everything goes for one song in one style, in box units (W x H)."""

    style: str
    card: tuple[float, float, float, float]  # x, y, w, h
    radius: float
    cover: tuple[float, float, float, float] | None  # a rounded square, or the ring style's circle
    title: tuple[float, float, float, float]  # where the title shows (it slides inside this when longer)
    title_px: int
    under: tuple[float, float, float, float] | None
    under_px: int
    bars: tuple[float, float, float, float] | None
    bar_count: int
    time_bar: tuple[float, float, float, float] | None  # the time bar's track
    times: tuple[float, float, float, float] | None  # where the times go
    times_px: int
    title_w: float = 0.0  # the words' own widths
    under_w: float = 0.0


def _text_w(text: str, px: int, bold: bool = True) -> float:
    from PySide6.QtGui import QFontMetricsF

    return QFontMetricsF(_font(px, bold)).horizontalAdvance(text) if text else 0.0


_FAMILIES = threading.local()  # the font the card being drawn uses (its look's), set by look() and still()


def _look_of(settings: dict[str, Any]) -> style.Style:
    """The card's look (colours, font, corners, custom look), and its font for the drawing on this thread."""
    st = style.resolve(settings, LOOK_BASE)
    _FAMILIES.families = st.families()
    return st


def _font(px: int, bold: bool = True):
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(getattr(_FAMILIES, "families", None) or style.OWN_FONT)
    font.setPixelSize(max(1, int(px)))
    font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    return font


def look(track: Track, settings: dict[str, Any], has_cover: bool, timed: bool) -> Look:
    """Lay the card out for this song. The card hugs the words up to the box's width; past that the words
    slide. timed: the app said how long the song is (and Show time is on), so the time bar shows."""
    _look_of(settings)
    style = style_of(settings)
    title, under = lines(track, settings)
    bars_on = bool(settings.get("sound_bars", True))
    edge = 3.0  # keeps the card's outline inside the box
    if style == "band":  # one slim line: cover, bars, the words, the time left; the time bar along the bottom
        h = 120.0
        y0 = H - edge - h
        x = 16.0 + (88 + 18 if has_cover else 14)
        bars = (x, y0 + 32, 56.0, 50.0) if bars_on else None
        x += 76 if bars_on else 0
        words = title + (f"  ·  {under}" if under else "")
        words_w = _text_w(title, 46) + (_text_w(f"  ·  {under}", 34, False) if under else 0.0)
        right = 30 + (_text_w("-0:00:00", 26, False) + 18 if timed else 0)
        room = W - edge - x - right
        card_w = min(W - 2 * edge, x - edge + min(words_w, room) + right)
        times = (edge + card_w - right + 12, y0, right - 30, h - 8) if timed else None
        return Look(style, (edge, y0, card_w, h), 30, (16, y0 + 16, 88, 88) if has_cover else None,
                    (x, y0, min(words_w, room), h - 8), 46, None, 34, bars, 5,
                    (edge, y0 + h - 8, card_w, 8) if timed else None, times, 26, words_w, 0.0)
    h = H - 2 * edge
    if style == "ring":
        x = 214.0
        right = 72.0  # the pill's round end
        room = W - edge - x - right
        title_w, under_w = _text_w(title, 46), _text_w(under, 30, False)
        least = 360.0 if (timed or bars_on) else 0.0
        content = min(room, max(title_w, under_w, least))
        card_w = x - edge + content + right
        time_w = _text_w("0:00:00 / 0:00:00", 24, False) if timed else 0.0
        times = (x, 128, time_w, 40) if timed else None
        bx = x + (time_w + 24 if timed else 0)
        bars = (bx, 130, x + content - bx, 36) if bars_on and x + content - bx > 60 else None
        middle = 0 if (timed or bars) else 26
        return Look(style, (edge, edge, card_w, h), h / 2, (100 - 70, 100 - 70, 140, 140) if has_cover else None,
                    (x, 16 + middle, min(title_w, content), 60), 46,
                    (x, 74 + middle, min(under_w, content), 42) if under else None, 30,
                    bars, max(6, int((bars[2] if bars else 0) // 16)), (100 - 86, 100 - 86, 172, 172) if timed else None,
                    times, 24, title_w, under_w)
    x = 200.0 if has_cover else 60.0
    right = 30.0
    room = W - edge - x - right
    if style == "glow":  # a label, the words, a row of bars, the time bar with the times at its end
        title_w, under_w = _text_w(title, 40), _text_w(under, 26, False)
        least = 440.0 if (timed or bars_on) else _text_w("Now playing", 18) + 40
        content = min(room, max(title_w, under_w, least))
        card_w = x - edge + content + right
        times_w = _text_w("0:00:00 / 0:00:00", 18, False) if timed else 0.0
        return Look(style, (edge, edge, card_w, h), 36, (20, 20, 156, 156) if has_cover else None,
                    (x, 38, min(title_w, content), 52), 40, (x, 88, min(under_w, content), 36) if under else None, 26,
                    (x, 126, content, 32) if bars_on else None, max(8, int(content // 24)),
                    (x, 170, content - times_w - 20, 7) if timed else None,
                    (x + content - times_w, 156, times_w, 34) if timed else None, 18, title_w, under_w)
    # Canopy card: the words, small bars at the end of the artist's line, the time bar with the times under it
    title_w, under_w = _text_w(title, 46), _text_w(under, 30, False)
    bars_w = 118.0 if bars_on else 0.0
    least = 420.0 if timed else 0.0
    content = min(room, max(title_w, under_w + (bars_w + 24 if bars_on else 0), least))
    card_w = x - edge + content + right
    drop = 0 if timed else 34  # no time bar: the words sit in the middle
    under_room = content - (bars_w + 24 if bars_on else 0)
    return Look(style, (edge, edge, card_w, h), 36, (20, 20, 156, 156) if has_cover else None,
                (x, 14 + drop, min(title_w, content), 60), 46,
                (x, 70 + drop, min(under_w, under_room), 42) if under else None, 30,
                (x + content - bars_w, 76 + drop, bars_w, 30) if bars_on else None, 9,
                (x, 130, content, 8) if timed else None, (x, 144, content, 34) if timed else None, 22,
                title_w, under_w)


def _rect(r):
    from PySide6.QtCore import QRectF

    return QRectF(*r)


_COVER: list = []  # [(the cover's bytes, its picture)]: the last one, read once per song


def _cover_image(track: Track, settings: dict[str, Any]):
    from PySide6.QtGui import QImage

    if not settings.get("show_art", True) or not track.art:
        return None
    if _COVER and _COVER[0][0] is track.art:
        return _COVER[0][1]
    cover = QImage.fromData(track.art)
    cover = None if cover.isNull() else cover
    _COVER[:] = [(track.art, cover)]
    return cover


def still(track: Track, settings: dict[str, Any], detail: float, timed: bool):
    """The part that doesn't move, as (QImage W x H at `detail`, Look, the words' pictures)."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainterPath, QPen, QRadialGradient

    cover = _cover_image(track, settings)
    lay = look(track, settings, cover is not None, timed)
    st = _look_of(settings)
    image, p = painted.canvas(W, H, detail)
    card = style.qcolor(st.card)
    accent = style.qcolor(st.accent)
    text_c = style.qcolor(st.text)
    box = _rect(lay.card)
    if card.alpha() or st.border_width:
        shape = style.draw_card(p, box, lay.radius, st, card)
        if lay.style == "glow":  # the accent glowing from behind the cover
            glow = QRadialGradient(QPointF(98, 98), 340)
            tint = QColor(accent)
            tint.setAlpha(int(70 * card.alphaF()))
            glow.setColorAt(0, tint)
            tint.setAlpha(0)
            glow.setColorAt(1, tint)
            p.save()
            p.setClipPath(shape)
            p.fillRect(box, glow)
            p.restore()
        if not st.border_width:
            edge = QColor(255, 255, 255, int(22 * card.alphaF()))  # the hairline edge the app's cards have
            p.setPen(QPen(edge, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(shape)
    if lay.style == "ring" and lay.time_bar is not None:
        track_c = QColor(text_c)
        track_c.setAlpha(46)
        p.setPen(QPen(track_c, 9))
        p.drawEllipse(_rect(lay.time_bar).adjusted(4.5, 4.5, -4.5, -4.5))
    if lay.cover is not None and cover is not None:
        spot = _rect(lay.cover)
        side = min(cover.width(), cover.height())  # square it from the middle (videos give wide pictures)
        source = QRectF((cover.width() - side) / 2, (cover.height() - side) / 2, side, side)
        clip = QPainterPath()
        if lay.style == "ring":
            clip.addEllipse(spot)
        else:
            clip.addRoundedRect(spot, 22 if lay.style != "band" else 16, 22 if lay.style != "band" else 16)
        p.save()
        p.setClipPath(clip)
        p.drawImage(spot, cover, source)
        p.restore()
    elif card.alpha() and lay.style != "ring" and st.shows("dot", settings):  # no cover: the sprig dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        cy = lay.card[1] + lay.card[3] / 2
        p.drawEllipse(QRectF(lay.title[0] - 38, cy - 9, 18, 18))
    elif card.alpha() and st.shows("dot", settings):
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawEllipse(QRectF(100 - 12, 100 - 12, 24, 24))
    if lay.style == "glow" and st.shows("label", settings):
        p.setFont(_font(18))
        p.setPen(accent)
        p.drawText(QRectF(lay.title[0], 12, 300, 28), int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   "Now playing")
        _leaf(p, QPointF(lay.title[0] + _text_w("Now playing", 18) + 22, 26), 9, accent)
    if lay.time_bar is not None and lay.style != "ring":
        track_c = QColor(text_c)
        track_c.setAlpha(40)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(track_c)
        if lay.style == "band":
            pass  # drawn with the fill, inside the card's rounded bottom
        else:
            r = _rect(lay.time_bar)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
    p.end()
    words = _words(track, settings, lay, detail)
    return image, lay, words


def _leaf(p, c, s, colour) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainterPath

    path = QPainterPath()
    path.moveTo(c.x() - s, c.y() + s * 0.3)
    path.cubicTo(c.x() - s * 0.4, c.y() - s * 0.9, c.x() + s * 0.8, c.y() - s * 0.7, c.x() + s, c.y() - s * 0.6)
    path.cubicTo(c.x() + s * 0.6, c.y() + s * 0.4, c.x() - s * 0.2, c.y() + s * 0.7, c.x() - s, c.y() + s * 0.3)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(colour)
    p.drawPath(path)


def _words(track: Track, settings: dict[str, Any], lay: Look, detail: float) -> tuple:
    """The title (and the line under it) drawn once each into a picture of their own, at `detail`."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor

    text_c = style.qcolor(_look_of(settings).text)
    muted = QColor(text_c)
    muted.setAlphaF(muted.alphaF() * 0.62)
    title, under = lines(track, settings)
    flags = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def picture(parts: list[tuple[str, int, bool, Any]], width: float, height: float):
        image, p = painted.canvas(max(1, int(width + 8)), max(1, int(height)), detail)
        x = 0.0
        for text, px, bold, colour in parts:
            p.setFont(_font(px, bold))
            p.setPen(colour)
            p.drawText(QRectF(x, 0, width + 8, height), flags, text)
            x += _text_w(text, px, bold)
        p.end()
        return image

    if lay.style == "band":
        parts = [(title, lay.title_px, True, text_c)] + ([(f"  ·  {under}", lay.under_px, False, muted)] if under else [])
        return (picture(parts, lay.title_w, lay.title[3]), lay.title_w), None
    first = (picture([(title, lay.title_px, True, text_c)], lay.title_w, lay.title[3]), lay.title_w)
    second = (picture([(under, lay.under_px, False, muted)], lay.under_w, lay.under[3]), lay.under_w) \
        if lay.under is not None else None
    return first, second


def moving(lay: Look) -> bool:
    """Whether the words slide (they're longer than their room)."""
    return lay.title_w > lay.title[2] + 0.5 or (lay.under is not None and lay.under_w > lay.under[2] + 0.5)


def frame(base, lay: Look, words: tuple, settings: dict[str, Any], detail: float, t: float,
          position: float | None, length: float, bands: np.ndarray | None):
    """One frame: the still part, then the words (sliding by t seconds), the bars and the time."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen

    image, p = painted.canvas(W, H, detail)
    p.drawImage(QRectF(0, 0, W, H), base)
    speed = slide_speed(settings)
    for spot, made in ((lay.title, words[0]), (lay.under, words[1])):
        if spot is None or made is None:
            continue
        picture, width = made
        room = _rect(spot)
        off = slid(t, width, room.width(), speed)
        src_w, src_h = picture.width() / detail, picture.height() / detail
        if not off and width <= room.width() + 0.5:
            p.drawImage(QRectF(room.x(), room.y(), src_w, src_h), picture)
            continue
        # Slides inside its room, fading out at the edges: drawn on a small picture of its own first.
        k = detail
        strip, q = painted.canvas(max(1, int(room.width())), max(1, int(room.height())), k)
        for x in (-off, -off + width + SLIDE_GAP):
            q.drawImage(QRectF(x, 0, src_w, src_h), picture)
        q.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        fade = QLinearGradient(0, 0, room.width(), 0)
        edge = min(0.08, 28 / max(1.0, room.width()))
        fade.setColorAt(0, QColor(0, 0, 0, 0 if off else 255))
        fade.setColorAt(edge, QColor(0, 0, 0, 255))
        fade.setColorAt(1 - edge, QColor(0, 0, 0, 255))
        fade.setColorAt(1, QColor(0, 0, 0, 0))
        q.fillRect(QRectF(0, 0, room.width(), room.height()), fade)
        q.end()
        p.drawImage(QRectF(room.x(), room.y(), room.width(), room.height()), strip)
    if lay.bars is not None:
        _bars(p, _rect(lay.bars), lay.bar_count, bands, style.qcolor(_look_of(settings).accent))
    _time(p, lay, settings, position, length)
    p.end()
    return image


def _time(p, lay: Look, settings: dict[str, Any], position: float | None, length: float) -> None:
    """The time bar (or ring) and the times."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPen

    st = _look_of(settings)
    accent = style.qcolor(st.accent)
    text_c = style.qcolor(st.text)
    if lay.time_bar is not None and position is not None and length > 0:
        share = min(1.0, max(0.0, position / length))
        r = _rect(lay.time_bar)
        if lay.style == "ring":
            pen = QPen(accent, 9)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            if share > 0.002:
                p.drawArc(r.adjusted(4.5, 4.5, -4.5, -4.5), 90 * 16, int(-360 * 16 * share))
        elif lay.style == "band":
            clip = style.card_path(_rect(lay.card), lay.radius, st)
            p.save()
            p.setClipPath(clip)
            under_c = QColor(text_c)
            under_c.setAlpha(40)
            p.fillRect(r, under_c)
            p.fillRect(QRectF(r.x(), r.y(), r.width() * share, r.height()), _sweep(r, accent))
            p.restore()
        else:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_sweep(r, accent))
            if share > 0.002:
                p.drawRoundedRect(QRectF(r.x(), r.y(), max(r.height(), r.width() * share), r.height()),
                                  r.height() / 2, r.height() / 2)
            if lay.style == "glow":
                p.setBrush(text_c)
                p.drawEllipse(QPointF(r.x() + r.width() * share, r.center().y()), r.height() * 1.3, r.height() * 1.3)
        _times(p, lay, position, length, text_c, accent)


def frame_listed(base, lay: Look, words: tuple, settings: dict[str, Any], detail: float, t: float,
                 position: float | None, length: float, bands: np.ndarray | None, memo: dict | None = None):
    """frame() as a list of pictures for the engine to draw (core/drawn.py): the still part and the words are
    pictures made once per song, the sliding words are cut into thin upright slices whose opacity follows the
    fade at the room's edges, the bars are discs and boxes, and only the time part is painted, in a picture of
    just its own size, when it changes (a second's clock, half a pixel of the bar)."""
    from PySide6.QtCore import QRectF

    memo = {} if memo is None else memo
    out_w, out_h = max(1, round(W * detail)), max(1, round(H * detail))
    d = drawn.Drawn(out_w, out_h)
    d.scale(out_w / W, out_h / H)
    d.drawImage(QRectF(0, 0, W, H), base)
    speed = slide_speed(settings)
    for spot, made in ((lay.title, words[0]), (lay.under, words[1])):
        if spot is None or made is None:
            continue
        picture, width = made
        room = _rect(spot)
        off = slid(t, width, room.width(), speed)
        src_w, src_h = picture.width() / detail, picture.height() / detail
        if not off and width <= room.width() + 0.5:
            d.drawImage(QRectF(room.x(), room.y(), src_w, src_h), picture)
            continue
        # frame() slides them in a strip of whole units, stretched onto the room
        rw, rh = float(max(1, int(room.width()))), float(max(1, int(room.height())))
        fx, fy = room.width() / rw, room.height() / rh
        for x0, x1, alpha in _fade_slices(room.x() * out_w / W, room.width() * out_w / W, room.width(), off != 0,
                                          max(1, round(out_w / W))):
            d.save()
            left, right = x0 * W / out_w, x1 * W / out_w
            d.clip(left, room.y(), right - left, room.height())
            d.opacity(alpha)
            for x in (-off, -off + width + SLIDE_GAP):
                at = room.x() + x * fx
                if at < right and at + src_w * fx > left:  # a copy that shows in this slice
                    d.drawImage(QRectF(at, room.y(), src_w * fx, src_h * fy), picture)
            d.restore()
    if lay.bars is not None:
        _bars_listed(d, _rect(lay.bars), lay.bar_count, bands, style.qcolor(_look_of(settings).accent).getRgb())
    part = _time_part(lay, settings, detail, position, length, memo)
    if part is not None:
        picture, box = part
        d.image("time", picture, *box)
    return d


def _fade_slices(left_px: float, width_px: float, room: float, left: bool,
                 step: int = 1) -> list[tuple[float, float, float]]:
    """frame()'s fade across a sliding line's room (from left_px, width_px wide, in the picture's pixels; `room`
    box units wide) as (x0, x1, opacity) slices on whole pixels: `step` pixels wide where it fades, one slice where
    it doesn't (slices off the pixel grid leave seams where a clip rounds)."""
    edge = min(0.08, 28 / max(1.0, room))
    a, z = round(left_px), round(left_px + width_px)
    b, c = round(left_px + edge * width_px), round(left_px + (1 - edge) * width_px)

    def at(x0: int, x1: int) -> float:  # the fade's opacity in a slice's middle
        f = ((x0 + x1) / 2 - left_px) / max(1e-6, width_px)
        return 1.0 if edge <= 0 else max(0.0, min(1.0, f / edge, (1 - f) / edge))

    def run(start: int, end: int) -> list[tuple[float, float, float]]:
        cuts = list(range(start, end, step)) + [end]
        return [(float(x0), float(x1), at(x0, x1)) for x0, x1 in zip(cuts, cuts[1:]) if x1 > x0]

    out = run(a, b) if left else []
    out.append((float(b if left else a), float(c), 1.0))
    return out + run(c, z)


_PILLS: dict = {}


def _pill(rgba: tuple[int, int, int, int], w: int, h: int):
    """A rounded bar of one colour, w x h pixels, ends round (made once per colour and size)."""
    key = (rgba, w, h)
    found = _PILLS.get(key)
    if found is None:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        found = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        found.fill(Qt.GlobalColor.transparent)
        p = QPainter(found)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(*rgba))
        p.drawRoundedRect(QRectF(0, 0, w, h), w / 2, w / 2)
        p.end()
        if len(_PILLS) > 16:
            _PILLS.clear()
        _PILLS[key] = found
    return found


def _bars_listed(d, r, count: int, bands: np.ndarray | None, rgba) -> None:
    """_bars() as two pieces of one tall bar picture each: its top (round end and as much of the bar as shows)
    and its round bottom end."""
    count = max(1, count)
    w = r.width() / count
    bar = w * 0.62
    k = d.width / W
    rgba = tuple(int(v) for v in rgba)
    pw, ph = max(2, round(bar * k)), max(4, round(max(bar, r.height()) * k))  # made at the size it's shown
    pill, key = _pill(rgba, pw, ph), f"pill:{rgba}:{pw}:{ph}"
    py = ph / max(bar, r.height())  # picture pixels per box unit, up and down
    over = 0.5 if rgba[3] == 255 else 0.0
    for i in range(count):
        level = float(bands[i]) if bands is not None and i < len(bands) else 0.0
        h = max(bar, r.height() * min(1.0, level))
        x, bottom = r.x() + i * w + (w - bar) / 2, r.bottom()
        top = min(h - bar / 2 + over, ph / py)  # down under the bottom end's edge: no seam
        d.image(key, pill, x, bottom - h, bar, top, (0, 0, pw, top * py))
        d.image(key, pill, x, bottom - bar / 2, bar, bar / 2, (0, ph - bar / 2 * py, pw, bar / 2 * py))


def _time_part(lay: Look, settings: dict[str, Any], detail: float, position: float | None, length: float,
               memo: dict):
    """The time bar and times painted into a picture of just their box, (picture, (x, y, w, h)), made again only
    when what it shows changes."""
    if lay.time_bar is None or position is None or length <= 0:
        return None
    import math

    from PySide6.QtCore import QRectF

    r = _rect(lay.time_bar)
    m = r.height() * 1.3 + 2 if lay.style == "glow" else 2.0
    box = r.adjusted(-m, -m, m, m)
    if lay.times is not None:
        box = box.united(_rect(lay.times).adjusted(-2, -2, 2, 2))
    if lay.style == "band":
        box = box.intersected(_rect(lay.card).adjusted(-1, -1, 1, 1))
    x0, y0 = math.floor(box.x()), math.floor(box.y())
    bw, bh = math.ceil(box.right()) - x0, math.ceil(box.bottom()) - y0
    share = min(1.0, max(0.0, position / length))
    key = (id(lay), detail, int(position), int(length), round(share * r.width() * detail * 2), bw, bh)
    found = memo.get("time")
    if found is not None and found[0] == key:
        return found[1], (x0, y0, bw, bh)
    image, p = painted.canvas(bw, bh, detail)
    p.translate(-x0, -y0)
    _time(p, lay, settings, position, length)
    p.end()
    memo["time"] = (key, image)
    return image, (x0, y0, bw, bh)


def _sweep(r, accent):
    from PySide6.QtGui import QColor, QLinearGradient

    sweep = QLinearGradient(r.left(), 0, r.right(), 0)
    dark = QColor(accent).darker(150)
    sweep.setColorAt(0, dark)
    sweep.setColorAt(1, accent)
    return sweep


def _times(p, lay: Look, position: float, length: float, text_c, accent) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor

    if lay.times is None:
        return
    muted = QColor(text_c)
    muted.setAlphaF(muted.alphaF() * 0.62)
    r = _rect(lay.times)
    left = int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    right = int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    p.setFont(_font(lay.times_px, False))
    if lay.style == "canopy":
        p.setPen(muted)
        p.drawText(r, left, clock(position))
        p.drawText(r, right, clock(length))
    elif lay.style == "ring":
        p.setPen(accent)
        p.drawText(r, left, f"{clock(position)} / {clock(length)}")
    elif lay.style == "glow":
        p.setPen(muted)
        p.drawText(r, right, f"{clock(position)} / {clock(length)}")
    else:
        p.setPen(muted)
        p.drawText(r, right, f"-{clock(length - position)}")


def _bars(p, r, count: int, bands: np.ndarray | None, accent) -> None:
    """Rounded bars standing on the bottom of r, the lowest a dot."""
    from PySide6.QtCore import QRectF, Qt

    count = max(1, count)
    w = r.width() / count
    bar = w * 0.62
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(accent)
    for i in range(count):
        level = float(bands[i]) if bands is not None and i < len(bands) else 0.0
        h = max(bar, r.height() * min(1.0, level))
        p.drawRoundedRect(QRectF(r.x() + i * w + (w - bar) / 2, r.bottom() - h, bar, h), bar / 2, bar / 2)


def paint(track: Track, settings: dict[str, Any], detail: float = 1.0, t: float = 0.0,
          position: float | None = None, bands: np.ndarray | None = None):
    """The whole card for one moment, as a QImage (premultiplied), W x H at `detail`."""
    timed = bool(settings.get("show_time", True)) and track.length > 0
    base, lay, words = still(track, settings, detail, timed)
    return frame(base, lay, words, settings, detail, t, track.position if position is None else position,
                 track.length, bands)


class Bands:
    """The sound bars' heights (0..1) from the sound the mixer hands over: a small FFT, eased."""

    def __init__(self):
        self._chunks = (np.zeros((N // 2, 2), np.float32), np.zeros((N // 2, 2), np.float32))
        self._fed = 0
        self._used = 0
        self._unheard = 0.0
        self._window = np.hanning(N).astype(np.float32)
        self.levels = np.zeros(0, np.float32)

    def feed(self, block: np.ndarray) -> None:
        """On the mixer thread: keep the newest block, nothing more."""
        self._chunks = (self._chunks[1], block.copy())
        self._fed += 1

    def step(self, count: int, dt: float) -> np.ndarray:
        a, b = self._chunks
        samples = np.concatenate([c.mean(axis=1) if c.ndim == 2 else c for c in (a, b)])[-N:].astype(np.float32)
        if len(samples) < N:
            samples = np.pad(samples, (N - len(samples), 0))
        if self._fed != self._used:
            self._used, self._unheard = self._fed, 0.0
        else:
            self._unheard += dt
            if self._unheard > 0.25:  # nothing handed over lately: fall away
                samples = np.zeros(N, np.float32)
        mags = np.abs(np.fft.rfft(samples * self._window)) * (4.0 / N)
        freqs = np.geomspace(60.0, 12_000.0, count + 1)
        edges = np.round(freqs / (RATE / N)).astype(int)
        for i in range(1, len(edges)):  # the low bars are narrower than a bin: give each its own
            edges[i] = max(edges[i], edges[i - 1] + 1)
        edges = np.minimum(edges, len(mags) - 1)
        peaks = np.maximum.reduceat(mags, edges[:-1])[:count]
        centres = np.sqrt(freqs[:-1] * freqs[1:])
        db = 20.0 * np.log10(peaks + 1e-9) + np.clip(3.0 * np.log2(centres / 500.0), 0.0, 18.0)
        target = np.clip((db + 60.0) / 60.0, 0.0, 1.0).astype(np.float32)
        if len(self.levels) != count:
            self.levels = np.zeros(count, np.float32)
        hold = 0.55 ** max(dt * BAR_FPS, 0.0)
        rising = target > self.levels
        self.levels = np.where(rising, self.levels + (target - self.levels) * 0.7,
                               target + (self.levels - target) * hold).astype(np.float32)
        return self.levels


# The source ------------------------------------------------------------------------------------------

make_watcher = MediaWatcher  # tests swap in a stand-in
run_job = workers.picture  # the pictures lane (core/workers.py); tests swap in one that runs straight away
_LATEST: dict[str, Track | None] = {}  # source id -> what it shows, for nowplaying.get


def _sounds() -> list[tuple[str, str]]:
    return [(listen.MIX, "Everything (stream)"), *listen.sounds()]


@register(KIND)
class NowPlayingSource(Source):
    """Frames are made on a shared worker, never on the render thread: video_frame hands over the last finished
    one and, when something moves, asks for the next (at most BAR_FPS a second while the words slide or the
    bars move, once a second for the time alone). Standing still, nothing is drawn or uploaded."""

    label = "Now playing"
    defaults = {"style": "canopy", "show_art": True, "show_album": False, "show_time": True, "sound_bars": True,
                "sound": listen.MIX, "slide_speed": 2, "only_from": "", "accent_color": ACCENT,
                "text_color": "#f2f7f3", "card_color": "#151c19e0", "font": "", "corners": "round",
                "show_dot": True, "custom_look": ""}
    choices = {"style": STYLES, "sound": _sounds, "slide_speed": SPEEDS}
    sleeps = True  # off screen it stops listening to Windows altogether
    sharp = True  # drawn at the size it's shown
    own_sprig = (W, H)
    own_sprig_corner = "bottom_left"
    own_sprig_height = 0.14  # starts readable: the card about a seventh of the canvas tall
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._lock = threading.Lock()
        self._track: Track | None = None  # the newest from Windows
        self._since = 0.0  # when this song started showing (the words slide from then)
        self._shown: tuple | None = None  # what the last frame asked for showed
        self._fade = (0.0, 0.0, 0.0)  # (from alpha, to alpha, start time)
        self._paused_at = 0.0
        self._watcher: MediaWatcher | None = None
        self._hidden = False
        self._bands = Bands()
        self._heard: str | None = None  # the sound the bars listen to now
        self._still: tuple | None = None  # (key, picture, Look, words): the part that doesn't move
        self._turn = 0  # goes up when it sleeps: frames asked for before then are dropped
        self._busy = False
        self._linger: threading.Timer | None = None  # stops the watcher a while after it went to sleep
        self._ready = None  # a finished frame (QImage, or a drawn.Drawn for the engine) for the next video_frame
        self._memo: dict = {}  # frame_listed's time part, made again only when it changes
        self._last = 0.0  # when the last frame was asked for
        self._flat = False  # the bars were still (silence) in the last frame
        self._slides = False  # the words are longer than their room
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    # Listening only while on screen -------------------------------------------------------------

    def start(self) -> None:
        self._listen()

    def wake(self) -> None:
        self._shown = None
        self._stay()
        with self._lock:
            track = self._track  # heard while it lingered: apps can ask about it again
        if track is not None:
            _LATEST[self.id] = track
        if not self._hidden:
            self._listen()

    def sleep(self) -> None:
        """Off screen: no bars, no pictures. It keeps listening to Windows for LINGER_S, so a bloom switched back
        to soon doesn't start the watcher again: starting it on every wake cost the PC's switch frame 70 ms
        (2026-11-18 heavy run, "now_playing-1 70.2"). Then it stops listening too."""
        self._deaf()
        _LATEST.pop(self.id, None)  # off screen: apps hear nothing's playing on it, as before
        self._turn += 1  # frames asked for before now are dropped
        self._busy = False
        self._frame.release()
        self._still = None
        self._ready = None
        self._shown = None
        if self._watcher is not None:
            timer = threading.Timer(LINGER_S, self._quiet_if_asleep)
            timer.args = (timer,)
            timer.daemon = True
            timer.name = "now-playing-linger"
            with self._lock:
                old, self._linger = self._linger, timer
            if old is not None:
                old.cancel()
            timer.start()

    def stop(self) -> None:
        self._stay()
        self._quiet()

    def _stay(self) -> None:
        """Called back (or closed): the stop waiting to happen after a sleep doesn't."""
        with self._lock:
            timer, self._linger = self._linger, None
        if timer is not None:
            timer.cancel()

    def _quiet_if_asleep(self, timer) -> None:
        """LINGER_S after it went to sleep (its own thread): asleep all that time, it stops listening."""
        with self._lock:
            if self._linger is not timer:
                return  # woke meanwhile
            self._linger = None
            watcher, self._watcher = self._watcher, None
            self._track = None  # back on screen it shows the song as it is then
        _LATEST.pop(self.id, None)
        if watcher is not None:
            watcher.stop()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._quiet()
        else:
            self._listen()

    def update(self, settings: dict[str, Any]) -> None:
        only_before, sound_before = self.settings.get("only_from", ""), self._sound()
        super().update(settings)
        self._still = None  # a new look: draw it again
        self._shown = None
        if self.settings.get("only_from", "") != only_before and self._watcher is not None:
            self._quiet()
            self._listen()
        elif self._sound() != sound_before:
            self._deaf()

    def _sound(self) -> str:
        return str(self.settings.get("sound") or listen.MIX)

    def _listen(self) -> None:
        if self._watcher is not None:
            return
        self._watcher = make_watcher(self._heard_song, str(self.settings.get("only_from", "") or ""), self._problem)
        self._watcher.start()

    def _quiet(self) -> None:
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.stop()
        self._deaf()
        with self._lock:
            self._track = None  # back on screen it shows the song as it is then
        _LATEST.pop(self.id, None)
        self._fade = (0.0, 0.0, 0.0)
        self._paused_at = 0.0
        self._turn += 1
        self._busy = False

    def _hear(self) -> None:
        """The bars listen to their sound (only while they show and the music plays)."""
        sound = self._sound()
        if self._heard == sound:
            return
        self._deaf()
        listen.add(sound, self._bands.feed)
        self._heard = sound

    def _deaf(self) -> None:
        if self._heard is not None:
            listen.remove(self._heard, self._bands.feed)
            self._heard = None
        self._bands = Bands()
        self._flat = False

    def _heard_song(self, track: Track | None) -> None:
        """From the watcher's thread: the song (or play / pause, or where it's up to) changed."""
        with self._lock:
            old, self._track = self._track, track
            if track is not None and (old is None or (old.title, old.artist) != (track.title, track.artist)):
                self._since = time.monotonic()
        self.status = "ok"
        if self.asleep:
            return  # lingering off screen: kept for when it's back, not told to apps
        _LATEST[self.id] = track
        _tell(self.id, track)

    def _problem(self, text: str) -> None:
        self.status = text

    # Frames ---------------------------------------------------------------------------------------

    def _alpha(self, now: float) -> float:
        a, b, t0 = self._fade
        if b == a:
            return b
        f = min(1.0, (now - t0) / FADE)
        return a + (b - a) * f

    def video_frame(self) -> np.ndarray | None:
        now = time.monotonic()
        with self._lock:
            track, since = self._track, self._since
        showing = track is not None and track.playing
        if track is not None and not track.playing:
            if not self._paused_at:
                self._paused_at = now
            showing = now - self._paused_at < HIDE_AFTER and self._still is not None  # stays up a moment
        else:
            self._paused_at = 0.0
        target = 1.0 if showing else 0.0
        if self._fade[1] != target:
            self._fade = (self._alpha(now), target, now)
        alpha = self._alpha(now)
        bars = bool(self.settings.get("sound_bars", True)) and track is not None and track.playing and alpha > 0
        if bars and not self._hidden:
            self._hear()
        elif self._heard is not None:
            self._deaf()
        if alpha <= 0.001 or track is None:
            self._ready = None
            key = ("gone",)
            if key == self._shown:
                return None
            self._shown = key
            if track is None:
                self._still = None
            self._frame.release()
            return painted.clear_frame(W, H)
        timed = bool(self.settings.get("show_time", True)) and track.length > 0
        position = track.now(time.time()) if timed else None
        moves = 0.0 < alpha < 1.0 or self._slides or (bars and not self._flat)
        what = (track.title, track.artist, track.album, track.art, track.length > 0, self.detail,
                tuple(sorted(self.settings.items(), key=lambda kv: kv[0])))
        key = (what, int(position) if position is not None else None, round(alpha, 3))
        due = now - self._last >= 1.0 / BAR_FPS - 0.002
        if (moves or key != self._shown) and due and not self._busy:
            self._shown = key
            self._last = now
            self._busy = True
            try:
                run_job(self._draw, self._turn, track, dict(self.settings), self.detail, now - since, position,
                        alpha, bars, what, name="now playing")
            except RuntimeError:  # the pool is shutting down (SprigCast closing)
                self._busy = False
        ready, self._ready = self._ready, None
        if isinstance(ready, drawn.Drawn):
            return ready
        return self._frame(ready) if ready is not None else None

    def _draw(self, turn: int, track: Track, settings: dict[str, Any], detail: float, t: float,
              position: float | None, alpha: float, bars: bool, what: tuple) -> None:
        """On a worker: the next frame, left for video_frame."""
        try:
            timed = position is not None
            found = self._still
            if found is None or found[0] != what:
                base, lay, words = still(track, settings, detail, timed)
                found = self._still = (what, base, lay, words)
            _, base, lay, words = found
            levels = None
            if bars and lay.bars is not None:
                levels = self._bands.step(lay.bar_count, 1.0 / BAR_FPS)
                flat = float(levels.max(initial=0.0)) < STILL
            else:
                flat = True
            if alpha >= 0.999 and drawn.wanted():  # the engine draws it from a list (fading: painted)
                image = frame_listed(base, lay, words, settings, detail, t, position, track.length, levels,
                                     self._memo)
            else:
                image = frame(base, lay, words, settings, detail, t, position, track.length, levels)
                if alpha < 0.999:
                    image = _faded(image, alpha)
            if turn == self._turn:
                self._slides = moving(lay)
                self._flat = flat
                self._ready = image
        finally:
            if turn == self._turn:
                self._busy = False


def _faded(card, alpha: float):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    image = QImage(card.width(), card.height(), QImage.Format.Format_ARGB32_Premultiplied)  # the card's detail
    image.fill(Qt.GlobalColor.transparent)
    p = QPainter(image)
    p.setOpacity(alpha)
    p.drawImage(0, 0, card)
    p.end()
    return image


# Telling apps (the watcher's thread hands it to the UI thread) ----------------------------------------


def _answer(track: Track | None) -> dict[str, Any]:
    if track is None:
        return {"playing": False, "title": "", "artist": "", "album": "", "app": "", "length": 0, "position": 0}
    return {"playing": track.playing, "title": track.title, "artist": track.artist, "album": track.album,
            "app": track.app, "length": round(track.length), "position": round(track.now(time.time()))}


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
