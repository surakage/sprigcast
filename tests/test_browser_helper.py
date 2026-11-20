"""Browser pages in their own process (app/browser_helper.py): a page that takes its time to copy never
freezes SprigCast's window or renderer (the 2026-11-17 heavy run: 280-360 ms each time)."""

import os
import time

import numpy as np
import pytest

from sprigcast.app import browser_helper
from sprigcast.app.browser_helper import FrameSlot, RemoteHost


def test_both_slots_are_found_when_the_reader_sees_a_bigger_block():
    """Windows hands whoever opens a block its size rounded up to whole pages; the maker has the size it asked
    for. The second slot starts where the maker's header says, not at half of what each side sees."""
    import struct

    class Block:
        name = "block"

        def __init__(self, buf):
            self.buf = buf

        def close(self):
            pass

    pixels, exact = 8 * 4, browser_helper.HEADER + 2 * 8 * 4 * 4
    memory = memoryview(bytearray(4096))  # what the reader sees: a whole page
    struct.pack_into("<Q", memory, browser_helper.SLOT_BYTES_AT, pixels * 4)
    writer, reader = FrameSlot(Block(memory[:exact]), owner=False), FrameSlot(Block(memory), owner=False)
    assert writer.capacity == reader.capacity == pixels * 4
    for n in range(4):  # both slots, twice
        pic = (np.arange(4 * 8 * 4, dtype=np.uint8) + n).reshape(4, 8, 4)
        assert writer.write(pic)
        assert (reader.read() == pic).all()
    struct.pack_into("<Q", memory, browser_helper.SLOT_BYTES_AT, 0)  # a block with no size in it: as before
    assert FrameSlot(Block(memory), owner=False).capacity == (4096 - browser_helper.HEADER) // 2


def test_pictures_pass_through_shared_memory_whole():
    owner = FrameSlot.create(f"sprigcast-test-{os.getpid()}", 8 * 4)
    reader = FrameSlot(browser_helper._attach(owner.shm.name), owner=False)
    try:
        assert reader.read() is None  # nothing yet
        a = np.full((4, 8, 4), 7, dtype=np.uint8)
        b = np.arange(2 * 3 * 4, dtype=np.uint8).reshape(2, 3, 4)
        assert owner.write(a)
        got = reader.read()
        assert got.shape == (4, 8, 4) and (got == 7).all()
        assert reader.read() is None  # taken: no repeat
        owner.write(a)
        owner.write(b)  # two pictures before a read: the newest is taken
        got = reader.read()
        assert got.shape == (2, 3, 4) and (got == b).all()
        assert not owner.write(np.zeros((10, 10, 4), dtype=np.uint8))  # bigger than its block: a new one is made
    finally:
        reader.close()
        owner.close()


