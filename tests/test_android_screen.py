"""Android Screen: a phone's screen over USB through Android's own screen recorder (sources/android_screen.py).
A stand-in adb plays the part of the phone here."""

import stat
import sys
import textwrap
import time

import numpy as np
import pytest
from test_native_media import (
    player_lib,  # noqa: F401 (builds or finds the engine's media library)
)

from sprigcast.sources import android_screen as screen


def test_devices_sizes_and_turning_are_read():
    text = ("List of devices attached\n"
            "R58M123ABC  device usb:1-1 product:beyond1 model:SM_G973F device:beyond1 transport_id:2\n"
            "0123456789  unauthorized usb:1-2 transport_id:3\n")
    assert screen.parse_devices(text) == [
        {"serial": "R58M123ABC", "state": "device", "model": "SM G973F"},
        {"serial": "0123456789", "state": "unauthorized", "model": ""}]
    assert screen.parse_size("Physical size: 1080x2400\n") == (1080, 2400)
    assert screen.parse_size("Physical size: 1440x3200\nOverride size: 1080x2400\n") == (1080, 2400)
    assert screen.parse_size("error") is None
    assert screen.parse_turned("    SurfaceOrientation: 1\n") and screen.parse_turned("SurfaceOrientation: 0") is False
    # newer Androids (PC 2026-11-26, a Galaxy S26 Ultra was always taken as portrait)
    assert screen.parse_turned("  Viewport INTERNAL: displayId=0, uniqueId=local:1, port=0, orientation=ROTATION_90, "
                               "logicalFrame=[0, 0, 3120, 1440]\n")
    assert screen.parse_turned("Viewport INTERNAL: displayId=0, orientation=3, logicalFrame=[0, 0, 3120, 1440]")
    assert screen.parse_turned("Viewport INTERNAL: displayId=0, orientation=0, logicalFrame=[0, 0, 1440, 3120]") is False
    assert screen.parse_turned("    mCurrentRotation=ROTATION_270\n")
    assert screen.parse_turned("Touch orientation=1") is None and screen.parse_turned("") is None


def test_the_recorded_size_follows_the_screen_and_fits():
    assert screen.record_size((1080, 2400), False, 1280) == (576, 1280)
    assert screen.record_size((1080, 2400), True, 1280) == (1280, 576)
    assert screen.record_size((1080, 2400), True, 1920) == (1920, 864)
    w, h = screen.record_size((720, 1600), True, 1920)
    assert (w, h) == (1600, 720) and w % 16 == 0 and h % 16 == 0


@pytest.fixture
def fake_adb(tmp_path):
    av = pytest.importorskip("av")
    clip = tmp_path / "screen.h264"
    with av.open(str(clip), "w", format="h264") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 320, 176, "yuv420p"
        import numpy as np

        for i in range(60):
            picture = np.full((176, 320, 3), (i * 4) % 255, dtype=np.uint8)
            for packet in stream.encode(av.VideoFrame.from_ndarray(picture, format="rgb24")):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    still = tmp_path / "still.h264"  # a still screen: one picture, then nothing
    with av.open(str(still), "w", format="h264") as out:
        stream = out.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 320, 176, "yuv420p"
        stream.options = {"tune": "zerolatency"}  # as a phone's encoder: no pictures held back for reordering
        import numpy as np

        for packet in stream.encode(av.VideoFrame.from_ndarray(np.full((176, 320, 3), 90, np.uint8), format="rgb24")):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    log = tmp_path / "calls.txt"
    script = tmp_path / "adb"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys, time
        args = sys.argv[1:]
        open({str(log)!r}, "a").write(" ".join(args) + "\\n")
        if args[:1] == ["devices"]:
            print("List of devices attached")
            print("FAKE01 device usb:1 model:Test_Phone")
        elif "wm" in args:
            print("Physical size: 1080x2400")
        elif args[-1:] and args[-1].startswith("while true"):  # the turn watcher: answers until it's ended
            turn = {str(tmp_path / "turn.txt")!r}
            while True:
                print(open(turn).read() if os.path.exists(turn) else "SurfaceOrientation: 1")
                print("SPRIGCAST-LOOK", flush=True)
                time.sleep(0.05)
        elif "dumpsys" in args:
            turn = {str(tmp_path / "turn.txt")!r}
            print(open(turn).read() if os.path.exists(turn) else "SurfaceOrientation: 1")
        elif "push" in args:
            print("1 file pushed")
        elif "app_process" in args and os.environ.get("FAKE_OLD"):
            sys.stdout.write("sprigcast-sound: needs Android 11 or newer (this phone has API 29)\\n")
            sys.exit(2)  # (exec-out sends the error output down the same pipe)
        elif "app_process" in args:
            import struct
            block = struct.pack("<2h", 16384, -16384) * 960  # 20 ms: left half up, right half down
            sys.stdout.buffer.write(b"sprigcast-sound: no bound app (x), trying anyway\\nSPRIGCAST-SOUND-START\\n")
            data = block * 500
            for at in range(0, len(data), 3001):  # pieces that aren't whole frames
                sys.stdout.buffer.write(data[at:at + 3001])
                sys.stdout.flush()
                time.sleep(0.01)
        elif "screenrecord" in args and os.environ.get("FAKE_STILL"):
            sys.stdout.buffer.write(open({str(still)!r}, "rb").read())
            sys.stdout.flush()
            time.sleep(30)  # the screen doesn't change: nothing more
        elif "screenrecord" in args:
            data = open({str(clip)!r}, "rb").read()
            for _ in range(150):  # a phone keeps sending while its screen changes
                sys.stdout.buffer.write(data)
                sys.stdout.flush()
                time.sleep(0.2)
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script), log


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_the_phones_screen_shows(fake_adb):
    adb, log = fake_adb
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        frame = None
        for _ in range(100):
            frame = source.video_frame()
            if frame is not None:
                break
            time.sleep(0.05)
        assert frame is not None, source.status
        assert source.size == (320, 176) and source.status.startswith("capturing")
        calls = log.read_text()
        assert "screenrecord --output-format=h264 --size 1280x576" in calls  # landscape, longest 1280
    finally:
        source.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_the_next_recording_takes_over_without_a_gap(fake_adb, monkeypatch):
    adb, log = fake_adb
    monkeypatch.setattr(screen, "SEGMENT_S", 0.6)
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        deadline = time.monotonic() + 8
        while source._current < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._current >= 2
        time.sleep(0.3)
        with source._lock:
            assert all(n >= source._current for n in source._procs)  # the older recorder was ended
    finally:
        source.stop()


