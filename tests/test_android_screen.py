"""Android Screen: a phone's screen over USB through Android's own screen recorder (sources/android_screen.py).
A stand-in adb plays the part of the phone here."""

import stat
import sys
import textwrap
import time

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
    assert screen.parse_turned("    SurfaceOrientation: 1\n") and not screen.parse_turned("SurfaceOrientation: 0")


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
    log = tmp_path / "calls.txt"
    script = tmp_path / "adb"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, time
        args = sys.argv[1:]
        open({str(log)!r}, "a").write(" ".join(args) + "\\n")
        if args[:1] == ["devices"]:
            print("List of devices attached")
            print("FAKE01 device usb:1 model:Test_Phone")
        elif "wm" in args:
            print("Physical size: 1080x2400")
        elif "dumpsys" in args:
            print("SurfaceOrientation: 1")
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
    assert dialog.values() == {"serial": "A1", "longest": 1920, "bitrate_kbps": 12000}
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
