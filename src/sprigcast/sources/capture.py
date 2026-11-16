"""Display and window capture through Windows Graphics Capture (the `windows-capture` package).

Each source runs a small supervisor thread. It starts a capture session, and if the session ends
(the window closed, the game restarted, the display changed) it tries again every two seconds,
so a source recovers on its own without the user touching anything.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from sprigcast.core.mailbox import FrameRing
from sprigcast.core.source import Source, register

log = logging.getLogger(__name__)

RETRY_SECONDS = 2.0


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
        return self._frames.take()

    # Capture thread -------------------------------------------------------------

    def _on_frame(self, frame) -> None:
        if not self._due():
            return  # over the frame-rate limit: skip the copy altogether
        # frame.frame_buffer is a view into memory the capture library reuses, and its rows may be
        # padded, so copy it (into one of three reused buffers) before handing it to the render thread.
        self._size = (int(frame.width), int(frame.height))
        self._frames.put(frame.frame_buffer)

    def _supervise(self, stop: threading.Event | None = None) -> None:
        stop = stop or self._stop
        try:
            windows_capture_cls = _load_backend()
        except RuntimeError as exc:
            self.status = str(exc)
            log.warning("%s: %s", self.id, exc)
            return

        while not stop.is_set():
            self._restart.clear()
            target = self._target()
            if target is None:
                self.status = "waiting for target"
                self._restart.wait(RETRY_SECONDS)
                continue
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
            attempts = [border, None] if border is False else [border]
            control, error = None, None
            for draw_border in attempts:
                try:
                    capture = windows_capture_cls(
                        cursor_capture=bool(self.settings.get("cursor", True)), draw_border=draw_border, **target
                    )
                    capture.event(on_frame_arrived)
                    capture.event(on_closed)
                    control = capture.start_free_threaded()
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
                self.status = "target closed, retrying"
                time.sleep(RETRY_SECONDS)


@register("display_capture")
class DisplayCaptureSource(_CaptureSource):
    """Settings: monitor (1-based index), cursor (bool)."""

    def _target(self) -> dict[str, Any] | None:
        return {"monitor_index": int(self.settings.get("monitor", 1))}


@register("window_capture")
class WindowCaptureSource(_CaptureSource):
    """Settings: title, process (exe name), cursor (bool).

    The window is looked up by its process and title every time capture (re)starts, so a game that is
    restarted, or a browser whose title changes, is found again.
    """

    def _target(self) -> dict[str, Any] | None:
        from sprigcast.sources.win32_enum import find_window

        window = find_window(str(self.settings.get("title", "")), str(self.settings.get("process", "")))
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


def _window_alive(hwnd: int) -> bool:
    import ctypes

    return bool(ctypes.windll.user32.IsWindow(hwnd))