def test_no_phone_says_what_to_do(tmp_path):
    script = tmp_path / "adb"
    script.write_text(f"#!{sys.executable}\nprint('List of devices attached')\nprint('XYZ unauthorized usb:1')\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    source = screen.AndroidScreenSource("android-1", {})
    source.adb_path = str(script)
    if sys.platform == "win32":
        pytest.skip("the stand-in adb is a script")
    assert source._phone(str(script)) is None and source.status == "allow USB debugging on your phone"


def test_the_pop_up_lists_ready_phones_and_keeps_choices():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.android_screen_dialog import AndroidScreenDialog

    phones = [{"serial": "A1", "state": "device", "model": "Pixel 8"},
              {"serial": "B2", "state": "unauthorized", "model": ""}]
    dialog = AndroidScreenDialog({"serial": "A1", "longest": 1920, "bitrate_kbps": 12000}, lister=lambda: phones)
    for _ in range(100):
        app.processEvents()
        if dialog.phone.count():
            break
        time.sleep(0.02)
    assert [dialog.phone.itemText(i) for i in range(dialog.phone.count())] == ["Pixel 8"]
    assert dialog.values() == {"serial": "A1", "longest": 1920, "bitrate_kbps": 12000, "sound": True,
                                     "trim": True, "fill": True}
    dialog.deleteLater()


def test_phone_leaves_are_listed_as_their_own_kind(tmp_path, monkeypatch):
    from test_add_menu import _window

    from sprigcast.core.scene import SceneItem, SourceConfig

    col, w = _window(tmp_path, monkeypatch)
    try:
        col.sources += [SourceConfig(id="browser-1", kind="browser", name="Alerts", settings={"url": "https://a"}),
                        SourceConfig(id="browser-2", kind="browser", name="Phone Camera",
                                     settings={"phone": "x", "url": "http://127.0.0.1:1/view/x"})]
        col.scene("b").items += [SceneItem(source="browser-1"), SceneItem(source="browser-2")]
        w.selected_scene_id = "a"
        assert [c.name for c in w._existing_of_kind("browser")] == ["Alerts"]
        assert [c.name for c in w._existing_of_kind("browser:phone")] == ["Phone Camera"]
    finally:
        w.close()


def test_adb_is_unpacked_from_googles_zip_with_its_notice(tmp_path):
    import zipfile

    zip_path = tmp_path / "platform-tools.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        for name in ("adb.exe", "adb", "AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "fastboot.exe",
                     "source.properties"):
            z.writestr(f"platform-tools/{name}", name)
        z.writestr("platform-tools/lib64/libc++.so", "x")
        z.writestr("../evil", "x")
    adb = screen.install_adb(zip_path, tmp_path / "home")
    kept = sorted(p.name for p in (tmp_path / "home" / "platform-tools").iterdir())
    assert kept == ["AdbWinApi.dll", "AdbWinUsbApi.dll", "NOTICE.txt", "adb", "adb.exe", "source.properties"]
    assert adb.name in ("adb", "adb.exe") and not (tmp_path / "evil").exists()


