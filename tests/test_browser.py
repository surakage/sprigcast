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
