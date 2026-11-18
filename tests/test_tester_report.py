import logging
import os
import threading
import zipfile
from datetime import datetime

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QSettings  # noqa: E402

from sprigcast.app import logbook, tester_report  # noqa: E402
from sprigcast.app.tester_report import HIDDEN, Scrubber  # noqa: E402
from sprigcast.core.scene import Canvas, Output, Scene, SceneCollection, SourceConfig  # noqa: E402

TWITCH_KEY = "live_123456789_AbCdEfGhIjKlMnOpQrSt"
PASSWORD = "hunter2-sprig-pass"


def test_scrubber_hides_keys_passwords_people_and_addresses():
    scrub = Scrubber(secrets=[PASSWORD, "my-custom-key-9876"], names=["surar", "DESKTOP-ZX81"])
    text = scrub(
        f"stream Twitch dropped: rtmp://live.twitch.tv/app/{TWITCH_KEY} refused\n"
        "custom: rtmps://ingest.example.com:443/live/my-custom-key-9876?x=1\n"
        f"control password {PASSWORD}; token=abc123def456; Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.x.y\n"
        "logs in C:\\Users\\surar\\Documents\\SprigCast\\Logs and C:/Users/Surar/Videos\n"
        "youtube key abcd-efgh-ijkl-mnop-qrst, kick sk_us-AbC123dEf456\n"
        "mail me at someone@example.com from 203.0.113.7; local 127.0.0.1:4456 and 192.168.1.20\n"
        "PC DESKTOP-ZX81 user surar\n"
        "keyframes every 2 s, hotkey: Ctrl+Alt+2, 1920x1080 @ 60 fps, Windows 11 build 26300.9711")
    for private in (TWITCH_KEY, "my-custom-key-9876", PASSWORD, "abc123def456", "eyJhbGciOiJIUzI1NiJ9", "surar",
                    "Surar", "abcd-efgh", "sk_us-AbC123", "someone@example.com", "203.0.113.7", "DESKTOP-ZX81"):
        assert private not in text, private
    assert "rtmp://live.twitch.tv/" + HIDDEN in text  # the server stays: it says where it went
    assert "C:\\Users\\you\\Documents\\SprigCast\\Logs" in text
    assert "127.0.0.1:4456" in text and "192.168.1.20" in text
    assert "keyframes every 2 s, hotkey: Ctrl+Alt+2, 1920x1080 @ 60 fps, Windows 11 build 26300.9711" in text


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        sources=[SourceConfig(id="cam", kind="camera", name="Facecam of surar"),
                 SourceConfig(id="mic", kind="audio_input", name="Mic")],
        scenes=[Scene(id="h1", name="Main", canvas="horizontal"), Scene(id="v1", name="Main", canvas="vertical")],
        active_scenes={"horizontal": "h1", "vertical": "v1"},
        outputs=[
            Output(id="tw", type="stream", service="twitch", canvas="horizontal", key_ref="k/tw", enabled=True),
            Output(id="cu", type="stream", service="custom", canvas="vertical", key_ref="k/cu",
                   server="rtmp://user:pw@ingest.example.com/live/secret-path"),
        ],
    )


def _settings(tmp_path):
    q = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    q.setValue("control/password", PASSWORD)
    q.setValue("control/devices", '[{"name": "Phone", "key": "dev-key-0123456789"}]')
    q.setValue("chat/names", '{"youtube": "@surar_live"}')
    q.setValue("general/theme", "night canopy")
    q.setValue("window/geometry", b"\x01\x02")
    q.setValue("hotkeys/scene_brb", "Ctrl+Alt+2")
    return q