def test_a_downloaded_adb_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(screen.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "none"))
    monkeypatch.setattr(screen.Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setattr(screen, "ADB_HOME", tmp_path / "adb")
    assert screen.find_adb() is None
    name = "adb.exe" if sys.platform == "win32" else "adb"
    (tmp_path / "adb" / "platform-tools").mkdir(parents=True)
    (tmp_path / "adb" / "platform-tools" / name).write_text("x")
    assert screen.find_adb() is None  # "not on this PC" is remembered a few seconds...
    assert screen.find_adb(fresh=True) == str(tmp_path / "adb" / "platform-tools" / name)  # ...unless asked afresh


def test_the_download_card_fetches_and_closes():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.android_screen_dialog import AdbDownloadCard

    calls = []

    def fake(progress, cancelled):
        progress(3_500_000, 7_000_000)
        calls.append(True)

    card = AdbDownloadCard(download=fake)
    card.start()
    for _ in range(100):
        app.processEvents()
        if card.result():
            break
        time.sleep(0.02)
    assert calls and card.result() == 1

    def broken(progress, cancelled):
        raise OSError("no network")

    card2 = AdbDownloadCard(download=broken)
    card2.start()
    for _ in range(100):
        app.processEvents()
        if card2.go.isEnabled():
            break
        time.sleep(0.02)
    assert card2.go.text() == "Try again" and "try again" in card2.progress.text()


def test_the_pop_up_stays_whole_when_its_answer_comes_in():
    """The answer (two lines on "needs adb") came after the pop-up was up: it grew down past the screen's free part
    and Cancel and Save were cut off (PC 2026-11-26). Two lines are kept from the start, and a pop-up that grows
    anyway is kept on screen."""
    import threading

    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.android_screen_dialog import AndroidScreenDialog
    from sprigcast.app.card import CardDialog

    gate = threading.Event()
    dialog = AndroidScreenDialog({}, lister=lambda: (gate.wait(5), ["no adb"])[1])
    dialog._asked = True
    dialog.show()
    room = dialog.screen().availableGeometry()
    dialog.move(room.left() + 10, room.bottom() - QWidget.height(dialog) + 1)  # resting on the bottom edge
    for _ in range(5):
        app.processEvents()
    before = QWidget.height(dialog)
    gate.set()
    for _ in range(100):
        app.processEvents()
        if "adb" in dialog.status.text():
            break
        time.sleep(0.02)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)
    assert "adb" in dialog.status.text()
    assert QWidget.height(dialog) == before
    assert dialog.frameGeometry().bottom() <= room.bottom()

    grown = CardDialog("Grows")
    grown.show()
    grown.move(room.left() + 10, room.bottom() - QWidget.height(grown) + 1)
    app.processEvents()
    grown.body.addWidget(QWidget(minimumHeight=80))
    for _ in range(10):
        app.processEvents()
        time.sleep(0.01)
    assert grown.frameGeometry().bottom() <= room.bottom()
    dialog.deleteLater()
    grown.deleteLater()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_a_still_screen_shows_its_picture(fake_adb, monkeypatch):
    """screenrecord sends a picture only when the screen changes, and FFmpeg's parser held the one it had until the
    next began: a still phone screen left the leaf on "starting" for minutes (PC 2026-11-26)."""
    adb, _ = fake_adb
    monkeypatch.setenv("FAKE_STILL", "1")
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        frame = None
        deadline = time.monotonic() + 6
        while frame is None and time.monotonic() < deadline:
            frame = source.video_frame()
            time.sleep(0.05)
        assert frame is not None, source.status
        assert source.status.startswith("capturing")
    finally:
        source.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_turning_the_phone_starts_a_recording_in_the_new_shape(fake_adb, monkeypatch):
    """screenrecord keeps the shape it started in: a turn starts the next one at once (PC 2026-11-26)."""
    adb, log = fake_adb
    monkeypatch.setattr(screen, "TURN_CHECK_S", 0.2)
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        deadline = time.monotonic() + 8
        while source._current < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._current == 1
        (log.parent / "turn.txt").write_text("Viewport INTERNAL: displayId=0, orientation=ROTATION_0, x\n")
        deadline = time.monotonic() + 8
        while source._current < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._current == 2
        calls = log.read_text()
        assert "--size 1280x576" in calls and "--size 576x1280" in calls  # landscape, then portrait
        assert "while true; do dumpsys input" in calls  # watched by one adb shell on the phone
        assert source._watched is not None and source._watched[0] is False
    finally:
        source.stop()


