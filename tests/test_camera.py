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


def wait_for(predicate, timeout=5.0):
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
        # The camera thread opens the capture, then sets its mode: wait for the last setting (asserting at once
        # failed now and then in a full run, KeyError FOURCC)
        assert wait_for(lambda: "BUF" in cap.props)
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


def FakeAvFrame():  # noqa: N802 (stands in for the class it used to be)
    """What a webcam's MJPEG decodes to: full-range 4:2:2, here with a blue left column."""
    import av

    bgr = np.zeros((36, 64, 3), dtype=np.uint8)
    bgr[:, 0] = (255, 0, 0)
    return av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="yuvj422p")


class FakeAvContainer:
    rate = 30

    def __init__(self):
        cc = types.SimpleNamespace(width=64, height=36, name="mjpeg", thread_count=0, pix_fmt="yuvj422p")
        self.streams = types.SimpleNamespace(video=[types.SimpleNamespace(codec_context=cc, average_rate=self.rate,
                                                                          base_rate=self.rate)])
        self.closed = False
        self.decoded = 0

    def _decode(self):
        self.decoded += 1
        return [FakeAvFrame()]

    def demux(self, stream):
        while not self.closed:
            time.sleep(0.005)
            yield types.SimpleNamespace(size=1000, decode=self._decode)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_av(monkeypatch, fake_cv2):
    import av

    opened = []

    def fake_open(name, format, options):
        assert format == "dshow"
        if options.get("list_options"):
            raise OSError("listed")  # what real dshow does after printing the modes
        opened.append((name, dict(options)))
        if fake_av_fail:
            raise OSError("no such device")
        container = FakeAvContainer()
        containers.append(container)
        return container

    fake_av_fail = []
    containers = []
    monkeypatch.setattr(av, "formats_available", set(av.formats_available) | {"dshow"})
    monkeypatch.setattr(av, "open", fake_open)
    return types.SimpleNamespace(opened=opened, fail=fake_av_fail, containers=containers)


def test_ffmpeg_is_used_first_and_opencv_is_not_touched(fake_av):
    sys.modules["cv2_enumerate_cameras"].devices[1].path = "\\\\?\\usb#b"  # a real DirectShow device path
    src = create_source("camera", "c", {"name": "Cam B", "path": "\\\\?\\usb#b", "width": 64, "height": 36, "fps": 30,
                                         "mirror": True})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        # The camera's own planes go to the GPU (Y rows, then U | V rows), here flipped: blue is on the right
        assert src.channel_order.startswith("I420/") and src.channel_order.endswith("/pc")
        assert frame.shape == (36 + 18, 64) and src.size == (64, 36) and frame.flags["C_CONTIGUOUS"]
        assert frame[0, -1] > 20 and frame[0, 0] == 0  # Y: blue at the right edge; black is 0 in full range
        assert frame[36, 31] > 160 > 140 > frame[36, 0]  # U (left half): high for blue, on the flipped side
        assert src.status == "capturing 64x36 @ 30"
        name, options = fake_av.opened[0]
        assert name == "video=@device_pnp_\\\\?\\usb#b"  # opened by its device path, not its index
        assert options["video_size"] == "64x36" and options["vcodec"] == "mjpeg"
        assert FakeCapture.opened == []  # OpenCV never used
    finally:
        src.stop()


def test_faster_camera_is_paced_to_the_chosen_fps(fake_av, monkeypatch):
    monkeypatch.setattr(FakeAvContainer, "rate", 120)  # a camera that only gives its fast mode
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 20})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        time.sleep(0.5)
        decoded = fake_av.containers[0].decoded
        assert 5 <= decoded <= 16  # ~20 per second, not one per packet (~100 in that time)
        assert src.status == "capturing 64x36 @ 120, using 20"
        assert fake_av.containers[0].streams.video[0].codec_context.thread_count == 1
    finally:
        src.stop()


def test_falls_back_to_opencv_when_ffmpeg_cannot_open(fake_av):
    fake_av.fail.append(True)
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        assert len(FakeCapture.opened) == 1 and len(fake_av.opened) == 4  # each mode tried by name first
    finally:
        src.stop()


def test_lists_cameras_on_windows_without_loading_opencv(monkeypatch):
    from sprigcast.sources import camera

    backend = types.ModuleType("cv2_enumerate_cameras._windows_backend")
    backend.DSHOW_enumerate_cameras = lambda: [("Cam A", "\\\\?\\usb#a"), ("Cam B", "")]
    monkeypatch.setattr(camera, "_lister", backend)
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.delitem(sys.modules, "cv2_enumerate_cameras", raising=False)
    monkeypatch.setattr(camera.sys, "platform", "win32")
    cams = camera.list_cameras()
    assert [(c.index, c.name, c.path) for c in cams] == [(0, "Cam A", "\\\\?\\usb#a"), (1, "Cam B", "")]
    # Neither OpenCV nor the package (which would import it) got loaded, and cv2 was never hidden
    assert "cv2" not in sys.modules and "cv2_enumerate_cameras" not in sys.modules


