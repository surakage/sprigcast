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