def test_a_place_fits_its_canvas_again_when_the_phone_turns():
    """User 2026-11-26: fit automatically on every turn, centered, and smoothly: the place becomes a box the size
    of the canvas that the engine fits each picture into, so the first picture after a turn is already right."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="main", width=1920, height=1080)],
                          sources=[SourceConfig(id="android-1", kind="android_screen", name="Phone")],
                          scenes=[Scene(id="a", name="A", canvas="main", items=[
                              SceneItem(source="android-1", x=100, y=50, scale=0.5, crop=(4, 0, 0, 0))])])
    item = col.scenes[0].items[0]
    assert col.fit_places("android-1", (1280, 576))
    assert item.bounded and item.bounds == "fit" and item.bounds_size == (1920.0, 1080.0)
    assert (item.x, item.y, item.scale, item.crop, item.bounds_align) == (0.0, 0.0, 1.0, (0, 0, 0, 0), "center")
    assert item.picture_scale((1280, 576))[0] == 1.5  # landscape: as wide as the canvas
    assert abs(item.picture_scale((576, 1280))[1] - 1080 / 1280) < 1e-6  # portrait: as tall
    assert not col.fit_places("android-1", (576, 1280))  # already fitting: nothing to save
    assert not col.fit_places("android-2", (576, 1280))
    # Fill the canvas (user 2026-11-26: a 19.5:9 phone left gaps top and bottom): landscape fills it, its
    # excess sides cropped, and portrait on the same landscape canvas still fits whole.
    assert col.fit_places("android-1", (1280, 576), fill=True) and item.bounds == "fill_same"
    assert item.picture_scale((1280, 576))[0] == 1080 / 576
    x, y, w, h = item.picture_rect((1280, 576))
    assert (y, h) == (0.0, 1080.0) and x < 0 and abs(x * 2 + w - 1920) < 1e-6  # centered, sides spill
    assert abs(item.picture_scale((576, 1280))[1] - 1080 / 1280) < 1e-6



def test_pcm_becomes_the_mixers_samples():
    import struct

    out = screen.pcm_to_float(struct.pack("<4h", 16384, -32768, 0, 32767) + b"x")
    assert out.shape == (2, 2) and out.dtype == np.float32
    assert out[0].tolist() == [0.5, -1.0] and out[1][0] == 0.0


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_the_phones_sound_reaches_the_mixer(fake_adb, tmp_path):
    """User 2026-11-26: the phone's sound didn't come through. The helper's sound is read like a mic."""
    adb, log = fake_adb
    jar = tmp_path / "sprigcast-sound.jar"
    jar.write_bytes(b"helper")
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path, source.sound_jar = adb, jar
    source.start()
    try:
        deadline = time.monotonic() + 8
        block = np.zeros((480, 2), np.float32)
        while not block.any() and time.monotonic() < deadline:
            time.sleep(0.05)
            block = source.read_block(480)
        assert block.any(), source.sound_note
        time.sleep(0.3)
        heard = source.read_block(4800)
        heard = heard[np.abs(heard).sum(axis=1) > 0]
        assert len(heard) and np.allclose(heard, [0.5, -0.5])  # every sample in its place, no text in the sound
        assert source.gain == 1.0
        calls = log.read_text()
        assert f"push {jar} {screen.SOUND_ON_PHONE}" in calls and screen.SOUND_CLASS in calls
        time.sleep(0.6)  # the mixer reads nothing for a while: it never falls more than a moment behind
        assert source._sound_frames <= (screen.SOUND_KEEP_S + screen.SHOW_AFTER_S) * screen.SOUND_RATE + 960
        recording = source._thread
        source.update({**source.settings, "volume_db": -6.0, "muted": True})
        assert source._thread is recording and source.gain == 0.0  # the picture didn't start again
    finally:
        source.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_an_older_phone_says_why_it_has_no_sound(fake_adb, tmp_path, monkeypatch):
    adb, log = fake_adb
    monkeypatch.setenv("FAKE_OLD", "1")
    jar = tmp_path / "sprigcast-sound.jar"
    jar.write_bytes(b"helper")
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path, source.sound_jar = adb, jar
    source.start()
    try:
        deadline = time.monotonic() + 8
        while not source._sound_given_up and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._sound_given_up and "Android 11" in source.sound_note
        assert log.read_text().count("app_process") == 1  # not tried again and again
    finally:
        source.stop()


def test_phone_sound_is_listed_and_can_be_switched_off():
    from sprigcast.app.audio_page import has_sound
    from sprigcast.core.scene import SourceConfig

    on = SourceConfig(id="android-1", kind="android_screen", name="Phone", settings=dict(screen.DEFAULTS))
    off = SourceConfig(id="android-2", kind="android_screen", name="Phone", settings={**screen.DEFAULTS, "sound": False})
    assert has_sound(on) and not has_sound(off)



def _landscape_frame(bar: int, w: int = 320, h: int = 176):
    import av

    picture = np.full((h, w, 3), 200, np.uint8)
    picture[:, :bar] = 0  # the strip beside the camera cutout
    return av.VideoFrame.from_ndarray(picture, format="rgb24").reformat(format="yuv420p")