def test_lazy_cv2_loads_opencv_only_when_used(monkeypatch):
    import importlib.util

    from sprigcast.core import lazy_cv2

    real = types.ModuleType("cv2")
    real.imwrite = lambda *a: "written"
    monkeypatch.delitem(sys.modules, "cv2", raising=False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object() if name == "cv2" else None)
    monkeypatch.setattr(lazy_cv2.importlib, "import_module", lambda name: sys.modules.setdefault(name, real))
    lazy_cv2.install()
    proxy = sys.modules["cv2"]
    assert proxy is not real  # nothing loaded yet: what windows-capture's `import cv2` gets
    import cv2  # noqa: F401  (a library importing it at the top)
    assert sys.modules["cv2"] is proxy
    assert not hasattr(proxy, "__file__") and getattr(proxy, "__version__", None) is None  # module walkers
    assert sys.modules["cv2"] is proxy
    monkeypatch.setattr(lazy_cv2, "loaded_by", "")
    assert proxy.imwrite() == "written" and sys.modules["cv2"] is real  # loaded on first use
    assert lazy_cv2.loaded_by.startswith("cv2.imwrite from ")
    lazy_cv2.install()  # already loaded: leaves it alone
    assert sys.modules["cv2"] is real


FACECAM_LINES = [
    "DirectShow video device options (from video devices)",
    " Pin \"Capture\" (alternative pin name \"0\")",
    "  pixel_format=nv12  min s=1920x1080 fps=120 max s=1920x1080 fps=120 (tv, bt709/unknown/unknown, topleft)",
    "  pixel_format=nv12  min s=1920x1080 fps=60 max s=1920x1080 fps=60",
    "  pixel_format=yuyv422  min s=1920x1080 fps=30 max s=1920x1080 fps=30",
    "  vcodec=mjpeg  min s=1280x720 fps=30 max s=1280x720 fps=60",
    "  vcodec=h264  min s=1920x1080 fps=30 max s=1920x1080 fps=30",
]


def test_reads_the_cameras_mode_list():
    from sprigcast.sources.camera import parse_dshow_modes

    modes = parse_dshow_modes(FACECAM_LINES)
    assert modes[0] == ("pixel_format", "nv12", 1920, 1080, 120.0, 120.0)
    assert ("vcodec", "mjpeg", 1280, 720, 30.0, 60.0) in modes and len(modes) == 5


def test_picks_the_lightest_mode_that_gives_what_was_asked():
    from sprigcast.sources.camera import choose_modes, parse_dshow_modes

    modes = parse_dshow_modes(FACECAM_LINES)
    # 1080p30: the raw 30 fps mode, not the 120 fps one the camera falls back to
    assert choose_modes(modes, 1920, 1080, 30)[0] == {"video_size": "1920x1080", "framerate": "30",
                                                       "pixel_format": "yuyv422"}
    # 1080p50 isn't offered: the slowest faster mode (60) before 120
    assert [m.get("framerate") for m in choose_modes(modes, 1920, 1080, 50)][:3] == ["60", "120", "30"]  # slower last
    # MJPEG wins when it has the size and rate
    assert choose_modes(modes, 1280, 720, 60) == [{"video_size": "1280x720", "framerate": "60", "vcodec": "mjpeg"},
                                                  {"video_size": "1280x720", "vcodec": "mjpeg"}]
    assert choose_modes([], 1920, 1080, 30) == []


def test_facecam_that_only_does_1080p120():
    """FFmpeg 7 prints the format and the sizes on separate lines; the camera has no 720p mode."""
    from sprigcast.sources.camera import choose_modes, parse_dshow_modes, shrunk_size

    lines = ["DirectShow video device options (from video devices)",
             ' Pin "Capture" (alternative pin name "Capture")',
             "  pixel_format=yuyv422", "  min s=1920x1080 fps=120 max s=1920x1080 fps=120", "",
             "  pixel_format=nv12", "  min s=1920x1080 fps=120 max s=1920x1080 fps=120", ""]
    modes = parse_dshow_modes(lines)
    assert modes == [("pixel_format", "yuyv422", 1920, 1080, 120.0, 120.0),
                     ("pixel_format", "nv12", 1920, 1080, 120.0, 120.0)]
    # 720p30 asked: the 1080p mode it has, NV12 (less data) first
    assert choose_modes(modes, 1280, 720, 30) == [
        {"video_size": "1920x1080", "framerate": "120", "pixel_format": "nv12"},
        {"video_size": "1920x1080", "framerate": "120", "pixel_format": "yuyv422"},
        {"video_size": "1920x1080", "pixel_format": "nv12"},  # then the same without a rate (see below)
        {"video_size": "1920x1080", "pixel_format": "yuyv422"}]
    assert shrunk_size((1920, 1080), (1280, 720)) == (1280, 720)
    assert shrunk_size((1920, 1080), (640, 480)) == (640, 360)  # aspect kept
    assert shrunk_size((1280, 720), (1280, 720)) is None


def test_bigger_camera_frames_are_shrunk_to_the_chosen_size(fake_av):
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a", "width": 32, "height": 18, "fps": 30})
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        assert frame.shape == (18 + 9, 32) and src.size == (32, 18)
        assert src.status.endswith("shrunk to 32x18")
    finally:
        src.stop()