def test_report_zip_has_logs_pc_and_settings_with_nothing_private(tmp_path, monkeypatch):
    import keyring

    keys = {"k/tw": TWITCH_KEY, "k/cu": "custom-key-5555"}
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: keys.get(ref))
    now = datetime(2026, 11, 16, 12, 0)
    logs = tmp_path / "Logs"
    logs.mkdir()

    def made(name, text, days_ago=0):
        path = logs / name
        path.write_text(text, encoding="utf-8")
        stamp = now.timestamp() - days_ago * 86400
        os.utime(path, (stamp, stamp))

    made("sprigcast 2026-11-16.log", f"WARNING stream Twitch dropped: rtmp://x.tv/app/{TWITCH_KEY}\n"
         "INFO api password custom-key-5555 dev-key-0123456789 in C:\\Users\\surar\\Videos\n")
    made("stream 2026-11-15 20-00-00.txt", "0:05  Twitch: live, 6000 / 6000 kbps\n", 1)
    made("sprigcast 2026-11-01.log", "too old\n", 15)
    made("crash.log", f"=== SprigCast run 0.2.0 pid 1 started\n{PASSWORD}\n")
    target = tmp_path / "out" / "report.zip"
    monkeypatch.setattr(tester_report.Scrubber, "for_this_pc",
                        classmethod(lambda cls, secrets=(): cls(secrets, ["surar"])))
    tester_report.build(target, log_folder=logs, collection=_collection(), settings=_settings(tmp_path), now=now,
                        about=["SprigCast 0.2.0", "Windows: Windows 11 Home 26H2, build 26300.9711"])
    with zipfile.ZipFile(target) as z:
        names = sorted(z.namelist())
        texts = {n: z.read(n).decode("utf-8") for n in names}
    assert names == ["about.txt", "logs/crash.log", "logs/sprigcast 2026-11-16.log",
                     "logs/stream 2026-11-15 20-00-00.txt"]
    everything = "\n".join(texts.values())
    for private in (TWITCH_KEY, "custom-key-5555", PASSWORD, "dev-key-0123456789", "surar", "secret-path", "user:pw"):
        assert private not in everything, private
    about = texts["about.txt"]
    assert "build 26300.9711" in about
    assert "horizontal: 1920x1080 @ 60 fps" in about
    assert "twitch on horizontal: 6000 kbps" in about and "key saved" in about
    assert "server rtmp://ingest.example.com/" + HIDDEN in about
    assert "camera 1" in about and "audio_input 1" in about
    assert "general/theme = night canopy" in about
    assert "control/password = (set)" in about and "chat/names = (set)" in about
    assert "hotkeys/scene_brb = Ctrl+Alt+2" in about
    assert "window/geometry" not in about
    assert not list(target.parent.glob("*.part"))


def test_floods_hold_back_repeats_and_say_how_many():
    clock = [0.0]
    floods = logbook.Floods(clock=lambda: clock[0])

    def record(msg="frame failed: %s"):
        return logging.LogRecord("sprigcast.x", logging.ERROR, __file__, 1, msg, ("boom",), None)

    passed = [floods.filter(record()) for _ in range(50)]
    assert passed.count(True) == logbook.Floods.LIMIT
    assert floods.filter(record("something else"))  # other messages are not held
    clock[0] = 61.0
    again = record()
    assert floods.filter(again) and again.sprig_held == 50 - logbook.Floods.LIMIT
    text = logbook._Formatter(logbook.FORMAT).format(again)
    assert text.endswith("(and 40 more like it held back)")


def test_unhandled_errors_reach_the_log(caplog):
    import sys

    saved = sys.excepthook, threading.excepthook, sys.unraisablehook
    try:
        logbook.catch_unhandled()
        with caplog.at_level(logging.ERROR, logger="sprigcast.errors"):
            def broken():
                raise ValueError("thread went wrong")

            t = threading.Thread(target=broken, name="worker-1")
            t.start()
            t.join()
            try:
                raise RuntimeError("button went wrong")
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        text = caplog.text
        assert "unhandled error in thread worker-1" in text and "thread went wrong" in text
        assert "button went wrong" in text
    finally:
        sys.excepthook, threading.excepthook, sys.unraisablehook = saved
        from PySide6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(None)


def test_file_menu_exports_a_report(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(tmp_path / "Logs"))
    (tmp_path / "Logs").mkdir()
    (tmp_path / "Logs" / f"sprigcast {datetime.now():%Y-%m-%d}.log").write_text("INFO hello\n", encoding="utf-8")
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        help_menu = w.menuBar().actions()[6].menu()
        assert "Export report" in [a.text() for a in help_menu.actions()]
        made = w.export_report(tmp_path / "SprigCast report", reveal=False)
        w._report_future.result(timeout=30)  # made on a worker: the window never waits for it
        app.processEvents()
        assert made == tmp_path / "SprigCast report.zip"
        with zipfile.ZipFile(made) as z:
            assert "about.txt" in z.namelist() and any(n.startswith("logs/sprigcast ") for n in z.namelist())
            assert "Windows: " in z.read("about.txt").decode()
        assert w.statusBar().currentMessage() == "Report saved: SprigCast report.zip"
    finally:
        w.close()


def test_stream_says_when_it_is_back_live(caplog):
    from types import SimpleNamespace

    from sprigcast.output.stream import log_live

    with caplog.at_level(logging.INFO, logger="sprigcast.output.stream"):
        log_live(SimpleNamespace(name="Twitch", reconnects=0))
        log_live(SimpleNamespace(name="Twitch", reconnects=2))
    assert "stream Twitch live" in caplog.text
    assert "stream Twitch back live after 2 reconnects" in caplog.text
