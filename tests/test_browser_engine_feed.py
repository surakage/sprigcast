"""Browser sources on the native engine: the engine reads the helper's shared block itself; Python only notes the
first paint and the sound status (sources/browser.py engine_player / engine_tick)."""

from __future__ import annotations

import uuid

import numpy as np

from sprigcast.app.browser_helper import FrameSlot
from sprigcast.sources import browser


def test_painted_says_each_new_picture_once_without_taking_it():
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 4 * 4)
    try:
        assert not slot.painted()
        slot.write(np.zeros((4, 4, 4), np.uint8))
        assert slot.painted() and not slot.painted()
        assert slot.read() is not None  # still there for Python to take
        slot.write(np.zeros((4, 4, 4), np.uint8))
        assert slot.painted()
    finally:
        slot.close()


class Host:
    def __init__(self):
        self.name, self.paints, self.took = "sprigcast-9-1", [], 0

    def open(self, source_id, settings):
        pass

    def close(self, source_id):
        pass

    def shared_name(self, source_id):
        return self.name

    def painted(self, source_id):
        return self.paints.pop(0) if self.paints else False

    def sound_note(self, source_id):
        return ""

    def take(self, source_id):
        self.took += 1
        return None


def test_a_page_in_the_helper_is_shown_by_the_engine_and_its_first_paint_still_counts(monkeypatch):
    host = Host()
    monkeypatch.setattr(browser, "_host", host)
    source = browser.BrowserSource("b", {**browser.BrowserSource.defaults, "url": "https://example.com"})
    assert source.engine_player is None  # not open yet
    source.start()
    assert source.engine_player == "sprigcast-9-1"
    source.engine_tick()
    assert not source.first_picture and host.took == 0  # nothing drawn yet, and nothing copied
    host.paints.append(True)
    source.engine_tick()
    assert source.first_picture and host.took == 0
    source.stop()
    assert source.engine_player is None


def test_a_page_drawn_in_this_process_has_no_block(monkeypatch):
    class InProcess(Host):
        shared_name = None

    monkeypatch.setattr(browser, "_host", InProcess())
    source = browser.BrowserSource("b", {**browser.BrowserSource.defaults, "url": "https://example.com"})
    source.start()
    assert source.engine_player is None