def test_a_camera_shown_small_sends_small_frames_but_keeps_its_size(fake_av):
    """Shown at a quarter of its size or less, frames are made that small on the capture thread (the heavy run
    on 2026-11-17: the camera took 10 ms a frame); its size, which places it, stays the same."""
    src = create_source("camera", "c", {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30})
    src.set_shrink(0.25)
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        frame = src.video_frame()
        assert frame.shape == (10 + 5, 16) and src.size == (64, 36)
        src.set_shrink(1.0)
        src._frames.take()
        assert wait_for(lambda: (f := src._frames.peek()) is not None and f[0].shape == (36 + 18, 64))
        assert src.size == (64, 36)
    finally:
        src.stop()


def test_two_sources_on_one_camera_share_one_open_device(fake_cv2):
    from sprigcast.sources import camera

    settings = {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30}
    wide = create_source("camera", "wide", settings)
    crop = create_source("camera", "crop", {**settings, "mirror": True})  # mirroring is per source
    wide.start()
    crop.start()
    try:
        assert wait_for(lambda: wide._frames.peek() is not None and crop._frames.peek() is not None)
        assert len(FakeCapture.opened) == 1 and wide.shared and crop.shared
        assert tuple(wide.video_frame()[0, 0]) == (255, 0, 0) and tuple(crop.video_frame()[0, -1]) == (255, 0, 0)
        assert wide.status == crop.status and wide.status.startswith("capturing")

        wide.sleep()  # one goes off screen: the other keeps the camera
        crop.video_frame()
        assert wait_for(lambda: crop._frames.peek() is not None)
        assert wide.status == "paused (not on screen)" and not crop.shared and wide.video_frame() is None
        wide.wake()
        assert wait_for(lambda: wide._frames.peek() is not None) and len(FakeCapture.opened) == 1

        crop.update({**settings, "width": 32, "height": 18})  # a smaller mode: still the one camera, shown smaller
        assert wait_for(lambda: (f := crop._frames.peek()) is not None and crop.size == (32, 18))
        assert len(FakeCapture.opened) == 1 and wide.shared and len(camera._feeds) == 1
        assert wide.size == (64, 36)
    finally:
        wide.stop()
        crop.stop()
    assert camera._feeds == {}


def test_changing_the_mode_of_an_unshared_camera_reopens_on_the_same_thread(fake_cv2):
    from sprigcast.sources import camera

    settings = {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 30}
    src = create_source("camera", "c", settings)
    src.start()
    try:
        assert wait_for(lambda: src._frames.peek() is not None)
        feed = src._feed
        src.update({**settings, "width": 32, "height": 18})
        assert wait_for(lambda: len(FakeCapture.opened) == 2 and FakeCapture.opened[1].props.get("W") == 32)
        assert src._feed is feed and list(camera._feeds.values()) == [feed]
    finally:
        src.stop()


def test_own_virtual_camera_is_left_out_without_shifting_indexes(monkeypatch):
    from sprigcast.sources import camera as cam

    monkeypatch.setattr(cam, "_dshow_list", lambda: [("Facecam", "a"), ("SprigCast Camera", ""), ("Cam B", "b")])
    assert [(c.index, c.name) for c in cam.list_cameras()] == [(0, "Facecam"), (2, "Cam B")]


def test_a_camera_at_59_9999_fps_opens_in_its_own_mode():
    """The Elgato Virtual Camera says fps=59.9999 (an interval of 166667 x 100 ns). "60" and "59.9999"
    both reach dshow as 166666, which it refused, so the camera fell back to its default YUYV mode."""
    from fractions import Fraction

    from sprigcast.sources.camera import RATE_LIMIT, _rate, choose_modes, parse_dshow_modes

    lines = ["DirectShow video device options (from video devices)",
             ' Pin "Video" (alternative pin name "Video")',
             "  pixel_format=yuyv422", "  min s=1920x1080 fps=59.9999 max s=1920x1080 fps=59.9999", "",
             "  pixel_format=nv12", "  min s=1920x1080 fps=59.9999 max s=1920x1080 fps=59.9999", ""]
    modes = parse_dshow_modes(lines)
    for want_fps in (30, 60):
        first = choose_modes(modes, 1280, 720, want_fps)[0]
        assert first == {"video_size": "1920x1080", "framerate": "125039/2084", "pixel_format": "nv12"}
    # What dshow does with the rate we pass: interval = den * 10^7 / num, truncated. FFmpeg keeps the two numbers
    # as given only up to 1,001,000: "10000000/166667" became 1000018/16667, an interval of 166666, and every
    # camera refused it (PC 2026-11-21; checked against av_parse_video_rate itself).
    for rate, interval in ((59.9999, 166667), (60.0002, 166666), (30.0, 333333), (120.0, 83333), (29.97, 333667),
                           (59.94, 166834), (23.976, 417084), (119.88, 83417), (25.0001, 399998), (7.5, 1333333)):
        r = Fraction(_rate(rate))
        assert max(r.numerator, r.denominator) <= RATE_LIMIT, rate
        assert r.denominator * 10_000_000 // r.numerator == interval, rate
    assert _rate(30.0) == "30" and _rate(120.0) == "120"


