"""Android Screen: a phone's screen over USB through Android's own screen recorder (sources/android_screen.py).
A stand-in adb plays the part of the phone here."""

import stat
import sys
import textwrap
import time

import numpy as np
import pytest

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
                                     "trim": True}
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
    assert screen.find_adb() == str(tmp_path / "adb" / "platform-tools" / name)


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
    finally:
        source.stop()


def test_a_place_fits_its_canvas_again_when_the_phone_turns():
    """User 2026-11-26: fit automatically on every turn, centered, instead of resizing it each time."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    col = SceneCollection(canvases=[Canvas(id="main", width=1920, height=1080)],
                          sources=[SourceConfig(id="android-1", kind="android_screen", name="Phone")],
                          scenes=[Scene(id="a", name="A", canvas="main", items=[
                              SceneItem(source="android-1", x=100, y=50, scale=0.5, crop=(4, 0, 0, 0))])])
    item = col.scenes[0].items[0]
    assert col.fit_places("android-1", (1280, 576))  # landscape: as wide as the canvas, centered up and down
    assert abs(1280 * item.scale - 1920) < 0.5 and abs(item.x) < 0.5 and abs(item.y - (1080 - 576 * 1.5) / 2) < 0.5
    assert item.crop == (0, 0, 0, 0)
    assert col.fit_places("android-1", (576, 1280))  # portrait: as tall as the canvas, centered across
    assert abs(1280 * item.scale - 1080) < 0.5 and abs(item.y) < 0.5
    assert abs(item.x + 576 * item.scale / 2 - 960) < 0.5
    assert not col.fit_places("android-2", (576, 1280))



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
        assert source._sound_frames <= screen.SOUND_KEEP_S * screen.SOUND_RATE + 960
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
