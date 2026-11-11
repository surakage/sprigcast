"""Capture sources against a fake `windows_capture` module, so the supervisor logic runs anywhere."""

import sys
import threading
import time
import types

import numpy as np
import pytest

from sprigcast.core.source import create_source


class FakeFrame:
    def __init__(self, width, height):
        padded = np.zeros((height, width * 4 + 16), dtype=np.uint8)  # rows padded like real GPU buffers
        self.frame_buffer = padded[:, : width * 4].reshape(height, width, 4)
        self.frame_buffer[..., 2] = 255  # red in BGRA
        self.width, self.height = width, height


class FakeControl:
    def __init__(self):
        self.stopped = threading.Event()

    def stop(self):
        self.stopped.set()

    def is_finished(self):
        return self.stopped.is_set()


class FakeWindowsCapture:
    instances: list = []

    def __init__(self, cursor_capture=True, draw_border=None, monitor_index=None, window_hwnd=None, **_):
        self.kwargs = dict(cursor_capture=cursor_capture, monitor_index=monitor_index, window_hwnd=window_hwnd)
        self.handlers = {}
        FakeWindowsCapture.instances.append(self)

    def event(self, handler):
        self.handlers[handler.__name__] = handler
        return handler

    def start_free_threaded(self):
        control = FakeControl()

        def run():
            while not control.stopped.is_set():
                self.handlers["on_frame_arrived"](FakeFrame(32, 18), control)
                time.sleep(0.01)
            self.handlers["on_closed"]()

        threading.Thread(target=run, daemon=True).start()
        return control


@pytest.fixture
def fake_backend(monkeypatch):
    module = types.ModuleType("windows_capture")
    module.WindowsCapture = FakeWindowsCapture
    monkeypatch.setitem(sys.modules, "windows_capture", module)
    FakeWindowsCapture.instances = []
    return FakeWindowsCapture


def wait_for(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_display_capture_delivers_contiguous_bgra_frames(fake_backend):
    src = create_source("display_capture", "d", {"monitor": 2})
    src.start()
    try:
        assert wait_for(lambda: src.size == (32, 18))
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        assert tuple(frame[0, 0]) == (0, 0, 255, 0)
        assert frame.flags["C_CONTIGUOUS"] and frame.shape == (18, 32, 4)
        assert src.channel_order == "BGRA"
        assert fake_backend.instances[0].kwargs["monitor_index"] == 2
        assert src.status == "capturing"
    finally:
        src.stop()


def test_settings_change_restarts_capture(fake_backend):
    src = create_source("display_capture", "d", {"monitor": 1})
    src.start()
    try:
        assert wait_for(lambda: len(fake_backend.instances) == 1)
        src.update({"monitor": 2})
        assert wait_for(lambda: len(fake_backend.instances) == 2)
        assert fake_backend.instances[1].kwargs["monitor_index"] == 2
    finally:
        src.stop()


def test_window_capture_waits_until_window_exists(fake_backend, monkeypatch):
    from sprigcast.sources import capture, win32_enum

    found = {"window": None}
    monkeypatch.setattr(win32_enum, "find_window", lambda title, process="": found["window"])
    monkeypatch.setattr(capture, "RETRY_SECONDS", 0.05)
    src = create_source("window_capture", "w", {"title": "Game", "process": "game.exe"})
    src.start()
    try:
        assert wait_for(lambda: src.status == "waiting for target")
        assert fake_backend.instances == []
        found["window"] = win32_enum.WindowInfo(hwnd=1234, title="Game", process="game.exe")
        assert wait_for(lambda: src.status == "capturing")
        assert fake_backend.instances[0].kwargs["window_hwnd"] == 1234
    finally:
        src.stop()


def test_missing_backend_reports_status(monkeypatch):
    monkeypatch.setitem(sys.modules, "windows_capture", None)  # import fails
    src = create_source("display_capture", "d", {})
    src.start()
    src.stop()
    assert "windows-capture" in src.status


def test_find_window_prefers_process_and_title(monkeypatch):
    from sprigcast.sources import win32_enum
    from sprigcast.sources.win32_enum import WindowInfo

    windows = [WindowInfo(1, "Game", "other.exe"), WindowInfo(2, "Lobby", "game.exe"), WindowInfo(3, "Game", "game.exe")]
    monkeypatch.setattr(win32_enum, "list_windows", lambda: windows)
    assert win32_enum.find_window("Game", "game.exe").hwnd == 3
    assert win32_enum.find_window("Renamed", "GAME.EXE").hwnd == 2  # title changed, same app
    assert win32_enum.find_window("Game", "").hwnd == 1
    assert win32_enum.find_window("Nope", "nope.exe") is None


def test_border_falls_back_to_default_when_hiding_is_unsupported(fake_backend, monkeypatch):
    class Win10Capture(FakeWindowsCapture):
        def __init__(self, draw_border=None, **kwargs):
            if draw_border is False:
                raise RuntimeError("border toggle not supported")
            super().__init__(draw_border=draw_border, **kwargs)
            self.kwargs["draw_border"] = draw_border

    sys.modules["windows_capture"].WindowsCapture = Win10Capture
    src = create_source("display_capture", "d", {"monitor": 1})
    src.start()
    try:
        assert wait_for(lambda: src.status == "capturing")
        assert Win10Capture.instances[0].kwargs["draw_border"] is None
    finally:
        src.stop()