def test_a_camera_that_refuses_every_rate_still_gets_nv12():
    """The Elgato Virtual Camera lists 1080p at 59.9999 fps in YUYV and NV12 but fails to open with any
    framerate asked; with size and format alone NV12 opens. Without a rate-less
    try it fell back to its default YUYV: 74 MB for the camera instead of 30 on the user's PC."""
    from sprigcast.sources.camera import choose_modes, mode_cost, plain_mode

    modes = [("pixel_format", "yuyv422", 1920, 1080, 59.9999, 59.9999),
             ("pixel_format", "nv12", 1920, 1080, 59.9999, 59.9999)]
    options = choose_modes(modes, 1280, 720, 30)
    assert [o.get("framerate") for o in options] == ["125039/2084", "125039/2084", None, None]
    assert options[2] == {"video_size": "1920x1080", "pixel_format": "nv12"}  # before the default YUYV
    assert plain_mode(options[2]) == "1920 x 1080 at its own rate (NV12)"
    assert mode_cost(options[2], 30) < mode_cost(options[3], 30)


def test_camera_pictures_go_up_as_yuv_planes_and_mirror_cleanly(monkeypatch):
    import av

    from sprigcast.sources import camera

    bgr = np.zeros((36, 64, 3), dtype=np.uint8)
    bgr[:, :8] = (0, 0, 255)  # red at the left
    nv12 = av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="nv12")  # the Elgato's NV12
    array, order, size = camera.camera_picture(nv12, None)
    assert order == "I420/601/tv" and size == (64, 36) and array.shape == (54, 64)
    flipped = camera.mirror_picture(array, order, size)
    assert (flipped[:36] == array[:36, ::-1]).all()
    assert (flipped[36:, :32] == array[36:, :32][:, ::-1]).all() and (flipped[36:, 32:] == array[36:, 32:][:, ::-1]).all()
    assert flipped[36, 63] > 200 > flipped[36, 32]  # V (right half): red now at the far right
    monkeypatch.setattr(camera, "PLANAR", False)  # the escape hatch: BGR as before
    array, order, size = camera.camera_picture(nv12, (32, 18))
    assert order == "BGR" and array.shape == (18, 32, 3) and size == (32, 18)


def test_nv12_and_mjpeg_pictures_go_up_without_a_conversion_pass(monkeypatch):
    import av

    from sprigcast.sources import camera

    rng = np.random.default_rng(1)
    bgr = rng.integers(0, 255, (36, 64, 3), dtype=np.uint8)
    nv12 = av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="nv12")
    expected, order, _ = camera.camera_picture(nv12.reformat(format="yuv420p"), None)  # the old way: convert first
    calls = []
    real = camera.reformat
    monkeypatch.setattr(camera, "reformat", lambda *a, **k: calls.append(k) or real(*a, **k))
    array, order2, size = camera.camera_picture(nv12, None)
    assert not calls and order2 == order and size == (64, 36)
    assert (array == expected).all()  # same picture, one pass less
    camera.camera_picture(nv12, (32, 18))  # a shrink still converts (and scales) in one pass
    assert len(calls) == 1
    jpeg = av.VideoFrame.from_ndarray(bgr, format="bgr24").reformat(format="yuvj420p")
    calls.clear()
    array, order, _ = camera.camera_picture(jpeg, None)
    assert not calls and order.endswith("/pc")  # MJPEG's full range is kept and labelled


def test_one_camera_in_two_modes_opens_once_at_the_bigger(fake_cv2):
    """The PC's Gameplay bloom (2026-11-21): the HD60 X saved at 1080p60 in one source and 720p30 in another was
    opened twice, the second open refused every mode and came up in the device's own; SprigCast doubled its CPU.
    Now the camera opens once, at the bigger mode, and the smaller source shows that picture at its own size."""
    from sprigcast.sources import camera

    small = create_source("camera", "small", {"name": "Cam A", "path": "usb#a", "width": 32, "height": 18, "fps": 30})
    big = create_source("camera", "big", {"name": "Cam A", "path": "usb#a", "width": 64, "height": 36, "fps": 60})
    small.start()
    try:
        assert wait_for(lambda: small._frames.peek() is not None)
        big.start()
        assert wait_for(lambda: (f := big._frames.peek()) is not None and f[0].shape[1] == 64)
        assert len(camera._feeds) == 1 and small.shared and big.shared
        assert FakeCapture.opened[-1].props["W"] == 64 and FakeCapture.opened[-1].props["FPS"] == 60
        assert small._feed.settings["fps"] == 60
        assert wait_for(lambda: small._frames.peek() is not None and small.size == (32, 18))
        assert big.size == (64, 36)
        # With the engine's own player the feed says what it plays: the smaller source is laid out at its size.
        assert small.engine_layout is None
        small._feed.picture = (64, 36)
        assert small.engine_layout == (32, 18) and big.engine_layout is None
        small.set_shrink(1.0)
        big.set_shrink(0.25)  # the big one shown small: the small one still needs half the picture
        assert small._feed.shrink() == 0.5
        # The camera came up in its own, bigger mode (none asked was taken): no picture bigger than the biggest
        # source is made (PC 2026-11-21: 1.1 GB of full-size pictures for 1080p and 720p sources).
        big.set_shrink(1.0)
        small._feed.picture = (256, 144)
        assert small._feed.shrink() == 0.25
        small._feed.picture = (64, 36)
        big.stop()  # it doesn't shrink back while the camera runs
        assert small._feed.settings["width"] == 64
    finally:
        small.stop()
        big.stop()
    assert camera._feeds == {}


