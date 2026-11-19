"""Cameras inside the native engine (docs/native-engine.md "Capture"): the engine's player opens the camera through
FFmpeg's input devices and always shows its newest picture. FFmpeg's "lavfi" test pictures stand in for a camera,
so these run wherever the player can be built (dshow is Windows only)."""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast import sprigcore
from sprigcast.core.source import create_source
from sprigcast.sources import camera


def _wait(check, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def devices(player_lib):  # noqa: F811
    if not sprigcore.camera_ready():
        pytest.skip("FFmpeg's input devices (libavdevice) aren't here")
    return True


def _player(source: str, fps=0, mirror=False, limit=(0, 0)):
    player = sprigcore.MediaPlayer(playing=True)
    player.set_picture("", limit)
    player.set_device("lavfi", [(source, "")], fps, mirror)
    return player


def _picture(player):
    got = []
    assert _wait(lambda: got.append(player.video_frame()) or got[-1] is not None)
    return got[-1]


def test_a_camera_shows_its_newest_picture_and_never_ends(devices):
    player = _player("testsrc2=size=64x48:rate=30,realtime")
    try:
        pixels, order = _picture(player)
        assert order.startswith("I420/") and pixels.shape == (48 + 24, 64)  # 4:2:0 planes, as the camera sends
        info = player.info()
        assert info.status == b"ok" and (info.width, info.height) == (64, 48) and info.state_name == "playing"
        time.sleep(0.5)
        decoded = player.info().decoded
        assert decoded >= 10  # it kept reading: no clock to wait for, no backlog
        assert _picture(player) is not None  # a newer one again
    finally:
        player.close()


def test_a_cameras_422_goes_up_as_420_planes_and_can_be_mirrored(devices):
    straight = _player("testsrc2=size=64x48:rate=30,format=yuyv422,realtime")
    flipped = _player("testsrc2=size=64x48:rate=30,format=yuyv422,realtime", mirror=True)
    try:
        a, order = _picture(straight)
        b, order_b = _picture(flipped)
        assert order.startswith("I420/") and order_b == order
        assert a.shape == (48 + 24, 64)
        # testsrc2 moves, so compare a still part: its colour bars' order is reversed left to right
        assert not np.array_equal(a[:48], b[:48])
        assert abs(int(a[10, 2]) - int(b[10, 61])) < 40 and abs(int(a[10, 61]) - int(b[10, 2])) < 40
    finally:
        straight.close()
        flipped.close()


def test_a_camera_faster_than_wanted_is_paced(devices):
    fast = _player("testsrc2=size=32x24:rate=120,realtime", fps=30)
    try:
        assert _picture(fast) is not None
        start = fast.info().decoded
        time.sleep(1.0)
        rate = fast.info().decoded - start
        assert 15 <= rate <= 45  # about 30 a second, not 120
    finally:
        fast.close()


def test_a_camera_that_wont_open_says_so_and_keeps_trying(devices):
    player = _player("no-such-filter-here")
    try:
        assert _wait(lambda: player.info().status.startswith(b"camera not available"))
        assert player.info().thread  # still trying, every 2 s
    finally:
        player.close()


def test_the_camera_source_hands_the_engine_its_player(devices, monkeypatch):
    """CameraSource with the engine: the feed opens the camera in the engine's player (here lavfi through a stand-in
    device), the source hands it to the render thread, and reads it back only for frames Python draws."""
    monkeypatch.setattr(sprigcore, "use_native_camera", lambda: True)
    monkeypatch.setattr(camera, "find_camera", lambda name, path: camera.CameraDevice(0, "Test cam", ""))
    monkeypatch.setattr(camera, "_engine_choices", lambda feed, device: ("lavfi", [
        ("testsrc2=size=64x48:rate=30,realtime", "")]))
    src = create_source("camera", "cam", {"name": "Test cam", "width": 64, "height": 48, "fps": 30})
    src.start()
    try:
        assert _wait(lambda: src.engine_player is not None)
        assert _wait(lambda: src.status.startswith("capturing 64x48"))
        src.engine_tick()
        assert src.size == (64, 48)
        assert _wait(lambda: src.video_frame() is not None)
        assert src.channel_order.startswith("I420/")
    finally:
        src.stop()
    assert src.engine_player is None
