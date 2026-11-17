"""Screen, window and game capture ask Windows for no more pictures than the canvas uses (OBS game capture
only copies what it draws too): every picture Windows hands over is copied off the graphics card first."""

import threading
import time

from sprigcast.sources import capture


class FakeControl:
    def __init__(self):
        self.done = threading.Event()

    def is_finished(self):
        return self.done.is_set()

    def stop(self):
        self.done.set()


def _fake_backend(made, refuse_pacing):
    class FakeCapture:
        def __init__(self, **kwargs):
            if refuse_pacing and "minimum_update_interval" in kwargs:
                raise OSError("MinUpdateInterval not supported")  # Windows before 11 24H2
            made.append(kwargs)

        def event(self, fn):
            return fn

        def start_free_threaded(self):
            return FakeControl()

    return FakeCapture


def _run_once(monkeypatch, source, refuse_pacing):
    made = []
    monkeypatch.setattr(capture, "_load_backend", lambda: _fake_backend(made, refuse_pacing))
    monkeypatch.setattr(capture, "_PACING_OK", None)
    source.start()
    deadline = time.monotonic() + 2
    while not made and time.monotonic() < deadline:
        time.sleep(0.01)
    source.stop()
    return made


def test_capture_asks_for_no_more_pictures_than_the_canvas_uses(monkeypatch):
    src = capture.DisplayCaptureSource("d", {"monitor": 1})
    src.canvas_fps = 60
    made = _run_once(monkeypatch, src, refuse_pacing=False)
    assert made[0]["minimum_update_interval"] == 15 and capture._PACING_OK is True  # a little under 1/60 s
    src = capture.DisplayCaptureSource("d", {"monitor": 1, "fps_limit": 30})
    assert src._min_interval_ms() == 30  # its own lower limit wins


def test_older_windows_captures_without_the_limit(monkeypatch):
    src = capture.DisplayCaptureSource("d", {"monitor": 1})
    made = _run_once(monkeypatch, src, refuse_pacing=True)
    assert made and "minimum_update_interval" not in made[0] and capture._PACING_OK is False