def test_a_reopened_camera_uses_the_modes_it_listed_before(monkeypatch):
    """PC 2026-11-21: a camera reopened came up as MJPEG, then NV12, then BGR, as its list (asked while it was still
    closing) came back empty or short. The last list it gave is used when it gives none."""
    from sprigcast.sources import camera

    monkeypatch.setattr(camera, "_listed", {})
    answers = [[("vcodec", "mjpeg", 1280, 720, 30.0, 30.0)], []]
    monkeypatch.setattr(camera, "dshow_modes", lambda name: answers.pop(0))
    first = camera.listed_modes("video=Cam")
    assert camera.listed_modes("video=Cam") == first and first


def test_the_camera_lister_is_loaded_ahead(monkeypatch):
    """PC 2026-11-21: opening a camera's properties held the window 318 ms loading what lists cameras. The warm-up
    loads it at start, off the window's thread, and says which lister there is."""
    import sys

    from sprigcast.sources import camera

    assert camera.warm_lister() == "" or sys.platform == "win32"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(camera, "_dshow_backend", lambda: object())
    assert camera.warm_lister() == "light"
    loaded = []
    monkeypatch.setattr(camera, "_dshow_backend", lambda: None)
    monkeypatch.setattr(camera, "_cv2", lambda: loaded.append("cv2"))
    monkeypatch.setitem(sys.modules, "cv2_enumerate_cameras", object())
    assert camera.warm_lister() == "opencv" and loaded == ["cv2"]
    monkeypatch.setattr(camera, "_cv2", lambda: (_ for _ in ()).throw(RuntimeError("no OpenCV")))
    assert camera.warm_lister() == ""


def test_other_sizes_are_tried_before_the_cameras_own_mode():
    """PC 2026-11-21: the HD60 X with no signal refused the size asked and came up in its own mode, 32 MB a
    picture. Its other sizes come first: the next bigger one, then the biggest below."""
    from sprigcast.sources import camera

    modes = [("pixel_format", "nv12", 1920, 1080, 30.0, 60.0), ("pixel_format", "nv12", 2560, 1440, 30.0, 60.0),
             ("pixel_format", "bgr0", 3840, 2160, 30.0, 60.0), ("pixel_format", "nv12", 3840, 2160, 30.0, 60.0),
             ("pixel_format", "nv12", 1280, 720, 30.0, 60.0), ("pixel_format", "nv12", 640, 480, 30.0, 60.0)]
    first = camera.choose_modes(modes, 1920, 1080, 60)
    assert {m["video_size"] for m in first} == {"1920x1080"}
    more = camera.other_modes(modes, 1920, 1080, 60, first)
    sizes = list(dict.fromkeys(m["video_size"] for m in more))
    assert sizes == ["2560x1440", "1280x720", "3840x2160"]  # OTHER_SIZES of them: next above, biggest below
    assert more[0] == {"video_size": "2560x1440", "framerate": "60", "pixel_format": "nv12"}
    assert [m for m in more if m["video_size"] == "3840x2160"][0]["pixel_format"] == "nv12"  # least data first
    assert camera.other_modes([], 1920, 1080, 60, []) == []


def test_the_camera_lister_is_never_left_in_sys_modules(monkeypatch, tmp_path):
    """PC 2026-11-21: PySide imports again, by name, every compiled module in sys.modules the first time a new Qt
    class is used. The lister sat there as "cv2_enumerate_cameras._windows_backend", so that import loaded its
    package and OpenCV with it (83 MB, a 318-407 ms stall when the first Control API app connected)."""
    import importlib.machinery
    import importlib.util

    from sprigcast.sources import camera

    package = tmp_path / "cv2_enumerate_cameras"
    package.mkdir()
    (package / "_windows_backend.pyd").write_bytes(b"")
    name = "cv2_enumerate_cameras._windows_backend"
    made = types.ModuleType(name)

    class Loader:
        def __init__(self, name, path):
            pass

        def create_module(self, spec):
            sys.modules[name] = made  # as Python does for a compiled module made this way
            return made

        def exec_module(self, module):
            pass

    monkeypatch.setattr(camera, "_lister", None)
    monkeypatch.setattr(camera.sys, "platform", "win32")
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: types.SimpleNamespace(
        submodule_search_locations=[str(package)]))
    monkeypatch.setattr(importlib.machinery, "ExtensionFileLoader", Loader)
    monkeypatch.setattr(camera.os if hasattr(camera, "os") else __import__("os"), "add_dll_directory",
                        lambda path: None, raising=False)
    try:
        assert camera._dshow_backend() is made and camera._dshow_backend() is made
        assert name not in sys.modules
    finally:
        sys.modules.pop(name, None)


