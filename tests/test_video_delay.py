"""Video delay on a camera: the picture held back to meet a voice that arrives late (the PC, 2026-11-25: the mic
through Wave Link, NVIDIA Broadcast and Voicemod reached the stream about 0.4 s after the mouth moved)."""

import numpy as np
import pytest

from sprigcast.sources import camera
from sprigcast.sources.camera import CameraDevice, DelayLine, delay_of


def test_delay_of_reads_and_bounds_the_setting():
    assert delay_of({}) == 0 and delay_of({"delay_ms": 400}) == 0.4
    assert delay_of({"delay_ms": 5000}) == 1.0 and delay_of({"delay_ms": -3}) == 0 and delay_of({"delay_ms": "x"}) == 0


def test_the_delay_line_shows_the_newest_picture_that_has_waited():
    line = DelayLine()
    for i in range(10):
        line.put(i, now=i * 0.1)
    assert line.take(0.4, now=0.35) is None  # nothing has waited 0.4 s yet
    assert line.take(0.4, now=0.65) == 2  # 0.0, 0.1, 0.2 have; the newest of them shows
    assert line.take(0.4, now=0.66) is None  # nothing new is due
    assert line.take(0.4, now=2.0) == 9


def test_the_delay_line_keeps_a_bounded_number():
    line = DelayLine()
    for i in range(DelayLine.MAX_HELD + 50):
        line.put(i, now=0.0)
    assert line.take(0, now=1.0) == DelayLine.MAX_HELD + 49 and len(line._held) == 0


def test_a_camera_read_by_python_holds_its_pictures_back(monkeypatch):
    times = [0.0, 0.1, 0.2, 0.3, 0.55]
    monkeypatch.setattr(camera.DelayLine, "_now", staticmethod(lambda: times.pop(0)))
    source = camera.CameraSource.__new__(camera.CameraSource)
    source.settings = {"delay_ms": 400}
    source._frames, source._held, source._size = camera.Mailbox(), DelayLine(), (4, 2)
    source._feed, source._attached = None, False
    frames = [np.full((2, 4, 3), i, dtype=np.uint8) for i in range(3)]
    for f in frames:
        source._receive(f)  # held at 0.0, 0.1, 0.2
    assert source.video_frame() is None  # at 0.3
    frames[0][:] = 99  # the feed reusing its picture doesn't change a held one
    got = source.video_frame()  # at 0.55: 0.0 and 0.1 are due
    assert got is not None and int(got[0, 0, 0]) == 1


def test_no_delay_shows_the_newest_at_once():
    source = camera.CameraSource.__new__(camera.CameraSource)
    source.settings = {}
    source._frames, source._held, source._size = camera.Mailbox(), DelayLine(), (4, 2)
    source._feed, source._attached = None, False
    source._receive(np.zeros((2, 4, 3), dtype=np.uint8))
    assert source.video_frame() is not None


def test_the_engines_player_is_told_the_longest_delay_its_sources_ask_for():
    class Player:
        told = []

        def set_delay(self, seconds):
            self.told.append(seconds)

    class Sub:
        def __init__(self, ms):
            self.settings = {"delay_ms": ms}

    feed = camera._Feed("camera-2", {"name": "Streamfog VirtualCam"})
    feed.subscribers = [Sub(0), Sub(400)]
    player = Player()
    feed._apply_delay(player)
    feed._apply_delay(player, fresh=False)  # unchanged: not told again
    feed.subscribers[1].settings["delay_ms"] = 250
    feed._apply_delay(player, fresh=False)
    assert player.told == [0.4, 0.25]


def test_the_delay_doesnt_open_the_camera_again():
    a = {"name": "Cam", "path": "p", "delay_ms": 0}
    assert camera._feed_key(a) == camera._feed_key({**a, "delay_ms": 400})


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_camera_dialog_has_a_video_delay(qapp):
    from sprigcast.app.camera_dialog import CameraDialog

    dialog = CameraDialog([CameraDevice(0, "Cam", "p")], {"name": "Cam", "path": "p", "delay_ms": 400})
    assert dialog.delay.value() == 400 and dialog.settings()["delay_ms"] == 400
    dialog.delay.setValue(5000)
    assert dialog.settings()["delay_ms"] == camera.MAX_DELAY_MS
    dialog.deleteLater()
