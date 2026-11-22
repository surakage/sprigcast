import os
import sys
import time

import numpy as np
import pytest

from sprigcast.sources import browser
from sprigcast.sources.browser import BrowserSource, page_name, page_url


def test_page_url_and_name(tmp_path):
    assert page_url({"url": "https://"}) == ""
    assert page_url({"url": "tikfinity.zerody.one/widget"}) == "https://tikfinity.zerody.one/widget"
    page = tmp_path / "my_alerts.html"
    assert page_url({"url": "https://x.com", "file": str(page)}).startswith("file:")
    assert page_name({"url": "https://streamelements.com/overlay/1"}) == "Streamelements"
    assert page_name({"file": str(page)}) == "My alerts"
    assert page_name({}) == "Browser"


def test_without_a_host_it_says_so_and_stays_see_through():
    browser.set_host(None)
    source = BrowserSource("b", {"url": "https://example.com", "width": 640, "height": 360})
    source.start()
    assert "WebEngine" in source.status
    assert source.size == (640, 360)
    frame = source.video_frame()
    assert frame is not None and frame[..., 3].max() == 0
    assert source.video_frame() is None


class FakeHost:
    def __init__(self):
        self.calls = []
        self.frame = None

    def open(self, sid, settings):
        self.calls.append(("open", settings.get("url")))

    def close(self, sid):
        self.calls.append(("close",))

    def pause(self, sid, paused):
        self.calls.append(("pause", paused))

    def take(self, sid):
        frame, self.frame = self.frame, None
        return frame


def test_sleep_closes_the_page_unless_kept_running():
    host = FakeHost()
    browser.set_host(host)
    try:
        source = BrowserSource("b", {"url": "https://a.com"})
        source.start()
        source.sleep()
        source.wake()
        assert host.calls == [("open", "https://a.com"), ("close",), ("open", "https://a.com")]
        host.calls.clear()
        source.update({"url": "https://a.com", "keep_running": True})
        source.sleep()
        source.wake()
        assert host.calls == [("open", "https://a.com"), ("pause", True), ("pause", False)]
        host.frame = np.ones((4, 4, 4), np.uint8)
        assert source.video_frame().shape == (4, 4, 4)
    finally:
        browser.set_host(None)


def _skip_if_offscreen_windows(app):
    """Chromium paints nothing under Qt's offscreen platform on Windows (it does in the app, with real
    windows: checked on a Windows 11 PC), so these drawing checks only run on Linux CI."""
    if sys.platform == "win32" and app.platformName() == "offscreen":
        pytest.skip("Chromium doesn't paint under Qt's offscreen platform on Windows")


def test_chromium_draws_the_page_see_through(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    _skip_if_offscreen_windows(app)
    from sprigcast.app.browser_host import start_host

    page = tmp_path / "overlay.html"
    page.write_text("<body><div style='width:50px;height:40px;background:rgb(255,0,0)'></div>"
                    "<script>window.seen = typeof window.obsstudio</script></body>")
    host = start_host()
    assert host is not None
    try:
        host.open("b", {"file": str(page), "width": 200, "height": 100, "fps": 30, "css": browser.DEFAULT_CSS})
        frame, deadline = None, time.monotonic() + 20
        while time.monotonic() < deadline:
            app.processEvents()
            got = host.take("b")
            if got is not None and got[10, 10, 0] == 255:
                frame = got
                break
            time.sleep(0.02)
        assert frame is not None, "the page never drew"
        assert frame.shape == (100, 200, 4)
        assert tuple(frame[10, 10]) == (255, 0, 0, 255)
        assert frame[80, 150, 3] == 0  # the page's background is see-through
        host.pause("b", True)
        app.processEvents()
        page_b = host.pages["b"]
        assert page_b.paused and not page_b.page.isVisible()  # hidden like a background tab: Chromium throttles it
        assert page_b.timer.interval() == 1000
        host.pause("b", False)
        app.processEvents()
        assert page_b.page.isVisible() and page_b.timer.interval() == round(1000 / 30)
        host.close("b")
        app.processEvents()
        assert "b" not in host.pages
    finally:
        host.shutdown()


def test_page_sound_goes_to_the_mixer_not_the_desktop(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    pytest.importorskip("soxr")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    from sprigcast.app.browser_host import start_host

    page = tmp_path / "beep.html"  # an alert that plays a tone through Web Audio, at 44.1 kHz
    page.write_text("<script>const ctx = new AudioContext({sampleRate: 44100}); const o = ctx.createOscillator();"
                    " o.connect(ctx.destination); o.start();</script>")
    host = start_host()
    try:
        settings = {"file": str(page), "width": 64, "height": 64, "fps": 10}
        host.open("b", settings)
        source = BrowserSource("b", settings)
        source._open = True
        heard, deadline = 0.0, time.monotonic() + 20
        while time.monotonic() < deadline and heard < 0.5:
            app.processEvents()
            heard = max(heard, float(np.abs(source.read_block(1024)).max()))
            time.sleep(0.01)
        assert heard > 0.5, "no sound reached the mixer"
        assert host.pages["b"].page.isAudioMuted()  # not also on the desktop
        assert source.gain == 1.0
        source.update({**settings, "muted": True})
        assert source.gain == 0.0
        assert host.sound_note("b") == ""
    finally:
        host.shutdown()


def _serve_tone(tmp_path, cors: bool):
    import http.server
    import threading
    import wave

    tone = tmp_path / "tone.wav"
    with wave.open(str(tone), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        t = np.arange(48000 * 3) / 48000
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 30000).astype("<i2").tobytes())

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(tmp_path), **k)

        def end_headers(self):
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/tone.wav"