def test_the_log_says_every_size_a_camera_offers():
    """PC 2026-11-21: the HD60 X opened at 2560x1440 for sources saved as 1080p and 720p, and the log couldn't say
    whether the card offered anything smaller. Each open now lists the sizes with their formats and fastest rate."""
    from sprigcast.sources.camera import modes_line

    modes = [("pixel_format", "nv12", 2560, 1440, 30.0, 60.0), ("pixel_format", "yuyv422", 1920, 1080, 30.0, 59.94),
             ("vcodec", "mjpeg", 1920, 1080, 30.0, 60.0), ("pixel_format", "nv12", 2560, 1440, 30.0, 30.0)]
    assert modes_line(modes) == "1920x1080 mjpeg/yuyv422 up to 60 fps; 2560x1440 nv12 up to 60 fps"
    assert modes_line([]) == "no list of modes"


def test_two_cameras_list_their_modes_one_at_a_time(monkeypatch):
    """PC 2026-11-21: two cameras opening together at start. The modes come as FFmpeg's log lines and the capture
    takes every thread's: the HD60 X took the Streamfog camera's 35 modes on top of its own, and the Streamfog
    camera, left with no list, came up as bgr24 at 30 fps and not nv12."""
    import threading
    import time

    import av

    from sprigcast.sources import camera

    inside, most = [0], [0]

    class Opened:
        def close(self):
            pass

    def fake_open(name, format=None, options=None):  # noqa: A002
        inside[0] += 1
        most[0] = max(most[0], inside[0])
        time.sleep(0.05)
        inside[0] -= 1
        return Opened()

    monkeypatch.setattr(av, "open", fake_open)
    threads = [threading.Thread(target=camera.dshow_modes, args=(f"video=Camera {n}",)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert most[0] == 1


def test_a_camera_listing_up_to_59_9999_gets_60_when_asked():
    """PC 2026-11-21: the Streamfog camera lists 30 to 59.9999 fps. Asked for 60 it was sent a rate FFmpeg turned
    into the wrong interval, refused it, and came up at its own 30."""
    from fractions import Fraction

    from sprigcast.sources.camera import choose_modes

    modes = [("pixel_format", "nv12", 1280, 720, 30.0, 59.9999)]
    first = choose_modes(modes, 1280, 720, 60)[0]
    r = Fraction(first["framerate"])
    assert r.denominator * 10_000_000 // r.numerator == 166667 and r.numerator <= 1_001_000
    assert choose_modes(modes, 1280, 720, 30)[0]["framerate"] == "30"


def test_a_capture_card_is_asked_for_the_size_wanted_before_its_bigger_listed_one():
    """PC 2026-11-21: the HD60 X, fed 1440p, listed 2560x1440 alone and was opened at that for a 720p source, though
    Elgato says it sends 1080p and 720p for that input. The size wanted is asked first, always with a rate; a
    device that gives another size isn't asked again."""
    from fractions import Fraction

    from sprigcast.sources import camera as cam_mod
    from sprigcast.sources.camera import asked_first, not_given

    cam_mod._not_given.clear()
    card = [("pixel_format", "nv12", 2560, 1440, 59.9999, 59.9999), ("pixel_format", "yuyv422", 2560, 1440, 30.0, 30.0)]
    first = asked_first(card, 1280, 720, 60, "card")
    assert [o["video_size"] for o in first] == ["1280x720"] * 4 and all("framerate" in o for o in first)
    assert [o["pixel_format"] for o in first] == ["nv12", "yuyv422", "nv12", "yuyv422"]
    assert first[0]["framerate"] == "60"
    exact = Fraction(first[2]["framerate"])  # the card's own 59.9999, as the interval dshow matches
    assert exact.denominator * 10_000_000 // exact.numerator == 166667
    # A size the list has, a list with nothing bigger, or no list: nothing to ask first.
    assert asked_first(card, 2560, 1440, 60, "card") == []
    assert asked_first(card, 3840, 2160, 60, "card") == [] and asked_first([], 1280, 720, 60, "card") == []
    webcam = [("vcodec", "mjpeg", 1280, 720, 30.0, 60.0), ("pixel_format", "nv12", 1920, 1080, 30.0, 30.0)]
    assert asked_first(webcam, 1280, 720, 60, "webcam") == []
    # It opened at its listed size after all: said once, and not asked again this run.
    assert not not_given("card", (1280, 720), (1280, 720)) and asked_first(card, 1280, 720, 60, "card")
    assert not_given("card", (1280, 720), (2560, 1440)) and not not_given("card", (1280, 720), (2560, 1440))
    assert asked_first(card, 1280, 720, 60, "card") == [] and asked_first(card, 1920, 1080, 60, "card")
    # The next run (PC 2026-11-24: refused on every start): not asked again while the card lists the same size...
    cam_mod._not_given.clear()
    assert asked_first(card, 1280, 720, 60, "card") == []
    # ...but asked afresh once its input changes (a console at 1080p lists 1920x1080).
    cam_mod._not_given.clear()
    console = [("pixel_format", "nv12", 1920, 1080, 59.9999, 59.9999)]
    assert asked_first(console, 1280, 720, 60, "card")
    from PySide6.QtCore import QSettings

    QSettings().remove(cam_mod.NOT_GIVEN_KEY)
    cam_mod._not_given.clear()


def test_the_engine_tries_the_size_wanted_first(monkeypatch):
    from types import SimpleNamespace

    from sprigcast.sources import camera as cam_mod

    cam_mod._not_given.clear()
    card = [("pixel_format", "nv12", 2560, 1440, 60.0, 60.0)]
    monkeypatch.setattr(cam_mod, "listed_modes", lambda name: card)
    feed = cam_mod._Feed("camera-1", {"name": "Card", "path": "", "width": 1280, "height": 720, "fps": 60})
    _fmt, choices = cam_mod._engine_choices(feed, SimpleNamespace(name="Card", path=""))
    sizes = [mode.get("video_size") for _name, mode in choices]
    assert sizes[:3] == ["1280x720", "1280x720", "2560x1440"] and feed.asked_first == [m for _n, m in choices[:2]]
    cam_mod._not_given.add(("Card", (1280, 720)))
    _fmt, choices = cam_mod._engine_choices(feed, SimpleNamespace(name="Card", path=""))
    assert choices[0][1]["video_size"] == "2560x1440" and feed.asked_first == []
    cam_mod._not_given.clear()


def test_the_whole_list_is_kept_for_the_log_as_it_came():
    from sprigcast.sources.camera import parse_dshow_modes, raw_modes_line

    lines = ['DirectShow video device options (from video devices)', ' Pin "Video" (alternative pin name "0")',
             "  pixel_format=nv12  min s=2560x1440 fps=59.9999 max s=2560x1440 fps=59.9999",
             "  pixel_format=nv12  min s=2560x1440 fps=59.9999 max s=2560x1440 fps=59.9999 (tv, bt709/bt709/unknown)",
             "  unknown compression type 0x30313050  min s=1920x1080 fps=60 max s=1920x1080 fps=60",
             "  pixel_format=yuyv422  min s=640x360 fps=30 max s=1920x1080 fps=60"]
    assert [m[2:4] for m in parse_dshow_modes(lines)] == [(2560, 1440), (2560, 1440)]  # what SprigCast reads
    assert raw_modes_line(lines) == "nv12 2560x1440 60; 0x30313050 1920x1080 60; yuyv422 640x360-1920x1080 60"
    assert raw_modes_line([]) == "nothing"


def test_a_capture_card_given_a_signal_is_opened_again():
    """PC 2026-11-24: the HD60 X listed only 2560x1440 with no signal and was opened at that; a console's 1080p then
    showed "INVALID OUTPUT RESOLUTION SELECTED". Dark, then lit: opened again with its list read afresh."""
    import numpy as np

    from sprigcast.sources import camera as cam_mod

    watch = cam_mod.SignalWatch()
    assert watch.due(0.0) and not watch.due(1.0) and watch.due(3.0)
    black = np.full((1080, 1280), 16, dtype=np.uint8)  # I420 rows: Y then U and V
    black[720:] = 128
    lit = black.copy()
    lit[:720, :640] = 200
    assert not watch.look(*cam_mod.picture_light((black, "I420/709/tv")))
    assert not watch.look(*cam_mod.picture_light((black, "I420/709/tv")))
    assert watch.look(*cam_mod.picture_light((lit, "I420/709/tv")))
    assert not watch.look(*cam_mod.picture_light((lit, "I420/709/tv")))  # once per signal, never a loop
    # Once lit, a dark scene (a game's loading screen) and back never reopens it, and no more looks are taken.
    assert not watch.look(*cam_mod.picture_light((black, "I420/709/tv")))
    assert not watch.look(*cam_mod.picture_light((lit, "I420/709/tv")))
    assert not watch.due(100.0)
    # A picture lit from the start (a card opened with its signal on) never reopens.
    fresh = cam_mod.SignalWatch()
    assert not fresh.look(*cam_mod.picture_light((np.full((8, 8, 4), 180, dtype=np.uint8), "RGBA")))
    assert not fresh.look(*cam_mod.picture_light((black, "I420/709/tv")))
    assert not fresh.look(*cam_mod.picture_light((lit, "I420/709/tv"))) and not fresh.due(10.0)
    assert cam_mod.picture_light(None) is None and cam_mod.picture_light("blank") is None

    cam_mod._not_given.update({("Card", (1280, 720)), ("Webcam", (1280, 720))})
    cam_mod.forget_refusals("Card")
    assert cam_mod._not_given == {("Webcam", (1280, 720))}
    cam_mod._not_given.clear()


def test_only_a_one_size_card_opened_bigger_is_looked_at():
    from types import SimpleNamespace

    import numpy as np

    from sprigcast.sources import camera as cam_mod

    feed = cam_mod._Feed("camera-1", {"name": "Card", "path": "", "width": 1280, "height": 720, "fps": 30})
    taken = []
    player = SimpleNamespace(video_frame=lambda: taken.append(1) or (np.zeros((8, 8, 4), np.uint8), "RGBA"))
    feed.modes_seen = [("pixel_format", "nv12", 2560, 1440, 60.0, 60.0)]
    feed.given = (1280, 720)  # given what was asked: nothing to look at
    assert not feed._signal_came(player, 1280, 720, cam_mod.SignalWatch()) and not taken
    feed.given = (2560, 1440)
    assert not feed._signal_came(player, 1280, 720, cam_mod.SignalWatch()) and taken
    taken.clear()
    feed.modes_seen.append(("pixel_format", "nv12", 1920, 1080, 60.0, 60.0))  # a webcam's list: never looked at
    assert not feed._signal_came(player, 1280, 720, cam_mod.SignalWatch()) and not taken


def test_a_card_offering_one_bigger_size_is_said_once(caplog):
    import logging

    from sprigcast.sources import camera as cam_mod

    cam_mod._said_only_size.clear()
    feed = cam_mod._Feed("camera-3", {"name": "Card", "path": "", "width": 1280, "height": 720, "fps": 30})
    feed.modes_seen = [("pixel_format", "nv12", 2560, 1440, 60.0, 60.0)]
    feed.given = (2560, 1440)
    with caplog.at_level(logging.INFO, logger="sprigcast.sources.camera"):
        feed._say_only_size("Card", 1280, 720)
        feed._say_only_size("Card", 1280, 720)
    lines = [r.getMessage() for r in caplog.records if "offers apps only" in r.getMessage()]
    assert len(lines) == 1 and "Input EDID mode to Internal" in lines[0]
    cam_mod._said_only_size.clear()


def test_custom_device_mode_is_tried_first():
    """Custom device mode (like a capture app's device properties): the size, rate and format picked are asked of
    the device before anything SprigCast would choose; the source's size is still what's shown."""
    from types import SimpleNamespace

    from sprigcast.sources import camera as cam_mod

    card = [("pixel_format", "nv12", 1920, 1080, 59.9999, 59.9999), ("pixel_format", "yuyv422", 1920, 1080, 59.9999,
                                                                      59.9999),
            ("pixel_format", "nv12", 1280, 720, 59.9999, 59.9999)]
    assert cam_mod.custom_modes({"capture_mode": "auto", "capture_size": [1920, 1080]}, card) == []
    picked = cam_mod.custom_modes({"capture_mode": "custom", "capture_size": [1920, 1080], "capture_fps": 60,
                                   "capture_format": "yuyv422"}, card)
    assert picked[0] == {"video_size": "1920x1080", "framerate": "60", "pixel_format": "yuyv422"}
    assert all(o["pixel_format"] == "yuyv422" for o in picked)
    any_format = cam_mod.custom_modes({"capture_mode": "custom", "capture_size": [1920, 1080], "capture_fps": 60}, card)
    assert [o["pixel_format"] for o in any_format][:1] == ["nv12"] and {o["pixel_format"] for o in any_format} == {
        "nv12", "yuyv422"}
    unlisted = cam_mod.custom_modes({"capture_mode": "custom", "capture_size": [3840, 2160], "capture_fps": 30}, card)
    assert unlisted == [{"video_size": "3840x2160", "framerate": "30"}]

    cam_mod._not_given.clear()
    feed = cam_mod._Feed("camera-1", {"name": "Card", "path": "", "width": 1280, "height": 720, "fps": 60,
                                      "capture_mode": "custom", "capture_size": [1920, 1080], "capture_fps": 60,
                                      "capture_format": "nv12"})
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(cam_mod, "listed_modes", lambda name: card)
    try:
        _fmt, choices = cam_mod._engine_choices(feed, SimpleNamespace(name="Card", path=""))
    finally:
        mp.undo()
    assert choices[0][1]["video_size"] == "1920x1080" and choices[0][1]["pixel_format"] == "nv12"
    assert feed.asked_first == []


def test_the_capture_dialog_keeps_a_custom_device_mode():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app.camera_dialog import CameraDialog
    from sprigcast.sources.camera import CameraDevice

    card = [("pixel_format", "nv12", 2560, 1440, 59.9999, 59.9999), ("pixel_format", "nv12", 1920, 1080, 59.9999,
                                                                      59.9999)]
    cams = [CameraDevice(0, "Game Capture HD60 X", "")]
    d = CameraDialog(cams, {"width": 1280, "height": 720, "fps": 60}, modes=card)
    assert d.settings()["capture_mode"] == "auto" and not d.custom_box.isVisibleTo(d)
    d.custom.setChecked(True)
    assert [d.device_size.itemData(i) for i in range(d.device_size.count())] == [(2560, 1440), (1920, 1080)]
    d.device_size.setCurrentIndex(1)
    got = d.settings()
    assert got["capture_mode"] == "custom" and got["capture_size"] == [1920, 1080] and got["capture_fps"] == 60
    assert "1920 x 1080" in d.note.text() and "1280 x 720" in d.note.text()
    again = CameraDialog(cams, got, modes=card)
    assert again.custom.isChecked() and again.device_size.currentData() == (1920, 1080)
    d.deleteLater()
    again.deleteLater()
