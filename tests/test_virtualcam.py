import ctypes
import types

from sprigcast.output import virtualcam
from sprigcast.output.virtualcam import HEADER, MAGIC, SharedBlock, VirtualCamera


def _block(size=HEADER + 64 * 64 * 3 // 2):
    buf = ctypes.create_string_buffer(size)
    block = SharedBlock(ctypes.addressof(buf), size)
    block._buf = buf  # keep it alive
    ctypes.c_uint32.from_address(block.address).value = MAGIC
    return block


def _camera_side(block, w, h, beat):
    ctypes.c_uint32.from_address(block.address + 8).value = w
    ctypes.c_uint32.from_address(block.address + 12).value = h
    ctypes.c_uint64.from_address(block.address + 32).value = beat


def test_write_uses_the_seqlock_and_layout():
    block = _block()
    assert block.write(4, 2, bytes(range(12)))
    seq = ctypes.c_uint64.from_address(block.address + 24).value
    assert seq == 2  # odd while writing, even after
    assert ctypes.c_uint32.from_address(block.address + 16).value == 4
    assert ctypes.string_at(block.address + HEADER, 12) == bytes(range(12))
    assert not block.write(4, 2, b"short")
    assert not block.write(100, 100, bytes(15000))  # bigger than the block


class _Engine:
    def __init__(self):
        self.taps = []

    def add_frame_tap(self, cid, cb, size=None):
        self.taps.append((cid, cb, size))

    def remove_frame_tap(self, cid, cb):
        self.taps = [t for t in self.taps if not (t[0] == cid and t[1] == cb)]


def test_feeds_the_canvas_that_matches_the_shape_only_while_watched():
    block = _block()
    clock = [10_000]
    canvases = [types.SimpleNamespace(id="horizontal", width=1920, height=1080, enabled=True),
                types.SimpleNamespace(id="vertical", width=1080, height=1920, enabled=True)]
    engine = _Engine()
    cam = VirtualCamera(engine, lambda: canvases, open_block=lambda: block, clock=lambda: clock[0])
    cam.poll()
    assert engine.taps == []  # nobody watching yet
    _camera_side(block, 32, 18, clock[0])
    cam.poll()
    assert [(c, s) for c, _, s in engine.taps] == [("horizontal", (32, 18))]
    frame = types.SimpleNamespace(fmt="nv12", width=32, height=18, data=bytes(range(256)) * 4)
    engine.taps[0][1](frame)
    assert cam.frames == 1 and ctypes.c_uint32.from_address(block.address + 20).value == 18
    _camera_side(block, 18, 32, clock[0])  # the app switched to portrait
    cam.poll()
    assert [(c, s) for c, _, s in engine.taps] == [("vertical", (18, 32))]
    clock[0] += 5000  # heartbeat went stale: the app closed the camera
    cam.poll()
    assert engine.taps == [] and not cam.watched


def test_off_canvas_is_skipped_and_closed_block_is_reopened_later():
    block = _block()
    canvases = [types.SimpleNamespace(id="horizontal", width=1920, height=1080, enabled=False),
                types.SimpleNamespace(id="vertical", width=1080, height=1920, enabled=True)]
    engine = _Engine()
    opened = []
    cam = VirtualCamera(engine, lambda: canvases, open_block=lambda: opened.append(1) or block, clock=lambda: 50)
    _camera_side(block, 32, 18, 50)
    cam.poll()
    assert engine.taps[0][0] == "vertical"  # the only canvas that's on
    _camera_side(block, 0, 0, 50)  # the camera stopped
    cam.poll()
    assert engine.taps == [] and cam.block is None
    cam.poll()
    assert len(opened) == 2


def test_not_supported_off_windows(monkeypatch):
    monkeypatch.setattr(virtualcam.sys, "platform", "linux")
    ok, why = virtualcam.supported()
    assert not ok and "Windows 11" in why
    assert virtualcam.open_block() is None and not virtualcam.installed()


def test_installed_camera_counts_without_a_bundled_one(monkeypatch, tmp_path):
    monkeypatch.setattr(virtualcam.sys, "platform", "win32")
    monkeypatch.setattr(virtualcam, "windows_build", lambda: 22631)
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda: None)
    monkeypatch.setattr(virtualcam, "registered_path", lambda: tmp_path / "sprigcast_camera.dll")
    assert virtualcam.supported() == (True, "")
    monkeypatch.setattr(virtualcam, "registered_path", lambda: None)
    assert not virtualcam.supported()[0]
