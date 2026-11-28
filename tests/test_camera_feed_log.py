"""The engine's camera reader says in the log when a camera goes away and when it comes back (PC test 2026-11-19:
an unplugged Facecam came back with nothing in the log)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from sprigcast import sprigcore
from sprigcast.sources import camera


def test_a_camera_going_and_coming_back_is_logged(monkeypatch, caplog):
    feed = camera._Feed("cam", {"name": "Test cam", "width": 64, "height": 48, "fps": 30})
    said = iter(["starting", "ok", "camera disconnected, retrying: I/O error", "camera not available, retrying",
                 "ok", "ok"])

    class Player:
        def __init__(self, playing=True):
            pass

        def set_picture(self, *a):
            pass

        def set_device(self, *a):
            pass

        def set_shrink(self, *a):
            pass

        def info(self):
            text = next(said, None)
            if text is None:
                feed._stop.set()
                text = "ok"
            return SimpleNamespace(faulted=False, status_text=text, width=64, height=48)

        def close(self):
            pass

    monkeypatch.setattr(sprigcore, "MediaPlayer", Player)
    monkeypatch.setattr(camera, "_engine_choices", lambda f, d: ("lavfi", [("x", "")]))
    monkeypatch.setattr(feed._restart, "wait", lambda t: None)
    caplog.set_level(logging.INFO, logger=camera.log.name)
    feed._run_engine(camera.CameraDevice(0, "Test cam", ""))
    lines = [r.getMessage() for r in caplog.records if "Test cam" in r.getMessage()]
    assert lines[1:] == ["cam: 'Test cam' went away: camera disconnected, retrying: I/O error",
                         "cam: 'Test cam' is back (64x48)"]
    assert feed.status == "capturing 64x48"


def test_reports_name_each_capture_device_and_how_it_opened(monkeypatch):
    """User 2026-11-27: track which capture devices testers use. The tester report and the stream report carry a
    Capture devices section: the device, the mode asked for, what it gave and what it offers."""
    from sprigcast.app import tester_report

    monkeypatch.setattr(camera, "_captures_seen", {})
    monkeypatch.setattr(sprigcore, "camera_said", [])
    assert camera.capture_lines() == ["Capture devices:", "  none opened since SprigCast started"]
    modes = [("pixel_format", "yuyv422", 1280, 720, 5.0, 60.0), ("vcodec", "mjpeg", 640, 480, 5.0, 60.0)]
    feed = SimpleNamespace(opened_with=None, given=(1280, 720), backend="Engine (FFmpeg)", modes_seen=modes,
                           _mode=lambda: (1280, 720, 30))
    camera._note_capture(feed, "fifine Video Capture")
    sprigcore.camera_said.append("media: the camera gives 1280x720 yuyv422 at 30 fps")
    lines = tester_report.capture_lines()
    assert lines[0] == "Capture devices:"
    assert lines[1] == ("  fifine Video Capture: asked 1280x720 at 30 fps, gives 1280x720, read by Engine (FFmpeg); "
                        "offers 640x480 mjpeg up to 60 fps; 1280x720 yuyv422 up to 60 fps")
    assert lines[2] == "  the engine said: the camera gives 1280x720 yuyv422 at 30 fps"
    # Python reading it: the mode that opened says the format and rate
    feed.opened_with, feed.given, feed.backend = {"video_size": "640x480", "framerate": "30", "vcodec": "mjpeg"}, None, "FFmpeg"
    camera._note_capture(feed, "fifine Video Capture")
    assert "gives 640x480 mjpeg at 30 fps, read by FFmpeg" in camera.capture_lines()[1]
