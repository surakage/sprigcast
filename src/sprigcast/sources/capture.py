"""Display and window capture through Windows Graphics Capture.

Each source runs a small supervisor thread. It starts a capture session, and if the session ends
(the window closed, the game restarted, the display changed) it tries again every two seconds,
so a source recovers on its own without the user touching anything.

With the native engine (SPRIGCAST_ENGINE=native, docs/native-engine.md "Capture") the engine captures on its own
graphics device and copies each picture on the card: the supervisor only finds the target and watches how it goes,
and no picture passes through Python. When the engine can't (an older Windows, the engine stopped) it falls back to
the `windows-capture` package, as without the engine. SPRIGCAST_NO_NATIVE_CAPTURE_VIDEO=1 always uses the package.

The engine's captures run in the capture helper (app/capture_helper.py, docs/native-engine.md "Capture helper") when
it is there: Windows keeps about one picture of graphics memory per capture started until the program that started
it ends, and the helper can be ended. So a source asleep ends its capture there, and the helper ends once nothing
is captured. When the helper can't, the engine captures itself (SPRIGCAST_CAPTURE_IN_ENGINE=1 always).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from sprigcast import sprigcore
from sprigcast.app import capture_helper
from sprigcast.core.mailbox import FrameRing
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

RETRY_SECONDS = 2.0
QUICK_RETRY_SECONDS = 0.25  # just after a target closed: looked for again this often, QUICK_TRIES times
QUICK_TRIES = 8  # (a game or window that reopens came back 2.4 s later, waiting the full RETRY_SECONDS: PC 2026-11-19)
ENGINE_AGAIN_SECONDS = 60.0  # a source the engine refused tries the engine again after this (one slow start)
LINK_SECONDS = 5.0  # an engine capture nothing took up in this long: the engine isn't drawing, capture in Python
CARD_SECONDS = 3.0  # a new source waits this long for the engine to say which card it is on (the helper needs it)
_PACING_OK: bool | None = None  # does this Windows take minimum_update_interval? None until a capture tried


def _paced(ok: bool, error: Exception | None = None) -> None:
    global _PACING_OK
    if _PACING_OK is None:
        _PACING_OK = ok
        log.info("capture: Windows %s the picture rate limit%s", "takes" if ok else "doesn't take",
                 f" ({error})" if error is not None else "")


def _load_backend():
    """Import windows-capture lazily so the app still starts on machines without it."""
    from sprigcast.core import lazy_cv2

    lazy_cv2.install()  # windows-capture imports OpenCV but never needs it here: don't load 90 MB for nothing
    try:
        from windows_capture import WindowsCapture
    except ImportError as exc:
        raise RuntimeError("unavailable: needs Windows 10 1903+ (windows-capture)") from exc
    return WindowsCapture


class _CaptureSource(Source):
    channel_order = "BGRA"  # Windows Graphics Capture delivers BGRA; the compositor swizzles on the GPU
    sleeps = True  # a capture session restarts in a moment, and a hidden one costs a copy per frame

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frames = FrameRing()
        self._size = (0, 0)
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = "starting"
        self._engine_target: sprigcore.CaptureTarget | None = None  # the engine captures it (render thread links it)
        self._engine_refused = False  # the engine couldn't: the package captures it (ENGINE_AGAIN_SECONDS)
        self._engine_refused_at = 0.0
        self._feed: capture_helper.Feed | None = None  # the capture helper captures it (the engine takes its block)
        self._helper_refused = False  # the helper couldn't: the engine captures it itself (ENGINE_AGAIN_SECONDS)
        self._helper_refused_at = 0.0
        self._cleared = False  # the window closed: show nothing (once), not its last picture, until it's back
        self._serial = 0

    # Subclasses say what to capture. Return None when the target is not available right now.
    def _target(self) -> dict[str, Any] | None:
        raise NotImplementedError

    def _should_retarget(self) -> bool:
        """Checked while capturing: True starts over on a new target (a game capture's next game)."""
        return False

    def start(self) -> None:
        """A new supervisor with its own stop flag. Never waits for one still winding down after sleep(): it
        stops by its own flag (a bloom switched straight back used to wait here, on the render thread)."""
        if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
            return
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._supervise, args=(self._stop,), name=f"capture-{self.id}",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._restart.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def sleep(self) -> None:
        self._stop.set()  # the supervisor ends the session on its own; don't wait for it here
        self._restart.set()
        self._frames = FrameRing()  # let the frame buffers go
        self.status = "paused (not on screen)"

    def wake(self) -> None:
        self.status = "starting"
        self.start()

    def update(self, settings: dict[str, Any]) -> None:
        changed = settings != self.settings
        super().update(settings)
        if changed:
            self._restart.set()  # end the current session; the supervisor starts a new one

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    _next_due = 0.0

    def _due(self) -> bool:
        """With fps_limit set, keep at most that many frames a second (a 144 fps game costs a 60 fps copy)."""
        limit = float(self.settings.get("fps_limit", 0) or 0)
        if limit <= 0:
            return True
        now = time.monotonic()
        step = 1.0 / limit
        if now < self._next_due - step * 0.25:  # a little slack, so 60 on a 60 Hz game doesn't drop to 30
            return False
        # steady on the grid while frames keep up; a pause (or the first frame) starts the grid over from now
        self._next_due = self._next_due + step if self._next_due > now - step else now + step
        return True

    def video_frame(self) -> np.ndarray | None:
        target = self._engine_target or self._feed
        if target is not None and target.linked:  # captured by the engine: read back for a frame Python draws
            return target.read()
        frame = self._frames.take()
        if frame is not None or not self._cleared:
            self._cleared = self._cleared and frame is None  # back before anything was shown: nothing to clear
            return frame
        self._cleared = False
        return np.zeros((2, 2, 4), dtype=np.uint8)  # see-through: nothing, as the window is gone

    def _closed(self) -> None:
        """Its window (or display) went: the item shows nothing until it's back, not a stale last picture
        (PC 2026-11-20: a closed window's last picture stayed for good)."""
        if self.status == "target closed, retrying":
            return
        self.status = "target closed, retrying"
        self._frames = FrameRing()
        self._cleared = True
        log.info("%s: its window closed; nothing shows until it's back", self.id)

    @property
    def engine_player(self) -> sprigcore.CaptureTarget | capture_helper.Feed | None:
        """What the engine captures for this source, or the capture helper's feed it takes the pictures from
        (render/engine.py hands it over): the render thread then only calls :meth:`engine_tick`."""
        target = self._engine_target
        if target is not None:
            return target
        feed = self._feed
        return feed if feed is not None and feed.told else None

    def engine_tick(self) -> None:
        """Render thread, on a tick the engine showed the picture itself: nothing to do (the supervisor keeps the
        status and size)."""

    @property
    def engine_shows(self) -> bool:
        """The engine captures it and has shown a picture of it since it (re)started. A bloom switch waiting for
        this source goes by this, so no picture has to be read back from the card for Python's compositor just to
        end the wait (PC soak 2026-11-19: that read-back and copy held a switch frame 22 to 130 ms)."""
        target = self._engine_target or self._feed
        if target is None or not target.linked:
            return False
        state, width, _height, shown = target.state()
        return state == sprigcore.SCREEN_RUNNING and width > 0 and shown > 0

    # The renderer sets this to the fastest canvas's frame rate: more pictures than that are never shown.
    canvas_fps = 60

    def _min_interval_ms(self) -> int:
        """The least time between two pictures worth asking Windows for (a little under one frame)."""
        limit = float(self.settings.get("fps_limit", 0) or 0)
        fps = min(limit, float(self.canvas_fps)) if limit > 0 else float(self.canvas_fps)
        return max(1, int(900 / fps)) if fps > 0 else 0

    # Capture thread -------------------------------------------------------------

    def _on_frame(self, frame) -> None:
        if not self._due():
            return  # over the frame-rate limit: skip the copy altogether
        # frame.frame_buffer is a view into memory the capture library reuses, and its rows may be
        # padded, so copy it (into one of three reused buffers) before handing it to the render thread.
        self._size = (int(frame.width), int(frame.height))
        self._frames.put(frame.frame_buffer)

    def _engine_flags(self) -> int:
        flags = sprigcore.SCREEN_CURSOR if self.settings.get("cursor", True) else 0
        if self.settings.get("border", False) is not False:
            flags |= sprigcore.SCREEN_BORDER
        if self.channel_order == "BGRX":
            flags |= sprigcore.SCREEN_OPAQUE
        return flags

    def _engine_interval_ms(self) -> int:
        """At most one picture this often: the canvas's rate, or fps_limit when it is lower."""
        limit = float(self.settings.get("fps_limit", 0) or 0)
        fps = min(limit, float(self.canvas_fps)) if limit > 0 else float(self.canvas_fps)
        return max(1, int(1000 / fps)) if fps > 0 else 0

    def _in_engine(self, target: dict[str, Any], stop: threading.Event) -> str:
        """Capture ``target`` in the engine until it ends: "ended" (the window closed: find it again), "refused"
        (the engine can't: use the package) or "stopped" (stop, restart or a new target)."""
        window = int(target.get("window_hwnd") or 0)
        monitor = 0 if window else _monitor_handle(int(target.get("monitor_index", 1)))
        if not window and not monitor:
            self.status = "waiting for target"
            return "ended"
        self._serial += 1
        made = sprigcore.CaptureTarget(window=window, monitor=monitor, flags=self._engine_flags(),
                                       interval_ms=self._engine_interval_ms(), serial=self._serial)
        self._engine_target = made
        began = time.monotonic()
        try:
            while not (stop.is_set() or self._restart.is_set() or self._should_retarget()):
                state, width, height, _frames = made.state()
                if state == sprigcore.SCREEN_RUNNING:
                    if self.status != "capturing" and not self.status.startswith("capturing "):
                        self.status = "capturing"
                    if width > 0 and height > 0:
                        self._size = (width, height)
                elif state == sprigcore.SCREEN_ENDED:
                    return "ended"
                elif state == sprigcore.SCREEN_REFUSED:
                    log.info("%s: the engine can't capture here; windows-capture does", self.id)
                    return "refused"
                elif state == sprigcore.SCREEN_NONE and not made.linked and time.monotonic() - began > LINK_SECONDS:
                    log.info("%s: the engine isn't drawing; windows-capture captures it", self.id)
                    return "refused"
                self._restart.wait(0.2)
            return "stopped"
        finally:
            self._engine_target = None

    def _use_helper(self, began: float) -> bool:
        """This capture goes to the capture helper. A source just started waits a moment for the engine to say
        which card it draws on (False meanwhile is never returned: it waits here, on the supervisor's thread)."""
        helper = capture_helper.HELPER
        if self._helper_refused and time.monotonic() - self._helper_refused_at >= ENGINE_AGAIN_SECONDS:
            self._helper_refused = False
        if self._helper_refused or not helper.wanted():
            return False
        while helper.card is None and time.monotonic() - began < CARD_SECONDS and not self._restart.is_set():
            self._restart.wait(0.05)
        return helper.usable()

    def _in_helper(self, target: dict[str, Any], stop: threading.Event) -> str:
        """Capture ``target`` in the capture helper until it ends: "ended" (the window closed, or the helper went:
        find it again), "refused" (the helper can't: the engine captures), "unlinked" (the engine isn't drawing:
        the package captures) or "stopped" (stop, restart or a new target)."""
        window = int(target.get("window_hwnd") or 0)
        monitor = 0 if window else _monitor_handle(int(target.get("monitor_index", 1)))
        if not window and not monitor:
            self.status = "waiting for target"
            return "ended"
        feed = capture_helper.HELPER.start(window, monitor, self._engine_flags(), self._engine_interval_ms())
        if feed is None:
            return "refused"
        self._feed = feed
        began = time.monotonic()
        try:
            while not (stop.is_set() or self._restart.is_set() or self._should_retarget()):
                state, width, height, _frames = feed.state()
                if state == sprigcore.SCREEN_RUNNING:
                    if self.status != "capturing" and not self.status.startswith("capturing "):
                        self.status = "capturing"
                    if width > 0 and height > 0:
                        self._size = (width, height)
                elif state == sprigcore.SCREEN_ENDED:
                    return "ended"
                elif state == sprigcore.SCREEN_REFUSED:
                    log.info("%s: the capture helper can't capture here; the engine does", self.id)
                    return "refused"
                if not feed.linked and time.monotonic() - began > LINK_SECONDS:
                    if state == sprigcore.SCREEN_NONE:
                        log.info("%s: the capture helper didn't answer; the engine captures it", self.id)
                        return "refused"
                    log.info("%s: the engine isn't drawing; windows-capture captures it", self.id)
                    return "unlinked"
                self._restart.wait(0.05 if state in (sprigcore.SCREEN_NONE, sprigcore.SCREEN_STARTING) else 0.2)
            return "stopped"
        finally:
            self._feed = None
            capture_helper.HELPER.stop(feed)

    def _supervise(self, stop: threading.Event | None = None) -> None:
        stop = stop or self._stop
        began = time.monotonic()
        windows_capture_cls = None
        quick = 0  # quick looks left after a target closed
        while not stop.is_set():
            self._restart.clear()
            target = self._target()
            if target is None:
                self.status = "waiting for target"
                self._restart.wait(QUICK_RETRY_SECONDS if quick > 0 else RETRY_SECONDS)
                quick = max(0, quick - 1)
                continue
            if self._engine_refused and time.monotonic() - self._engine_refused_at >= ENGINE_AGAIN_SECONDS:
                self._engine_refused = False  # one slow start doesn't keep this source off the engine for good
            if not self._engine_refused and sprigcore.use_native_capture():
                helped = self._use_helper(began)
                outcome = self._in_helper(target, stop) if helped else self._in_engine(target, stop)
                if helped and outcome == "refused":
                    self._helper_refused, self._helper_refused_at = True, time.monotonic()
                elif outcome in ("refused", "unlinked"):
                    self._engine_refused, self._engine_refused_at = True, time.monotonic()
                elif outcome == "ended" and not (stop.is_set() or self._restart.is_set()):
                    if self.status == "capturing" or self.status.startswith("capturing "):
                        self._closed()
                        quick = QUICK_TRIES
                    self._restart.wait(QUICK_RETRY_SECONDS if quick > 0 else RETRY_SECONDS)
                    quick = max(0, quick - 1)
                else:
                    quick = 0
                continue
            if windows_capture_cls is None:
                try:
                    windows_capture_cls = _load_backend()
                except RuntimeError as exc:
                    self.status = str(exc)
                    log.warning("%s: %s", self.id, exc)
                    return
            ended = threading.Event()

            def on_frame_arrived(frame, control):
                if stop.is_set() or self._restart.is_set():
                    control.stop()
                    return
                self._on_frame(frame)

            def on_closed():
                ended.set()

            # Hide the yellow capture border by default. Windows 10 does not allow that, so if
            # starting with border=False fails, try once more with the Windows default.
            border = self.settings.get("border", False)
            borders = [border, None] if border is False else [border]
            # Ask Windows for no more pictures than are used (Windows 11 24H2+). Each picture it hands over is
            # copied from the graphics card first, used or not: a 240 fps game copied 240 a second for a 60 fps
            # canvas. Older Windows refuses the setting: then without it, and _due() skips the extras.
            interval = self._min_interval_ms()
            paces = [interval, None] if interval and _PACING_OK is not False else [None]
            attempts = [(b, p) for p in paces for b in borders]
            control, error = None, None
            for draw_border, pace in attempts:
                try:
                    capture = windows_capture_cls(
                        cursor_capture=bool(self.settings.get("cursor", True)), draw_border=draw_border,
                        **({"minimum_update_interval": pace} if pace else {}), **target
                    )
                    capture.event(on_frame_arrived)
                    capture.event(on_closed)
                    control = capture.start_free_threaded()
                    if pace:
                        _paced(True)
                    elif interval and _PACING_OK is None and error is not None:
                        _paced(False, error)  # it only started without the setting
                    break
                except Exception as exc:  # target vanished, border not supported, access denied, ...
                    error = exc
            if control is None:
                self.status = f"error: {error}"
                log.info("%s: capture failed to start: %s", self.id, error)
                self._restart.wait(RETRY_SECONDS)
                continue
            self.status = "capturing"

            while not (stop.is_set() or self._restart.is_set() or ended.is_set() or control.is_finished()
                       or self._should_retarget()):
                self._restart.wait(0.2)
            try:
                control.stop()
            except Exception:
                pass
            if ended.is_set() and not (stop.is_set() or self._restart.is_set()):
                self._closed()
                quick = QUICK_TRIES
                time.sleep(QUICK_RETRY_SECONDS)


@register("display_capture")
class DisplayCaptureSource(_CaptureSource):
    """Settings: monitor (1-based index), cursor (bool)."""

    def _target(self) -> dict[str, Any] | None:
        return {"monitor_index": int(self.settings.get("monitor", 1))}


@register("window_capture")
class WindowCaptureSource(_CaptureSource):
    """Settings: title, process (exe name), cursor (bool), match ("app", the default, or "title").

    The window is looked up by its process and title every time capture (re)starts, so a game that is
    restarted, or a browser whose title changes, is found again.
    """

    def _target(self) -> dict[str, Any] | None:
        from sprigcast.sources.win32_enum import find_window

        title, process = str(self.settings.get("title", "")), str(self.settings.get("process", ""))
        if not title and not process:  # a Window Capture just added from + Add: nothing picked yet
            return None
        if self.settings.get("match") == "title":  # Find it again by: Exact title
            window = find_window(title, process, exact=True)
        else:  # Same app (the default): a browser whose title changes is still found
            window = find_window(title, process)
        if window is None:
            return None
        return {"window_hwnd": window.hwnd}


@register("game_capture")
class GameCaptureSource(WindowCaptureSource):
    """A game. mode "any" (the default): the full-screen game in front; "window": one game's window (title and
    process, like a window capture); "hotkey": the window that was in front when you pressed its hotkey (saved
    as its title and process). The cursor is off by default, as games draw their own.

    In "any" mode it switches when another full-screen game comes to the front, and keeps the last game
    while you alt-tab to the desktop or a chat, so the stream doesn't flash your other windows.

    see_through (off by default) keeps the game's transparency; off, the picture is solid, as games mean it.
    fps_limit caps how many frames a second are copied (0 = every frame the game draws).
    """

    label = "Game capture"
    CHECK_SECONDS = 2.0

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._game: tuple[int, int, str] | None = None  # (window handle, pid, exe) of the game being shown
        self._checked = 0.0

    @property
    def channel_order(self) -> str:  # type: ignore[override]
        return "BGRA" if self.settings.get("see_through", False) else "BGRX"

    def _mode(self) -> str:
        return str(self.settings.get("mode", "any"))

    def _target(self) -> dict[str, Any] | None:
        if self._mode() in ("window", "hotkey"):
            if self._mode() == "hotkey" and not (self.settings.get("title") or self.settings.get("process")):
                return None  # the hotkey hasn't been pressed yet
            return super()._target()
        from sprigcast.sources.app_audio import fullscreen_window

        found = fullscreen_window()
        if found is not None:
            self._game = found
        if self._game is None or not _window_alive(self._game[0]):
            self._game = None
            return None
        self.status = f"capturing {self._game[2]}"
        return {"window_hwnd": self._game[0]}

    def _should_retarget(self) -> bool:
        if self._mode() != "any":
            return False
        now = time.monotonic()
        if now - self._checked < self.CHECK_SECONDS:
            return False
        self._checked = now
        from sprigcast.sources.app_audio import fullscreen_window

        found = fullscreen_window()
        return found is not None and self._game is not None and found[0] != self._game[0]


def _monitor_handle(index: int) -> int:
    """The HMONITOR of display ``index`` (1-based, in Windows' order, as windows-capture counts them); 0 if none."""
    import ctypes
    from ctypes import wintypes

    found: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       wintypes.LPARAM)

    def each(monitor, _dc, _rect, _data):
        found.append(int(monitor or 0))
        return True

    try:
        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(each), 0)
    except (AttributeError, OSError):
        return 0
    return found[index - 1] if 1 <= index <= len(found) else 0


def _window_alive(hwnd: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.IsWindow(hwnd))