@pytest.mark.parametrize("cors", [True, False])
def test_an_alert_sound_from_another_site(tmp_path, cors):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    from sprigcast.app.browser_host import start_host

    server, url = _serve_tone(tmp_path, cors)
    page = tmp_path / "alert.html"
    page.write_text(f"<script>new Audio('{url}').play();</script>")
    host = start_host()
    try:
        settings = {"file": str(page), "width": 64, "height": 64, "fps": 10}
        host.open("b", settings)
        source = BrowserSource("b", settings)
        source._open = True
        heard, deadline = 0.0, time.monotonic() + 15
        while time.monotonic() < deadline:
            app.processEvents()
            heard = max(heard, float(np.abs(source.read_block(1024)).max()))
            if heard > 0.5 or host.sound_note("b"):
                break
            time.sleep(0.01)
        if cors:  # the site allows listening: the alert is in the mixer, silent on the desktop
            assert heard > 0.5 and host.pages["b"].page.isAudioMuted()
        else:  # it doesn't: the page plays on the desktop instead, and says so
            assert host.sound_note("b") and not host.pages["b"].page.isAudioMuted()
    finally:
        host.shutdown()
        server.shutdown()


def test_interact_puts_the_page_on_screen_and_back(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    _skip_if_offscreen_windows(app)
    from sprigcast.app.browser_host import start_host

    page = tmp_path / "widget.html"
    page.write_text("<body><div id='box' style='width:50px;height:40px;background:rgb(255,0,0)'></div></body>")
    host = start_host()
    settings = {"file": str(page), "width": 200, "height": 100, "fps": 30, "css": browser.DEFAULT_CSS}

    def wait_for(colour):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            app.processEvents()
            got = host.take("w")
            if got is not None and tuple(got[10, 10, :3]) == colour:
                return True
            time.sleep(0.02)
        return False

    try:
        window = host.interact("w", settings, "Widget")  # asleep: opens the page first
        assert window is not None and "w" in host.pages
        view = host.pages["w"].view
        assert view.window() is window
        assert not view.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        assert host.interact("w", settings, "Widget") is window  # a second ask raises the same window
        assert wait_for((255, 0, 0)), "the page never drew while on screen"
        view.page().runJavaScript("document.getElementById('box').style.background='rgb(0,0,255)'")
        assert wait_for((0, 0, 255)), "stream frames stopped while interacting"
        window.close()
        app.processEvents()
        assert host.pages["w"].interacting is None
        assert view.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        assert view.parent() is None
        view.page().runJavaScript("document.getElementById('box').style.background='rgb(0,255,0)'")
        assert wait_for((0, 255, 0)), "the page stopped drawing after going back off screen"
        host.close("w")
        app.processEvents()
        assert "w" not in host.pages
    finally:
        host.shutdown()


def test_an_empty_page_opens_no_browser_until_a_link_is_added():
    host = FakeHost()
    browser.set_host(host)
    try:
        source = BrowserSource("b", {"url": "https://"})
        source.start()
        assert source.status == "No page set" and host.calls == []  # no Chromium page for nothing
        source.update({"url": "https://a.com"})
        assert host.calls == [("open", "https://a.com")] and source.status == "ok"
        source.update({"url": ""})
        assert host.calls[-1] == ("close",) and source.status == "No page set"
        source.sleep()
        source.update({"url": "https://b.com"})
        assert host.calls[-1] == ("close",)  # asleep: waits until it's on screen again
        source.wake()
        assert host.calls[-1] == ("open", "https://b.com")
    finally:
        browser.set_host(None)


def test_a_cut_to_a_closed_page_waits_for_its_first_paint():
    """The bloom switch handover: the page starts loading as the switch starts, and the old bloom stays up
    until it has drawn (the clear placeholder doesn't count), at most WAKE_WAIT_S."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render.engine import WAKE_WAIT_S, Engine

    host = FakeHost()
    browser.set_host(host)
    try:
        col = SceneCollection(
            canvases=[Canvas(id="h", width=64, height=36)],
            sources=[SourceConfig(id="p", kind="browser", name="Alerts", settings={"url": "https://a.com"})],
            scenes=[Scene(id="one", name="One", canvas="h", items=[]),
                    Scene(id="two", name="Two", canvas="h", items=[SceneItem(source="p")])],
            active_scenes={"h": "one"},
        )
        engine = Engine(col)
        page = BrowserSource("p", {"url": "https://a.com"})
        page.asleep = True
        engine._sources = {"p": page}
        assert page.slow_wake
        new = col.model_copy(deep=True)
        new.active_scenes = {"h": "two"}
        engine._wait_for_waking("h", col.scenes[0], new, "two")
        page.asleep = False
        page.wake()  # loads as the switch starts
        assert host.calls == [("open", "https://a.com")]

        class Comp:
            textures = {}

        assert page.video_frame() is not None  # the clear placeholder
        Comp.textures["p"] = object()
        now = time.perf_counter()
        assert engine._still_waking(Comp, "h", now) is not None  # not painted yet: the old bloom stays
        host.frame = np.ones((4, 4, 4), np.uint8)
        page.video_frame()
        assert engine._still_waking(Comp, "h", now) is None  # first paint: the cut lands

        engine._wait_for_waking("h", col.scenes[0], new, "two")
        assert "h" not in engine._waking  # awake already: nothing to wait for
        page.update({"url": "https://a.com", "keep_running": True})
        page.sleep()
        page.asleep = True
        assert not page.slow_wake  # a page kept running is back at once
        page._paused_at -= browser.RELEASE_AFTER_S  # ...until it has let go of its graphics device
        assert page.slow_wake
        page.first_picture = True
        page.wake()
        assert not page.first_picture  # its first picture back is waited for
        page.sleep()
        page.asleep = True
        page.update({"url": "https://a.com"})
        page.sleep()
        assert page.slow_wake
        page.wake()
        engine._waking["h"] = (col.scenes[0], now + WAKE_WAIT_S, {"p"})
        assert engine._still_waking(Comp, "h", now + WAKE_WAIT_S + 0.1) is None  # never later than the cap
    finally:
        browser.set_host(None)


def _clip(path, codec: str, fmt: str) -> None:
    """A 1 s 64x64 clip in `codec` (skips when this PyAV has no such encoder)."""
    av = pytest.importorskip("av")
    if codec not in av.codecs_available:
        pytest.skip(f"no {codec} encoder here")
    with av.open(str(path), "w", format=fmt) as out:
        stream = out.add_stream(codec, rate=10)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        for i in range(10):
            frame = av.VideoFrame.from_ndarray(np.full((64, 64, 3), i * 20, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def _wait_note(app, host, source_id: str, seconds: float) -> str:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if host.page_note(source_id):
            break
        time.sleep(0.02)
    return host.page_note(source_id)


def test_a_video_the_page_engine_cant_play_shows_in_the_status(tmp_path):
    """The pip PySide6 page engine has no H.264/AAC: a clip player's MP4 used to show nothing, silently
    (StreamBridge's Random Clip Player, 2026-11-20). An MP4 file is converted to WebM now (app/page_video.py) and
    plays; what can't be converted (a streaming player asking for H.264) says so in the status, once per page."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    from sprigcast.app.browser_host import MEDIA_NOTES, start_host

    _clip(tmp_path / "clip.mp4", "libx264", "mp4")
    _clip(tmp_path / "clip.webm", "libvpx-vp9", "webm")
    (tmp_path / "mp4.html").write_text(
        "<body><video autoplay muted src='clip.mp4' onplaying='document.title += \" playing\"'></video><script>"
        "document.title = document.createElement('video').canPlayType('video/mp4; codecs=\"avc1.42E01E\"') || 'no'"
        "</script></body>")
    (tmp_path / "frame.html").write_text("<body><iframe src='mp4.html'></iframe></body>")
    (tmp_path / "mse.html").write_text(  # a streaming player (MSE) asking for H.264
        "<body><video></video><script>const ms = new MediaSource(); document.querySelector('video').src ="
        " URL.createObjectURL(ms); ms.addEventListener('sourceopen', () => { try {"
        " ms.addSourceBuffer('video/mp4; codecs=\"avc1.42E01E\"') } catch (e) {} })</script></body>")
    (tmp_path / "webm.html").write_text("<body><video autoplay muted loop src='clip.webm'></video></body>")
    host = start_host()
    try:
        host.open("w", {"file": str(tmp_path / "webm.html"), "width": 64, "height": 64, "fps": 10})
        host.open("m", {"file": str(tmp_path / "mp4.html"), "width": 64, "height": 64, "fps": 10})
        host.open("f", {"file": str(tmp_path / "frame.html"), "width": 200, "height": 200, "fps": 10})
        host.open("s", {"file": str(tmp_path / "mse.html"), "width": 64, "height": 64, "fps": 10})
        note = _wait_note(app, host, "s", 20)
        # The MP4 page's own answer, once it has loaded: until then its title is its file's name, which read as
        # "this engine plays H.264" when the streaming page's note came first (a busy run, 2026-11-21).
        deadline = time.monotonic() + 20
        while host.pages["m"].page.title().split(" ")[0] not in ("", "no", "maybe", "probably") and \
                time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
        if host.pages["m"].page.title().split(" ")[0] not in ("", "no"):
            assert note == "" and host.page_note("m") == "", "a page engine that plays H.264 blamed an MP4 on it"
            return
        assert note == MEDIA_NOTES["h264"]  # a streaming player (MSE) asking for H.264: nothing to convert
        deadline = time.monotonic() + 30
        while "playing" not in host.pages["m"].page.title() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.05)
        assert host.pages["m"].page.title() == "no playing"  # the MP4 plays, converted to WebM
        assert host.page_note("m") == "" and _wait_note(app, host, "f", 5) == ""  # an iframe's clip too
        assert _wait_note(app, host, "w", 1) == ""  # VP9 plays: nothing to say

        source = BrowserSource("s", {"file": str(tmp_path / "mse.html")})
        source._open, source.status = True, "ok"
        source.video_frame()
        assert source.status == MEDIA_NOTES["h264"]

        host.reload("s")  # loaded again: the note goes until the page says it again
        app.processEvents()
        assert host.page_note("s") == ""
        assert _wait_note(app, host, "s", 20) == MEDIA_NOTES["h264"]
        host.close("s")
        app.processEvents()
        assert host.page_note("s") == ""
    finally:
        host.shutdown()


def test_page_console_errors_are_logged_a_few_per_load(caplog):
    import logging
    from types import SimpleNamespace

    from sprigcast.app import browser_host

    page = SimpleNamespace(id="browser-69", _errors_told=0)
    caplog.set_level(logging.INFO, logger="sprigcast")
    for i in range(6):
        browser_host._Page._console_error(page, f"Error: Firebase:\n  auth failed {i}")
    said = [r.getMessage() for r in caplog.records if "the page says" in r.getMessage()]
    assert len(said) == browser_host.ERRORS_LOGGED
    assert said[0] == "browser source browser-69: the page says: Error: Firebase: auth failed 0"
