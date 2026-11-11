"""Camera source against fake `cv2` and `cv2_enumerate_cameras` modules."""

import sys
import time
import types

import numpy as np
import pytest

from sprigcast.core.source import create_source


class FakeCapture:
    opened: list = []
    fail_after: int | None = None  # simulate unplugging after N frames

    def __init__(self, index, backend):
        self.index, self.backend, self.props, self.reads = index, backend, {}, 0
        FakeCapture.opened.append(self)

    def isOpened(self):  # noqa: N802 (OpenCV naming)
        return True

    def set(self, prop, value):
        self.props[prop] = value

    def get(self, prop):
        return self.props.get(prop, 0)

    def read(self):
        time.sleep(0.005)
        self.reads += 1
        if FakeCapture.fail_after is not None and self.reads > FakeCapture.fail_after:
            return False, None
        w, h = int(self.props.get("W", 64)), int(self.props.get("H", 36))
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, 0] = (255, 0, 0)  # blue in the left column (BGR)
        return True, frame

    def release(self):
        pass


@pytest.fixture
def fake_cv2(monkeypatch):
    cv2 = types.ModuleType("cv2")
    cv2.CAP_DSHOW, cv2.CAP_PROP_FOURCC, cv2.CAP_PROP_FPS, cv2.CAP_PROP_BUFFERSIZE = 700, "FOURCC", "FPS", "BUF"
    cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT = "W", "H"
    cv2.VideoCapture = FakeCapture
    cv2.VideoWriter_fourcc = lambda *chars: "".join(chars)
    cv2.flip = lambda frame, code: frame[:, ::-1]
    enum = types.ModuleType("cv2_enumerate_cameras")
    enum.devices = [types.SimpleNamespace(index=0, name="Cam A", path="usb#a"), types.SimpleNamespace(index=1, name="Cam B", path="usb#b")]
    enum.enumerate_cameras = lambda backend: list(enum.devices)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras", enum)
    FakeCapture.opened, FakeCapture.fail_after = [], None
    return enum


def wait_for(predicate, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_lists_cameras(fake_cv2):
    from sprigcast.sources.camera import list_cameras

    assert [c.name for c in list_cameras()] == ["Cam A", "Cam B"]


def test_opens_by_path_even_after_renumbering(fake_cv2):
    fake_cv2.devices.reverse()  # Windows renumbered the cameras
    fake_cv2.devices[0].index, fake_cv2.devices[1].index = 0, 1
    src = create_source("camera", "c", {"name": "Cam B", "path": "usb#b", "width": 64, "height": 36, "fps": 30})
    src.start()
    try:
        assert wait_for(lambda: FakeCapture.opened)
        cap = FakeCapture.opened[0]
        assert cap.index == 0 and cap.backend == 700  # Cam B is now first
        assert cap.props["FOURCC"] == "MJPG" and cap.props["W"] == 64 and cap.props["BUF"] == 1
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        assert frame.shape == (36, 64, 3) and src.channel_order == "BGR"
        assert tuple(frame[0, 0]) == (255, 0, 0)
    finally:
        src.stop()


def test_mirror_flips_without_reopening(fake_cv2):
    settings = {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30}
    src = create_source("camera", "c", settings)
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        src.update({**settings, "mirror": True})
        src.video_frame()
        assert wait_for(lambda: src._frames.peek() is not None)
        assert tuple(src.video_frame()[0, -1]) == (255, 0, 0)  # blue column moved to the right
        assert len(FakeCapture.opened) == 1
    finally:
        src.stop()


def test_reconnects_after_unplug(fake_cv2, monkeypatch):
    from sprigcast.sources import camera

    monkeypatch.setattr(camera, "RETRY_SECONDS", 0.05)
    FakeCapture.fail_after = 3
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a"})
    src.start()
    try:
        assert wait_for(lambda: len(FakeCapture.opened) >= 2)
    finally:
        src.stop()


def test_missing_camera_waits(fake_cv2, monkeypatch):
    from sprigcast.sources import camera

    monkeypatch.setattr(camera, "RETRY_SECONDS", 0.05)
    src = create_source("camera", "c", {"name": "Gone", "path": "usb#gone"})
    src.start()
    try:
        assert wait_for(lambda: src.status == "camera not found, retrying")
        assert FakeCapture.opened == []
    finally:
        src.stop()