def test_the_phones_black_edges_are_found_and_cut_off():
    """PC 2026-11-26: in landscape a black strip ran down the left of the phone's picture."""
    frame = _landscape_frame(14)
    plane = frame.planes[0]
    y = np.frombuffer(plane, np.uint8).reshape(frame.height, plane.line_size)[:, :frame.width]
    assert screen.black_edges(y) == (14, 0, 0, 0)
    dark = np.full((176, 320), 16, np.uint8)
    assert screen.black_edges(dark) == (0, 0, 0, 0)  # a dark scene isn't a bar
    wide = _landscape_frame(100)
    plane = wide.planes[0]
    assert screen.black_edges(np.frombuffer(plane, np.uint8).reshape(176, plane.line_size)[:, :320]) == (0, 0, 0, 0)
    cut = screen.crop_yuv(frame, 14, 0, 0, 0)
    assert (cut.width, cut.height) == (306, 176)
    rgb = cut.to_ndarray(format="rgb24")
    assert rgb[:, :2].mean() > 150 and abs(int(rgb[88, 150, 0]) - 200) < 8


def test_a_bar_is_trimmed_only_once_its_steady(monkeypatch):
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    clock = [100.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    frame = _landscape_frame(14)
    for i in range(screen.TRIM_SURE - 1):
        assert source._trimmed(frame) is frame
        clock[0] += screen.TRIM_EVERY_S
    assert source._trimmed(frame).width == 306
    clock[0] += 0.2
    assert source._trimmed(frame).width == 306  # between looks the trim stays



def test_turning_back_uses_that_shapes_trim_at_once(monkeypatch):
    """Smoother turns (user 2026-11-26): a shape trimmed before is trimmed from its first picture again."""
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    clock = [100.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    wide, tall = _landscape_frame(14), _landscape_frame(0, w=176, h=320)
    for _ in range(screen.TRIM_SURE):
        source._trimmed(wide)
        clock[0] += screen.TRIM_EVERY_S
    assert source._trimmed(wide).width == 306
    assert source._trimmed(tall).width == 176  # turned: nothing to trim there
    clock[0] += 0.1
    assert source._trimmed(wide).width == 306  # turned back: trimmed straight away


def _boxed_frame(w: int, h: int, inner: int):
    """The other shape shrunk into the middle of the long side, black around it."""
    import av

    picture = np.zeros((h, w, 3), np.uint8)
    if w >= h:
        picture[:, (w - inner) // 2:(w + inner) // 2] = 200
    else:
        picture[(h - inner) // 2:(h + inner) // 2, :] = 200
    return av.VideoFrame.from_ndarray(picture, format="rgb24").reformat(format="yuv420p")


def test_a_phone_turned_under_the_old_recording_is_noticed():
    """PC 2026-11-26: after a turn, the old recording showed the phone boxed in black in the old shape."""
    def luma(frame):
        plane = frame.planes[0]
        return np.frombuffer(plane, np.uint8).reshape(frame.height, plane.line_size)[:, :frame.width]

    assert screen.boxed(luma(_boxed_frame(320, 176, 80)))  # portrait inside landscape
    assert screen.boxed(luma(_boxed_frame(176, 320, 80)))  # landscape inside portrait
    assert not screen.boxed(luma(_landscape_frame(14)))  # the cutout strip isn't a turn
    assert not screen.boxed(np.full((176, 320), 16, np.uint8))  # nor is a black screen
    assert not screen.boxed(luma(_boxed_frame(320, 176, 300)))


def test_the_last_good_picture_stays_through_a_turn(monkeypatch):
    """Smoother turns (PC 2026-11-26): the old recording's boxed pictures never show; a dark picture that only
    looked turned shows again once the phone says it didn't turn."""
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    good, turned = _landscape_frame(0), _boxed_frame(320, 176, 80)
    assert not source._held(1, good)
    assert source._held(1, turned) and source._look_now.is_set()
    assert source._held(1, good)  # held until the next recording takes over
    assert not source._held(2, good)  # the next recording's pictures show at once
    monkeypatch.setattr(screen, "HOLD_MOST_S", 0.0)
    assert not source._held(1, turned)  # held too long (no next recording): it shows anyway
    source._hold, source._not_turned = None, 3
    assert not source._held(3, turned)  # the phone hadn't turned: a dark scene, shown


def test_a_turn_takes_back_the_pictures_still_waiting(monkeypatch):
    """26o (2026-11-26): each turn still showed 1-2 frames sent while the phone was turning (sideways in the old
    box, the phone's own turning animation). Pictures wait SHOW_AFTER_S, so a turn found later can take them back."""
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    now = [100.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: now[0])
    old, new = np.zeros((4, 8, 4), np.uint8), np.ones((8, 4, 4), np.uint8)
    source._waiting = [(99.9, 1, old, 1, (8, 4)), (99.95, 1, old, 2, (8, 4)), (100.0, 2, new, 3, (4, 8))]
    source._release()
    assert source._frames.take() is None  # none is SHOW_AFTER_S old yet
    source._hold_from(1)  # the turn: recording 1's waiting pictures never show
    assert [w[1] for w in source._waiting] == [2]
    now[0] += screen.SHOW_AFTER_S
    source._release()
    picture, order = source._frames.take()
    assert order == 3 and picture is new and source._size == (4, 8)


def test_a_damaged_picture_is_skipped_not_the_whole_recording():
    """26o (2026-11-26): "Invalid data found when processing input" ended the recording, about 160 times a minute.
    A damaged picture is now skipped; only a long run of them starts the recording again."""
    av = pytest.importorskip("av")
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))

    class Codec:
        def decode(self, packet):
            if packet == "bad":
                raise av.InvalidDataError(1094995529, "Invalid data found when processing input")
            return [packet]

    damaged = [0]
    assert list(source._decoded(Codec(), ["a", "bad", "b"], damaged)) == ["a", "b"]
    with pytest.raises(av.InvalidDataError):
        list(source._decoded(Codec(), ["bad"] * screen.DAMAGED_MOST, [0]))


def test_adbs_place_is_remembered_instead_of_walking_the_disk_each_time(tmp_path, monkeypatch):
    """A tester's 4 GB PC (2026-11-27): shutil.which walked PATH on the window's thread each time the pop-up looked
    for phones, 0.7-1.1 s a time, and the leaf walked it again every time it tried the phone."""
    looks = []
    monkeypatch.setattr(screen, "look_for_adb", lambda: looks.append(1) or "C:/tools/adb.exe")
    monkeypatch.setattr(screen, "_adb_known", None)
    clock = [50.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    assert [screen.find_adb() for _ in range(5)] == ["C:/tools/adb.exe"] * 5 and len(looks) == 1
    clock[0] += screen.ADB_KNOWN_S + 1
    assert screen.find_adb() and len(looks) == 2  # looked at again now and then (it may have been removed)
    monkeypatch.setattr(screen, "look_for_adb", lambda: looks.append(1) or None)
    assert screen.find_adb(fresh=True) is None and screen.find_adb() is None and len(looks) == 3
    clock[0] += screen.ADB_NONE_S + 1
    assert screen.find_adb() is None and len(looks) == 4
    monkeypatch.setattr(screen, "ADB_HOME", tmp_path)  # downloaded somewhere new: looked for at once
    assert screen.find_adb() is None and len(looks) == 5


def test_a_sliver_of_black_isnt_trimmed_and_a_trim_packs_in_one_copy():
    """A tester's stream (2026-11-27): 2 pixels trimmed, then none, then 2 on another side, each a new picture
    size, and cutting them off copied the whole picture four times, every picture."""
    from sprigcast.sources.media import pack_i420

    y = np.full((176, 320), 200, np.uint8)
    y[:, :4] = 0
    y[-2:, :] = 0
    assert screen.black_edges(y) == (0, 0, 0, 0)
    frame = _landscape_frame(14)
    whole, order = pack_i420(frame, (320, 176))
    cut, cut_order = pack_i420(frame, (320, 176), crop=(14, 0, 0, 0))
    assert cut_order == order and cut.shape == (176 * 3 // 2, 306)
    assert np.array_equal(cut[:176], whole[:176, 14:])  # the picture's own rows, the bar left out
    assert np.array_equal(cut[176:, :153], whole[176:, 7:160]) and np.array_equal(cut[176:, 153:], whole[176:, 167:])
    same = screen.crop_yuv(frame, 14, 0, 0, 0)
    assert np.array_equal(cut, pack_i420(same, (306, 176))[0])  # as cutting it to a new frame first did
    assert pack_i420(frame, (320, 176), crop=(160, 0, 150, 0)) is None  # nothing left: not packed
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source._trim = ((320, 176), (14, 0, 0, 0))
    source._trim_at = float("inf")
    picture, got_order, size = source._picture(frame, lambda f, t: pytest.fail("packed straight from the planes"))
    assert size == (306, 176) and got_order == order and np.array_equal(picture, cut)


def test_the_phones_sound_is_handed_on_whole_or_not_at_all():
    """With the engine's mixer the sound's lateness is queued in the engine: the leaf hands on each whole block as
    it came and nothing while the phone is late (a made-up silent block was a gap you heard)."""
    from sprigcast.audio import native_mixer as nm

    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    assert nm.ahead_of(source) == int(screen.SHOW_AFTER_S * screen.SOUND_RATE) // nm.BLOCK * nm.BLOCK > nm.PY_CUSHION
    assert source.read_ahead(nm.BLOCK) is None
    source._heard(np.full((1000, 2), 0.25, np.float32))
    assert source.read_ahead(nm.BLOCK) is None  # not a whole block yet
    source._heard(np.full((1100, 2), 0.5, np.float32))
    block = source.read_ahead(nm.BLOCK)
    assert block.shape == (nm.BLOCK, 2) and np.allclose(block[:1000], 0.25) and np.allclose(block[1000:], 0.5)
    assert source.read_ahead(nm.BLOCK).shape == (nm.BLOCK, 2) and source.read_ahead(nm.BLOCK) is None
    assert source._sound_frames == 2100 - 2 * nm.BLOCK

    class Engine:
        def __init__(self):
            self.cushions, self.pushed = [], []

        def cushion(self, sid, frames):
            self.cushions.append((sid, frames))

        def push(self, sid, block):
            self.pushed.append((sid, np.array(block)))

    mixer = nm.NativeMixer.__new__(nm.NativeMixer)
    mixer._engine, mixer._clocked = Engine(), True
    sound = nm._Sound(source, 7)
    mixer._cushion(source, sound)
    assert mixer._engine.cushions == [(7, nm.ahead_of(source))]
    mixer._push_ahead(source, sound, {})
    assert mixer._engine.pushed == []  # under a block left: nothing pushed, the engine plays from its queue
    source._heard(np.full((5 * nm.BLOCK, 2), 0.1, np.float32))  # held up, then it all comes at once
    mixer._push_ahead(source, sound, {})
    assert len(mixer._engine.pushed) == 5 and source._sound_frames == 2100 - 2 * nm.BLOCK


def test_a_reader_isnt_left_waiting_on_a_queue_nobody_takes_from():
    """Three "android-...-read" threads were still alive after a tester's stream (2026-11-27): each waited to put
    into the full queue of a recording that was over."""
    import io
    import queue as q
    import threading

    class Ended:
        def poll(self):
            return 0

    chunks = q.Queue(maxsize=1)
    chunks.put(b"x")
    t = threading.Thread(target=screen._read_into, args=(io.BytesIO(b"more"), chunks, Ended()))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive()


def test_ffmpegs_lines_about_a_damaged_picture_from_the_phone_stay_out_of_the_log():
    import logging
    import threading

    screen.quiet_damaged()
    screen.quiet_damaged()
    logger = logging.getLogger("libav.h264")
    assert sum(isinstance(f, screen.DamagedQuiet) for f in logger.filters) == 1
    record = logging.LogRecord("libav.h264", logging.ERROR, "", 0, "No start code is found.", None, None)
    kept = {}

    def phone():
        kept["phone"] = logger.filter(record)

    t = threading.Thread(target=phone, name="android-android-screen-1-4")
    t.start()
    t.join()
    assert not kept["phone"] and logger.filter(record)  # a video file's or a camera's are still said


# The engine's player decodes it (SPRIGCAST_ENGINE=native) --------------------------------------------------

def test_adbs_bytes_are_cut_into_whole_pictures(fake_adb):
    _adb, log = fake_adb
    data = (log.parent / "screen.h264").read_bytes()
    pictures = screen.Pictures()
    out = []
    for at in range(0, len(data), 7):  # pieces that end anywhere, as adb sends them
        whole = pictures.feed(data[at:at + 7])
        if whole:
            out.append(whole)
    assert pictures.open
    out.append(pictures.rest())
    assert b"".join(out) == data  # nothing lost or moved
    assert len(out) > 20 and all(piece.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")) for piece in out)
    # Each piece ends where a picture does: none starts in the middle of one (a slice that isn't a picture's first)
    for piece in out[1:]:
        at = piece.find(b"\x00\x00\x01") + 3
        kind = piece[at] & 0x1F
        assert kind in (6, 7, 8, 9) or (kind in (1, 5) and piece[at + 1] & 0x80)


def test_the_engines_look_gives_the_same_trim_and_turn_as_a_picture():
    from sprigcast import sprigcore

    def look(w, h, left=0, top=0, right=0, bottom=0, brightest=200):
        return sprigcore.MediaLook(1, w, h, left, top, right, bottom, brightest)

    assert screen.look_edges(look(1280, 576, left=40, right=3)) == (40, 0, 0, 0)  # a sliver isn't trimmed
    assert screen.look_edges(look(1280, 576, left=41)) == (40, 0, 0, 0)  # even
    assert screen.look_edges(look(1280, 576, left=500, right=500)) == (0, 0, 0, 0)  # too much: the picture is dark
    assert screen.look_boxed(look(1280, 576, left=500, right=500))  # the other shape boxed in the middle
    assert not screen.look_boxed(look(1280, 576, left=500, right=100))
    assert not screen.look_boxed(look(1280, 576, left=1280, right=1280, brightest=16))  # all black: a dark moment
    assert screen.look_boxed(look(576, 1280, top=450, bottom=450))
    assert not screen.look_boxed(look(576, 1280, left=200, right=200))


def _stub_engine(source, look_of):
    """An engine whose player says what the test wants (no library needed)."""
    from types import SimpleNamespace

    from sprigcast import sprigcore

    calls = []
    info = SimpleNamespace(faulted=0, status_text="ok", width=1280, height=576)
    player = SimpleNamespace(info=lambda: info, look=lambda: sprigcore.MediaLook(*look_of()),
                             set_crop=lambda *e: calls.append(("crop", e)),
                             set_hold=lambda h: calls.append(("hold", h)), close=lambda: None)
    source._engine = screen._Engine(player)
    source._engine.shape = (1280, 576)
    return calls


def test_with_the_engine_a_steady_bar_is_trimmed_there(monkeypatch):
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    clock = [100.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    serial = [0]
    calls = _stub_engine(source, lambda: (serial[0], 1280, 576, 60, 0, 0, 0, 200))
    source._current = 1
    for _ in range(screen.TRIM_SURE - 1):
        serial[0] += 1
        source._engine_look(1)
        clock[0] += screen.TRIM_EVERY_S
    assert calls == [] and source.size == (1280, 576) and source.status == "capturing 1280x576"
    serial[0] += 1
    source._engine_look(1)
    assert calls == [("crop", (60, 0, 0, 0))]
    source._engine_look(1)  # the same look again: nothing more
    assert len(calls) == 1
    source.settings["trim"] = False
    serial[0] += 1
    source._engine_look(1)
    assert calls[-1] == ("crop", (0, 0, 0, 0))


def test_with_the_engine_a_turned_phones_pictures_are_held_there(monkeypatch):
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    clock = [100.0]
    monkeypatch.setattr(screen.time, "monotonic", lambda: clock[0])
    calls = _stub_engine(source, lambda: (1, 1280, 576, 500, 0, 500, 0, 200))  # boxed: it turned under us
    source._current = 1
    source._engine_look(1)
    assert ("hold", True) in calls and source._hold == 1 and source._look_now.is_set()
    # The phone hadn't turned after all (a dark picture): it shows again, as _run does
    source._hold, source._not_turned = None, 1
    source._engine_hold()
    assert calls[-1] == ("hold", False)
    # A real turn with no next recording coming: shown again after HOLD_MOST_S
    source._hold_from(1)
    assert calls[-1] == ("hold", True)
    clock[0] += screen.HOLD_MOST_S + 0.1
    source._engine_look(1)
    assert calls[-1] == ("hold", False)


@pytest.fixture
def engine(player_lib, monkeypatch):  # noqa: F811
    from sprigcast import sprigcore

    if not sprigcore.camera_ready():
        pytest.skip("FFmpeg's input devices (libavdevice) aren't here")
    monkeypatch.setattr(screen, "use_engine", lambda: True)


def _engine_picture(source, seconds=8.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        frame = source.video_frame()
        if frame is not None:
            return frame
        time.sleep(0.05)
    return None


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_the_engine_decodes_the_phones_screen(fake_adb, engine):
    adb, _log = fake_adb
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        frame = _engine_picture(source)
        assert frame is not None, source.status
        assert source.engine_player is not None and source.channel_order.startswith("I420/")
        assert frame.shape == (176 + 88, 320)
        deadline = time.monotonic() + 3
        while not source.status.startswith("capturing") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source.status.startswith("capturing 320x176") and source.size == (320, 176)
        assert not source._waiting  # no picture went through Python
    finally:
        source.stop()
    assert source._engine is None and source.engine_player is None and not source._engine_refused


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_the_engine_shows_a_still_screens_one_picture(fake_adb, engine, monkeypatch):
    adb, _log = fake_adb
    monkeypatch.setenv("FAKE_STILL", "1")
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        assert _engine_picture(source) is not None, source.status
    finally:
        source.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_with_the_engine_the_next_recording_carries_on_and_a_turn_opens_afresh(fake_adb, engine, monkeypatch):
    adb, log = fake_adb
    monkeypatch.setattr(screen, "SEGMENT_S", 0.8)
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        assert _engine_picture(source) is not None, source.status
        first = source._engine.conn
        assert first is not None and source._engine.shape == (1280, 576)
        deadline = time.monotonic() + 8
        while source._current < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._current >= 2
        assert source._engine.conn is first  # the same shape: on the same connection, no gap
        assert _engine_picture(source) is not None
        (log.parent / "turn.txt").write_text("Viewport INTERNAL: displayId=0, orientation=ROTATION_0, x\n")
        deadline = time.monotonic() + 8
        while source._engine.shape != (576, 1280) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert source._engine.shape == (576, 1280) and source._engine.conn is not first
        assert not source._engine.held
        assert _engine_picture(source) is not None  # the freshly opened player shows the new recording
        assert source._engine.player.info().status == b"ok" and not source._engine_refused
    finally:
        source.stop()


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in adb is a script")
def test_python_decodes_when_the_engines_player_doesnt_connect(fake_adb, engine, monkeypatch):
    from sprigcast import sprigcore

    adb, _log = fake_adb
    monkeypatch.setattr(screen, "ENGINE_ACCEPT_S", 0.3)
    monkeypatch.setattr(screen, "RETRY_S", 0.1)
    monkeypatch.setattr(sprigcore.MediaPlayer, "set_device", lambda self, *a, **k: None)  # it never opens
    source = screen.AndroidScreenSource("android-1", dict(screen.DEFAULTS))
    source.adb_path = adb
    source.start()
    try:
        assert _engine_picture(source, 10.0) is not None, source.status
        assert source._engine_refused and source._engine is None and source.channel_order
    finally:
        source.stop()