def _wait(check, seconds=40.0, app=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if app is not None:
            app.processEvents()
        if check():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def qt():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_page_opened_again_always_says_where_its_pictures_are(qt):
    """Open, close and open again in quick succession: the helper let go of the page's block when the close was
    asked for, before the page (opened a moment after its ask) had made it. The block stayed, the next opening
    used it without a word, and SprigCast never found the page's pictures: an empty page for good."""
    from sprigcast.app import browser_host

    said = []
    blocks = browser_helper.PageBlocks(lambda header, payload=b"": said.append(header))
    host = browser_host.BrowserHost()
    host.page_closed = blocks.drop

    class Page:
        def __init__(self, source_id):
            self.id = source_id
            blocks.slot_for(source_id, 8 * 8)  # as a page on the card does when it is made (target_for)

        def close(self):
            pass

    host._new_page = lambda source_id, settings: Page(source_id)
    host._check_state = host._tick_state = lambda: None
    try:
        host.open("b", {})
        host.close("b")  # both asked before either is done
        qt.processEvents()
        assert "b" not in blocks.slots and "b" not in host.pages  # the block went with the page
        host.open("b", {})
        qt.processEvents()
        names = [h["name"] for h in said if h["kind"] == "frames" and h["id"] == "b"]
        assert len(names) == 2 and names[0] != names[1] and blocks.slots["b"].shm.name == names[1]
        host.close("b")
        host.close("b")  # a close for a page already gone still lets go, and nothing breaks
        qt.processEvents()
        assert not blocks.slots
    finally:
        blocks.close()
        host.shutdown()


def test_a_page_is_drawn_in_the_helper_and_comes_back_after_it_stops(tmp_path, qt, monkeypatch):
    import sys

    if sys.platform == "win32":
        pytest.skip("Chromium doesn't paint under Qt's offscreen platform on Windows")
    monkeypatch.setattr(browser_helper, "RESTART_AFTER_S", 0.2)
    page = tmp_path / "overlay.html"
    page.write_text("<body style='margin:0'><div style='width:50px;height:40px;background:rgb(255,0,0)'></div>"
                    "<script>setInterval(() => document.title = Date.now(), 100)</script></body>")
    host = RemoteHost()
    states = []
    host.state_of = lambda: states.append(1) or {"streaming": True, "blooms": {}, "shapes": {}}
    settings = {"file": str(page), "width": 120, "height": 80, "fps": 30}
    try:
        host.open("b", settings)
        frames = []
        assert _wait(lambda: (f := host.take("b")) is not None and frames.append(f) is None
                     and f[10, 10, 0] == 255, app=qt), "the page never drew"
        frame = frames[-1]
        assert frame.shape == (80, 120, 4) and tuple(frame[10, 10]) == (255, 0, 0, 255) and frame[70, 100, 3] == 0
        assert _wait(lambda: bool(states), 3.0, app=qt)  # the window's state goes to the page
        first = host.proc
        first.kill()  # the helper stops (a crash): it starts again with the same page
        assert _wait(lambda: host.starts == 2 and host.take("b") is not None, app=qt)
        host.close("b")
        assert "b" not in host._wanted and host.take("b") is None
    finally:
        host.shutdown()
    assert host.proc is None and first.poll() is not None


def test_the_helper_ends_once_no_page_is_left(tmp_path, qt, monkeypatch):
    import sys

    if sys.platform == "win32":
        pytest.skip("Chromium doesn't paint under Qt's offscreen platform on Windows")
    monkeypatch.setattr(browser_helper, "LINGER_S", 0.5)
    page = tmp_path / "p.html"
    page.write_text("<body style='background:rgb(0,0,255)'></body>")
    host = RemoteHost()
    try:
        host.open("b", {"file": str(page), "width": 40, "height": 30, "fps": 10})
        assert _wait(lambda: host.take("b") is not None, app=qt)
        proc = host.proc
        host.close("b")
        assert _wait(lambda: proc.poll() is not None, 10.0, app=qt) and host.proc is None
        host.open("b", {"file": str(page), "width": 40, "height": 30, "fps": 10})  # back: a new helper
        assert _wait(lambda: host.take("b") is not None, app=qt) and host.starts == 2
    finally:
        host.shutdown()


def test_without_a_helper_it_says_so_and_the_window_carries_on(monkeypatch, qt):
    host = RemoteHost(command=["/no/such/program"])
    try:
        host.open("b", {"url": "https://example.com"})
        assert host.proc is None and host.take("b") is None and host.read_audio("b", 10) is None
        host.pause("b", True)
        host.close("b")
    finally:
        host.shutdown()


def test_start_host_uses_the_helper_unless_told_not_to(monkeypatch, qt):
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from sprigcast.app import browser_host

    monkeypatch.delenv("SPRIGCAST_BROWSER_IN_PROCESS", raising=False)
    host = browser_host.start_host()
    try:
        assert isinstance(host, RemoteHost) and host.proc is None  # nothing started until a page opens
    finally:
        host.shutdown()
    monkeypatch.setenv("SPRIGCAST_BROWSER_IN_PROCESS", "1")
    host = browser_host.start_host()
    try:
        assert isinstance(host, browser_host.BrowserHost)
    finally:
        host.shutdown()


def test_page_sound_comes_through_the_pipe_to_the_mixer(tmp_path, qt):
    pytest.importorskip("soxr")
    from sprigcast.sources.browser import BrowserSource, set_host

    page = tmp_path / "beep.html"
    page.write_text("<script>const ctx = new AudioContext({sampleRate: 44100}); const o = ctx.createOscillator();"
                    " o.connect(ctx.destination); o.start();</script>")
    host = RemoteHost()
    set_host(host)
    try:
        settings = {"file": str(page), "width": 64, "height": 64, "fps": 10}
        host.open("b", settings)
        source = BrowserSource("b", settings)
        source._open = True
        heard = [0.0]

        def loud():
            heard[0] = max(heard[0], float(np.abs(source.read_block(1024)).max()))
            return heard[0] > 0.5
        assert _wait(loud, app=qt), "no sound reached the mixer"
        assert host.sound_note("b") == ""
    finally:
        set_host(None)
        host.shutdown()


def test_a_video_that_cant_play_is_told_through_the_pipe(tmp_path, qt):
    """The helper's page says its MP4 can't play: SprigCast's source shows it (the pip page engine has no H.264)."""
    from sprigcast.app.browser_host import MEDIA_NOTES
    from sprigcast.sources.browser import BrowserSource, set_host

    page = tmp_path / "clip.html"  # names an MP4 the engine can't open: a format error like a real H.264 clip
    (tmp_path / "clip.mp4").write_bytes(b"\0" * 64)
    page.write_text("<body><video autoplay muted src='clip.mp4'></video></body>")
    host = RemoteHost()
    set_host(host)
    try:
        settings = {"file": str(page), "width": 64, "height": 64, "fps": 10}
        host.open("v", settings)
        source = BrowserSource("v", settings)
        source._open, source.status = True, "ok"
        assert _wait(lambda: bool(host.page_note("v")), app=qt), "the page's note never came"
        assert host.page_note("v") in (MEDIA_NOTES["h264"], MEDIA_NOTES["format"])
        source.engine_tick()
        assert source.status == host.page_note("v")
        host.close("v")
        assert host.page_note("v") == ""
    finally:
        set_host(None)
        host.shutdown()
