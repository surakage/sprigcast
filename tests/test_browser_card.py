"""Browser pages drawn on the native engine's card (app/browser_quick.py): the helper's side, without a card.
The card itself is checked on Windows in tests/test_native_page_texture.py."""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from sprigcast.app import browser_helper, browser_quick  # noqa: E402
from sprigcast.app.browser_helper import FrameSlot, RemoteHost  # noqa: E402
from sprigcast.sources import browser  # noqa: E402


def test_a_picture_on_the_card_says_so_in_the_block_and_isnt_read_from_memory():
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 4 * 4)
    try:
        assert not slot.on_card()
        slot.mark_on_card(4, 4, 3)
        assert slot.on_card() and slot.painted()  # a new picture (the first paint counts)
        assert slot.read() is None  # nothing in memory to take
        slot.write(np.full((4, 4, 4), 9, np.uint8))  # SprigCast asked for memory pictures: back in a slot
        assert not slot.on_card() and slot.read()[0, 0, 0] == 9
    finally:
        slot.close()


def _remote():
    host = RemoteHost(command=["true"])
    sent = []
    host._send = sent.append
    return host, sent


def test_python_reading_a_page_on_the_card_asks_for_memory_pictures_now_and_then(monkeypatch):
    host, sent = _remote()
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 4 * 4)
    try:
        host._frames["b"] = slot
        assert host.take("b") is None and sent == []  # nothing drawn yet: nothing to ask
        slot.mark_on_card(4, 4, 1)
        clock = [100.0]
        monkeypatch.setattr(browser_helper.time, "monotonic", lambda: clock[0])
        assert host.take("b") is None and sent == [{"op": "memory", "id": "b"}]
        host.take("b")
        assert len(sent) == 1  # not on every frame
        clock[0] += browser_helper.MEMORY_ASK_S
        host.take("b")
        assert len(sent) == 2
    finally:
        host._frames.clear()
        slot.close()
        host.shutdown()


def test_the_engines_card_goes_to_the_helper_it_starts(monkeypatch):
    host, _sent = _remote()
    started = []

    class Proc:
        pid, stdin, stdout = 1, None, None

        def poll(self):
            return None

    monkeypatch.setattr(browser_helper.subprocess, "Popen", lambda *a, **k: started.append(k["env"]) or Proc())
    monkeypatch.setattr(browser_helper.threading.Thread, "start", lambda self: None)  # no pipe reader
    monkeypatch.setenv(browser_helper.CARD_ENV, "stale")
    try:
        assert host._ensure()
        assert browser_helper.CARD_ENV not in started[0]  # no engine: pages in memory
        host.proc = None
        host.use_card((1234, -5))  # nothing running: only remembered
        assert host._ensure() and started[1][browser_helper.CARD_ENV] == "1234,-5"
        host.use_card((1234, -5))  # the same card again: nothing to do
        assert len(started) == 2
    finally:
        host.proc = None
        host.shutdown()


def test_use_card_reaches_the_host_and_a_host_without_it_is_fine(monkeypatch):
    seen = []
    monkeypatch.setattr(browser, "_host", SimpleNamespace(use_card=seen.append))
    browser.use_card((1, 2))
    assert seen == [(1, 2)]
    monkeypatch.setattr(browser, "_host", SimpleNamespace())
    browser.use_card((1, 2))  # an in-process host draws in memory: nothing happens
    monkeypatch.setattr(browser, "_host", SimpleNamespace(use_card=lambda luid: 1 / 0))
    browser.use_card((1, 2))  # pages stay in memory; nothing breaks


def test_the_card_needs_a_card_in_the_environment(monkeypatch):
    monkeypatch.delenv(browser_helper.CARD_ENV, raising=False)
    assert browser_helper._card_from_env() is None
    monkeypatch.setenv(browser_helper.CARD_ENV, "12,-3")
    monkeypatch.setattr(browser_quick, "available", lambda: False)  # not Windows, or Qt Quick's WebEngine missing
    assert browser_helper._card_from_env() is None
    monkeypatch.setattr(browser_quick, "available", lambda: True)
    monkeypatch.setattr(browser_quick, "prepare", lambda: None)
    assert browser_helper._card_from_env() == (12, -3)
    monkeypatch.setenv(browser_helper.CARD_ENV, "nonsense")
    assert browser_helper._card_from_env() is None


def test_a_page_that_cant_be_drawn_on_the_card_is_a_widget_page(monkeypatch):
    from sprigcast.app import browser_host

    host = browser_host.BrowserHost.__new__(browser_host.BrowserHost)
    host.card, host.target_for = (1, 2), lambda *a: None
    made = []

    def broken(*a):
        raise RuntimeError("Qt Quick couldn't start on the engine's card")

    monkeypatch.setattr(browser_quick, "QuickPage", broken)
    monkeypatch.setattr(browser_host, "_Page", lambda h, source_id, settings: made.append(source_id) or "widget")
    assert host._new_page("b", {}) == "widget" and made == ["b"]
    host.card = None
    monkeypatch.setattr(browser_quick, "QuickPage", lambda *a: "card")
    assert host._new_page("c", {}) == "widget"  # no card given: widget pages, as before


class Target:
    def __init__(self, ready=True):
        self.ready, self.size, self.device = ready, (4, 4), 77
        self.calls = []

    def begin(self, timeout_ms=0):
        self.calls.append("begin")
        return self.ready

    def read(self):
        self.calls.append("read")
        return np.zeros((4, 4, 4), np.uint8)

    def end(self, publish=True):
        self.calls.append(("end", publish))


def _page(target):
    page = browser_quick.QuickPage.__new__(browser_quick.QuickPage)
    host = SimpleNamespace(drawn=[], stored=[])
    host.card_drawn = lambda source_id, t: host.drawn.append(source_id)
    host.store = lambda source_id, frame: host.stored.append(source_id)
    page.host, page.id, page.target = host, "b", target
    page.paused, page.dirty, page.on_card, page._initialized = False, True, True, True
    page.memory_until, page._waiting_since, page.interacting = 0.0, None, None
    page.control = SimpleNamespace(**{n: (lambda: None) for n in ("polishItems", "beginFrame", "sync", "render",
                                                                  "endFrame")})
    return page, host


def test_a_page_on_the_card_publishes_its_picture_and_reads_back_only_when_asked(monkeypatch):
    target = Target()
    page, host = _page(target)
    page._tick()
    assert ("end", True) in target.calls and "read" not in target.calls and host.drawn == ["b"]
    target.calls.clear()
    page.want_memory()  # SprigCast draws it itself for a while: memory, and the engine isn't handed it
    page._tick()
    assert "read" in target.calls and ("end", False) in target.calls and host.stored == ["b"]
    assert host.drawn == ["b"]
    target.calls.clear()
    page._tick()
    assert target.calls == []  # nothing changed on the page: nothing drawn


def test_a_page_the_engine_stops_taking_gets_a_new_texture(monkeypatch):
    target = Target(ready=False)
    page, _host = _page(target)
    clock = [10.0]
    monkeypatch.setattr(browser_quick.time, "monotonic", lambda: clock[0])
    resized = []
    page._resize = lambda w, h: resized.append((w, h))
    page._tick()
    clock[0] += browser_quick.STUCK_S / 2
    page._tick()
    assert resized == []
    clock[0] += browser_quick.STUCK_S
    page._tick()
    assert resized == [(4, 4)]
