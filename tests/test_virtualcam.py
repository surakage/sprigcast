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
    clock[0] += 2500
    _camera_side(block, 18, 32, clock[0])  # the app switched to portrait (landscape no longer asked for)
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
    assert not ok and "Windows" in why
    assert virtualcam.open_block() is None and not virtualcam.installed()


def test_installed_camera_counts_without_a_bundled_one(monkeypatch, tmp_path):
    monkeypatch.setattr(virtualcam.sys, "platform", "win32")
    monkeypatch.setattr(virtualcam, "windows_build", lambda: 22631)
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda: None)
    monkeypatch.setattr(virtualcam, "registered_path", lambda: tmp_path / "sprigcast_camera.dll")
    assert virtualcam.supported() == (True, "")
    monkeypatch.setattr(virtualcam, "registered_path", lambda: None)
    assert not virtualcam.supported()[0]


def test_privacy_block_names_the_switch_that_is_off():
    values = {}
    read = lambda hive, key, name: values.get((hive, key.rsplit("\\", 1)[-1], name))  # noqa: E731
    assert virtualcam.privacy_block(read) == ""
    values[("HKCU", "NonPackaged", "Value")] = "Deny"
    assert "Desktop apps" in virtualcam.privacy_block(read)
    values[("HKLM", "webcam", "Value")] = "Deny"
    assert "this PC" in virtualcam.privacy_block(read)
    values[("HKLM", "AppPrivacy", "LetAppsAccessCamera")] = 2
    assert "organisation" in virtualcam.privacy_block(read)


def test_refused_start_points_to_privacy(monkeypatch, tmp_path):
    dll = types.SimpleNamespace(SC_Start=lambda name: -1072873851,  # 0xC00D3E85
                                SC_Stage=lambda: 3, SC_SelfTest=lambda: 0)
    monkeypatch.setattr(virtualcam, "registered_path", lambda clsid=virtualcam.CLSID: tmp_path / "x.dll")
    monkeypatch.setattr(virtualcam, "has_directshow", lambda: False)  # an older camera: media source only
    monkeypatch.setattr(virtualcam.ctypes, "WinDLL", lambda path: dll, raising=False)
    monkeypatch.setattr(virtualcam, "privacy_block", lambda: "")
    monkeypatch.setattr(virtualcam, "needs_update", lambda: False)
    cam = VirtualCamera(object(), lambda: [])
    assert not cam.start() and cam.privacy and cam.error == virtualcam.PRIVACY_HINT
    assert virtualcam.state.privacy_failed
    monkeypatch.setattr(virtualcam, "needs_update", lambda: True)
    assert not cam.start() and not cam.privacy and "Update the camera" in cam.error



def test_directshow_camera_needs_no_windows_11_start(monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(virtualcam, "registered_path", lambda clsid=virtualcam.CLSID: tmp_path / "x.dll")
    monkeypatch.setattr(virtualcam, "has_directshow", lambda: True)
    monkeypatch.setattr(virtualcam.ctypes, "WinDLL", lambda path: started.append(path), raising=False)
    cam = VirtualCamera(object(), lambda: [])
    assert cam.start() and cam.error == "" and started == []
    cam.stop()


def test_set_up_installs_and_registers_both_sizes(monkeypatch, tmp_path):
    (tmp_path / "sprigcast_camera.dll").write_bytes(b"64")
    (tmp_path / "sprigcast_camera32.dll").write_bytes(b"32")
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda name=virtualcam.DLL_NAME: tmp_path / name)
    commands = []
    monkeypatch.setattr(virtualcam, "_elevated", lambda c: commands.append(c) or True)
    assert virtualcam.set_up()
    command = commands[0]
    assert command.count("regsvr32 /s") == 2 and "sprigcast_camera32.dll" in command


def test_an_app_with_the_camera_open_twice_doesnt_flip_it_every_second():
    """TikTok LIVE Studio opened it at 1080x1920 and 1920x1080: each copy writes its size into the block in
    turn, and SprigCast re-made its copy for the other size every second (350 times in 10 minutes)."""
    block = _block()
    clock = [10_000]
    canvases = [types.SimpleNamespace(id="horizontal", width=1920, height=1080, enabled=True),
                types.SimpleNamespace(id="vertical", width=1080, height=1920, enabled=True)]
    engine = _Engine()
    cam = VirtualCamera(engine, lambda: canvases, open_block=lambda: block, clock=lambda: clock[0])
    _camera_side(block, 18, 32, clock[0])
    cam.poll()
    tap = engine.taps[0]
    assert (tap[0], tap[2]) == ("vertical", (18, 32))
    frame = types.SimpleNamespace(fmt="nv12", width=18, height=32, data=bytes(18 * 32 * 3 // 2))
    for second in range(10):
        clock[0] += 500
        _camera_side(block, 18, 32, clock[0])
        tap[1](frame)  # the portrait copy asks between frames...
        clock[0] += 500
        _camera_side(block, 32, 18, clock[0])  # ...and the landscape copy wrote last when polled
        cam.poll()
        assert engine.taps == [tap], second
    for _ in range(3):  # the portrait copy closed: only landscape is asked for now
        clock[0] += 1000
        _camera_side(block, 32, 18, clock[0])
        cam.poll()
    assert [(c, s) for c, _, s in engine.taps] == [("horizontal", (32, 18))]
