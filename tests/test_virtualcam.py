import ctypes
import time
import types

from sprigcast.output import virtualcam
from sprigcast.output.virtualcam import HEADER, MAGIC, SharedBlock, VirtualCamera


def _block(size=HEADER + 64 * 64 * 3 // 2):
    buf = ctypes.create_string_buffer(size)
    block = SharedBlock(ctypes.addressof(buf), size)
    block._buf = buf  # keep it alive
    ctypes.c_uint32.from_address(block.address).value = MAGIC
    return block


def _again(block):
    """The same shared memory opened again (each open is a new view; a closed one stays closed)."""
    view = SharedBlock(block.address, block.size)
    view._buf = block._buf
    return view


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


def test_write_takes_a_numpy_frame_without_copying_to_bytes():
    import numpy as np

    block = _block()
    assert block.write(4, 2, np.arange(12, dtype=np.uint8))
    assert ctypes.string_at(block.address + HEADER, 12) == bytes(range(12))


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
    cam = VirtualCamera(engine, lambda: canvases, open_block=lambda: _again(block), clock=lambda: clock[0])
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
    assert engine.taps[0][2] == (32, 18)  # could be a second copy at another size: keep the first a while
    clock[0] += virtualcam.STILL_WANTED_MS
    _camera_side(block, 18, 32, clock[0])
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
    assert "organization" in virtualcam.privacy_block(read)


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
    cam = VirtualCamera(engine, lambda: canvases, open_block=lambda: _again(block), clock=lambda: clock[0])
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
    for _ in range(8):  # the two copies fall into step: landscape writes last for 8 s (soak 2026-11-15)
        clock[0] += 1000
        _camera_side(block, 32, 18, clock[0])
        tap[1](frame)
        cam.poll()
    assert engine.taps == [tap]
    for _ in range(16):  # the portrait copy closed: only landscape is asked for now
        clock[0] += 1000
        _camera_side(block, 32, 18, clock[0])
        cam.poll()
    assert [(c, s) for c, _, s in engine.taps] == [("horizontal", (32, 18))]


def test_install_step_moves_a_loaded_dll_aside_under_a_fresh_name():
    # A DLL an app still has loaded can only be renamed. A fixed ".old" name may itself still be loaded,
    # which made the rename fail and left the old 64-bit camera in place (unregistered).
    dll = virtualcam.Path(r"C:\Program Files\SprigCast Camera\sprigcast_camera.dll")
    a = virtualcam._install_step(virtualcam.Path("new.dll"), dll, "aaaa")
    assert f'move /y "{dll}" "{dll}.aaaa.old"' in a
    assert f'move /y "{dll}.new" "{dll}"' in a
    assert "regsvr32 /u" not in a  # never left unregistered
    # Registering runs whatever happened before it (joined with "&", not "&&").
    assert a.endswith(f'& regsvr32 /s "{dll}"')
    b, c = (virtualcam._install_step(virtualcam.Path("new.dll"), dll) for _ in range(2))
    assert b != c  # a new aside name each time


def test_set_up_registers_both_even_if_the_first_fails(monkeypatch, tmp_path):
    (tmp_path / "sprigcast_camera.dll").write_bytes(b"64")
    (tmp_path / "sprigcast_camera32.dll").write_bytes(b"32")
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda name=virtualcam.DLL_NAME: tmp_path / name)
    commands = []
    monkeypatch.setattr(virtualcam, "_elevated", lambda c: commands.append(c) or True)
    virtualcam.set_up()
    steps = commands[0].split(" & ")
    assert steps[-1].startswith("regsvr32 /s") and "camera32" in steps[-1]
    assert sum(s.startswith("regsvr32 /s") for s in steps) == 2


def test_turning_the_camera_off_never_unmaps_a_frame_mid_copy():
    """The render thread copies frames in while the UI thread turns the camera off: close() waits for the
    copy, and nothing is written, read or unmapped afterwards."""
    import threading

    block = _block()
    closed_while_copying = []
    copying = threading.Event()
    real_copy = block._copy

    def slow_copy(*args):
        copying.set()
        time.sleep(0.05)
        closed_while_copying.append(block.closed)
        real_copy(*args)

    block._copy = slow_copy
    calls = []
    block._close = lambda: calls.append("unmapped")
    frame = bytes(64 * 64 * 3 // 2)
    writer = threading.Thread(target=block.write, args=(64, 64, frame))
    writer.start()
    copying.wait(1)
    block.close()
    writer.join()
    assert closed_while_copying == [False]
    assert calls == ["unmapped"]
    assert block.write(64, 64, frame) is False
    assert block.wanted() == (0, 0) and block.heartbeat() == 0 and not block.valid


def test_needs_update_reads_the_files_once(monkeypatch, tmp_path):
    """PC test 2026-11-17: the camera window read both DLLs each time it opened (0.4 s while live)."""
    src, dst = tmp_path / "a" / virtualcam.DLL_NAME, tmp_path / "b" / virtualcam.DLL_NAME
    for path in (src, dst):
        path.parent.mkdir()
        path.write_bytes(b"camera" * 1000)
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda name=virtualcam.DLL_NAME: src if name == virtualcam.DLL_NAME else None)
    monkeypatch.setattr(virtualcam, "registered_path", lambda clsid=virtualcam.CLSID: dst)
    monkeypatch.setattr(virtualcam, "_compared", {})
    monkeypatch.setattr(virtualcam, "_versions", {})
    reads = []
    real = virtualcam.Path.read_bytes
    monkeypatch.setattr(virtualcam.Path, "read_bytes", lambda self: (reads.append(self), real(self))[1])
    assert not virtualcam.needs_update() and not virtualcam.needs_update()
    assert len(reads) == 3  # once each (the bundled one twice: its version, then the bytes; a build with no version)
    dst.write_bytes(b"older" * 10)  # a different size: no read needed to know
    assert virtualcam.needs_update() and len(reads) == 3


