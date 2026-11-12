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


class FakeAvFrame:
    def to_ndarray(self, format):
        assert format == "bgr24"
        frame = np.zeros((36, 64, 3), dtype=np.uint8)
        frame[:, 0] = (255, 0, 0)
        return frame


class FakeAvContainer:
    def __init__(self):
        cc = types.SimpleNamespace(width=64, height=36)
        self.streams = types.SimpleNamespace(video=[types.SimpleNamespace(codec_context=cc, average_rate=30, base_rate=30)])
        self.closed = False

    def demux(self, stream):
        while not self.closed:
            time.sleep(0.005)
            yield types.SimpleNamespace(decode=lambda: [FakeAvFrame()])

    def close(self):
        self.closed = True


@pytest.fixture
def fake_av(monkeypatch, fake_cv2):
    import av

    opened = []

    def fake_open(name, format, options):
        assert format == "dshow"
        opened.append((name, dict(options)))
        if fake_av_fail:
            raise OSError("no such device")
        return FakeAvContainer()

    fake_av_fail = []
    monkeypatch.setattr(av, "formats_available", set(av.formats_available) | {"dshow"})
    monkeypatch.setattr(av, "open", fake_open)
    return types.SimpleNamespace(opened=opened, fail=fake_av_fail)


def test_ffmpeg_is_used_first_and_opencv_is_not_touched(fake_av):
    sys.modules["cv2_enumerate_cameras"].devices[1].path = "\\\\?\\usb#b"  # a real DirectShow device path
    src = create_source("camera", "c", {"name": "Cam B", "path": "\\\\?\\usb#b", "width": 64, "height": 36, "fps": 30,
                                         "mirror": True})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        assert tuple(frame[0, -1]) == (255, 0, 0) and frame.flags["C_CONTIGUOUS"]  # mirrored
        assert src.status == "capturing 64x36 @ 30"
        name, options = fake_av.opened[0]
        assert name == "video=@device_pnp_\\\\?\\usb#b"  # opened by its device path, not its index
        assert options["video_size"] == "64x36" and options["vcodec"] == "mjpeg"
        assert FakeCapture.opened == []  # OpenCV never used
    finally:
        src.stop()


def test_falls_back_to_opencv_when_ffmpeg_cannot_open(fake_av):
    fake_av.fail.append(True)
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        assert len(FakeCapture.opened) == 1 and len(fake_av.opened) == 3  # each mode tried by name first
    finally:
        src.stop()


def test_lists_cameras_on_windows_without_loading_opencv(monkeypatch):
    from sprigcast.sources import camera

    seen = {}
    pkg = types.ModuleType("cv2_enumerate_cameras")
    pkg.__path__ = []
    backend = types.ModuleType("cv2_enumerate_cameras._windows_backend")

    def listing():
        seen["cv2"] = sys.modules.get("cv2", "absent")
        return [("Cam A", "\\\\?\\usb#a"), ("Cam B", "")]

    backend.DSHOW_enumerate_cameras = listing
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras", pkg)
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras._windows_backend", backend)
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.setattr(camera.sys, "platform", "win32")
    cams = camera.list_cameras()
    assert [(c.index, c.name, c.path) for c in cams] == [(0, "Cam A", "\\\\?\\usb#a"), (1, "Cam B", "")]
    assert "cv2" not in sys.modules
