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


# A phone's screen (sources/android_screen.py): H.264 over a local socket, its own black bars seen and left off.

AUD = b"\x00\x00\x00\x01\x09\xf0"  # an access unit delimiter: tells the reader the picture before it is whole


def _h264(w, h, count, bars=0, level=200):
    """`count` pictures as Annex B H.264, one bytes per picture: `level` grey with `bars` black on the left and
    right."""
    from fractions import Fraction

    import av

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base = Fraction(1, 30)
    enc.options = {"preset": "ultrafast", "tune": "zerolatency", "bframes": "0",
                   "x264-params": "keyint=30:repeat-headers=1"}
    out = []
    for i in range(count):
        img = np.zeros((h, w, 3), np.uint8)
        img[:, bars:w - bars] = level
        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        out += [bytes(p) for p in enc.encode(frame)]
    return out + [bytes(p) for p in enc.encode(None)]


def _phone(player):
    """A listening socket the player is opened on, as the Android Screen source does."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(2)
    server.settimeout(5)
    player.set_device("h264", [(f"tcp://127.0.0.1:{server.getsockname()[1]}", "analyzeduration=0:probesize=32")])
    conn, _addr = server.accept()
    return server, conn


def test_h264_from_a_socket_shows_each_picture_as_it_comes(devices):
    player = sprigcore.MediaPlayer(playing=True)
    server = conn = None
    try:
        server, conn = _phone(player)
        pictures = _h264(320, 176, 6)
        conn.sendall(pictures[0] + AUD)  # one picture, then a still screen: it shows without waiting for the next
        pixels, order = _picture(player)
        assert order.startswith("I420/") and pixels.shape == (176 + 88, 320)
        assert (player.info().width, player.info().height) == (320, 176)
        for data in pictures[1:]:
            conn.sendall(data + AUD)
        assert _wait(lambda: player.info().decoded >= 6)
    finally:
        player.close()
        for s in (conn, server):
            if s is not None:
                s.close()


def test_a_pictures_black_bars_are_seen_and_left_off(devices):
    player = sprigcore.MediaPlayer(playing=True)
    server = conn = None
    try:
        player.set_look(24)
        server, conn = _phone(player)
        pictures = _h264(320, 176, 40, bars=48)
        conn.sendall(pictures[0] + AUD)
        assert _wait(lambda: player.look().serial >= 1)
        look = player.look()
        assert (look.width, look.height) == (320, 176) and (look.top, look.bottom) == (0, 0)
        assert abs(look.left - 48) <= 2 and abs(look.right - 48) <= 2 and look.brightest > 150
        player.set_crop(48, 0, 48, 0)
        sent = 1

        def send_until(check):
            nonlocal sent
            end = time.monotonic() + 5
            while time.monotonic() < end and sent < len(pictures):
                conn.sendall(pictures[sent] + AUD)
                sent += 1
                time.sleep(0.03)
                if check():
                    return True
            return check()

        assert send_until(lambda: player.info().width == 224)  # 320 - 96
        info = player.info()
        assert (info.height, info.given_width, info.given_height) == (176, 320, 176)
        got = []
        assert send_until(lambda: got.append(player.video_frame()) or (
            got[-1] not in (None, "blank") and got[-1][0].shape == (176 + 88, 224)))
        assert got[-1][0][:176].min() > 150  # no black left
        player.set_crop()  # whole again
        assert send_until(lambda: player.info().width == 320)
    finally:
        player.close()
        for s in (conn, server):
            if s is not None:
                s.close()


def test_held_pictures_are_decoded_but_the_one_on_screen_stays(devices):
    player = sprigcore.MediaPlayer(playing=True)
    server = conn = None
    try:
        server, conn = _phone(player)
        pictures = _h264(320, 176, 12)
        conn.sendall(pictures[0] + AUD + AUD)  # (a second delimiter, as a phone that sends its own, is harmless)
        assert _picture(player) is not None
        player.set_hold(True)
        for data in pictures[1:6]:
            conn.sendall(data + AUD)
        assert _wait(lambda: player.info().decoded >= 6)
        time.sleep(0.1)
        assert player.video_frame() is None  # nothing new showed
        player.set_hold(False)
        for data in pictures[6:]:
            conn.sendall(data + AUD + AUD)
        assert _picture(player) is not None
        assert player.info().status == b"ok"
    finally:
        player.close()
        for s in (conn, server):
            if s is not None:
                s.close()