def test_the_native_engines_frames_reach_the_camera():
    """PC (2026-11-17): on the native engine the camera stayed on its "no picture" green: the engine's frames are
    bytearrays, which the copy refused."""
    block = _block()
    assert block.write(4, 2, bytearray(range(12)))
    assert ctypes.string_at(block.address + HEADER, 12) == bytes(range(12))
    assert block.write(4, 2, memoryview(bytearray(range(1, 13))))
    assert ctypes.string_at(block.address + HEADER, 12) == bytes(range(1, 13))
    assert block.write(4, 2, memoryview(bytes(range(2, 14))))  # read-only
    assert ctypes.string_at(block.address + HEADER, 12) == bytes(range(2, 14))


def _fixed_info_dll(path, version_ms: int | None):
    """A stand-in DLL: some bytes, then a VS_FIXEDFILEINFO with this FILEVERSION (high word = camera version)."""
    body = b"MZ" + b"\x00" * 200
    if version_ms is not None:
        body += virtualcam._FIXED_INFO + (0x00010000).to_bytes(4, "little") + version_ms.to_bytes(4, "little")
        body += b"\x00" * 40
    path.write_bytes(body + b"\x00" * 100)
    return path


def test_the_camera_is_compared_by_its_own_version_not_its_bytes(monkeypatch, tmp_path):
    """PC, test builds 6 and 7 (2026-11-27): every build's DLL had new bytes, so every update asked for Windows'
    permission to update a camera that hadn't changed."""
    monkeypatch.setattr(virtualcam, "_versions", {})
    monkeypatch.setattr(virtualcam, "_compared", {})
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    src = _fixed_info_dll(tmp_path / "a" / virtualcam.DLL_NAME, 2 << 16)
    dst = tmp_path / "b" / virtualcam.DLL_NAME
    monkeypatch.setattr(virtualcam, "bundled_dll", lambda name=virtualcam.DLL_NAME: src if name == virtualcam.DLL_NAME else None)
    monkeypatch.setattr(virtualcam, "registered_path", lambda clsid=virtualcam.CLSID: dst)
    assert virtualcam.camera_version(src) == 2
    _fixed_info_dll(dst, None)
    assert virtualcam.camera_version(dst) is None and virtualcam.needs_update()  # before versions: older
    _fixed_info_dll(dst, (2 << 16) | 7)
    dst.write_bytes(dst.read_bytes() + b"another build's bytes")
    assert not virtualcam.needs_update()  # same camera version, different bytes: nothing to ask
    _fixed_info_dll(dst, 1 << 16)
    assert virtualcam.needs_update()
    _fixed_info_dll(dst, 3 << 16)
    assert not virtualcam.needs_update()  # newer than this build's (a downgrade): leave it


def test_camera_version_is_bumped_when_the_camera_changes():
    """native/camera/version.h must go up whenever the camera's code changes, or installed cameras keep the old
    code (SprigCast and Setup only update a camera with a lower number). Changed the camera? Bump
    SPRIGCAST_CAMERA_VERSION and add the new fingerprint below with it."""
    import hashlib
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "native" / "camera"
    version = int(re.search(r"#define SPRIGCAST_CAMERA_VERSION (\d+)", (root / "version.h").read_text()).group(1))
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and path.name != "version.h" and rel.parts[0] != "tools" and "__pycache__" not in rel.parts:
            digest.update(rel.as_posix().encode())
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    known = {  # camera code fingerprint: the version it shipped as
        "9ae31ae43d18ec9a": 2,
    }
    assert known.get(digest.hexdigest()[:16]) == version, (
        f"native/camera changed (fingerprint {digest.hexdigest()[:16]}): bump SPRIGCAST_CAMERA_VERSION in "
        f"version.h (now {version}) and add the fingerprint here")
    assert (root / "sprigcast_camera.rc").read_text().count("SPRIGCAST_CAMERA_VERSION,0,0,0") == 2
