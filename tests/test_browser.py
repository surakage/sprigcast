import os
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


def test_chromium_draws_the_page_see_through(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtWidgets import QApplication

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
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
        assert host.pages["b"].paused
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
