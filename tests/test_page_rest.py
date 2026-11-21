"""Pages that grow are named in the log, and a page kept running rests after ten minutes off screen.

PC 2026-11-21, a 65 minute stream: the page helper went from 1.76 GB to 2.46 GB, one page process by 11 MB a
minute, with 18 pages running on off screen, and nothing said which page it was."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import browser_host, page_rest
from sprigcast.app.page_rest import PageSizes


def test_a_growing_page_is_named_at_once_and_the_rest_every_third_round():
    sizes = PageSizes()
    for page_id, text in (("browser-1", "400,90000000,1,0"), ("browser-2", "120,90000000,0,2"),
                          ("browser-3", "60,31000000,0,0")):
        sizes.feed(page_id, text, 0.0)
    # The first round is said, so a short run's log has the line (PC test 2026-11-21: none in 8 minutes).
    assert sizes.line().startswith("pages: 3 asked; none growing; script memory 90 MB for 2 pages")
    sizes.feed("browser-1", "4300,270000000,9,0", 3600.0)
    sizes.feed("browser-2", "130,270000000,0,2", 3600.0)
    sizes.feed("browser-3", "", 3600.0)  # couldn't say this time: its last answer stays
    line = sizes.line(resting=4)
    assert line == ("pages: 3 asked, 4 resting; browser-1 4,300 elements (+3,900 in 60 min), 9 videos and sounds (+8); "
                    "script memory 270 MB for 2 pages (+180 MB in 60 min); script memory 31 MB for 1 page")
    assert "http" not in line  # by source id only
    assert sizes.line().startswith("pages: 3 asked; browser-1 ")  # the third round: said whatever happened
    assert sizes.line() is None  # the same page still growing: not said again until the next third round
    assert sizes.line() is None
    assert sizes.line().startswith("pages: 3 asked; browser-1 ")
    # Loaded again (far fewer elements): counted from there, no longer growing.
    sizes.feed("browser-1", "410,95000000,1,0", 4000.0)
    assert not sizes.grew("browser-1")
    sizes.forget("browser-2")
    assert "browser-2" not in sizes.last and "browser-2" not in sizes.first


def test_a_quiet_round_says_none_growing():
    sizes = PageSizes()
    sizes.feed("browser-1", "400,0,0,0", 0.0)
    sizes.rounds = page_rest.LOG_EVERY - 1
    assert sizes.line() == "pages: 1 asked; none growing"


class FakePage:
    def __init__(self):
        self.id, self.settings, self.paused, self.resting = "browser-1", {}, True, False
        self.sound = self.interacting = None
        self.states, self.pushed, self.audible = [], 0, False

    def _lifecycle(self, state):
        self.states.append(state)

    def _audible(self):
        return self.audible

    def push_state(self):
        self.pushed += 1

    def _rest_if_still_hidden(self):
        browser_host.rest_if_still_hidden(self)


@pytest.fixture
def timers(monkeypatch):
    armed = []
    monkeypatch.setattr(browser_host.QTimer, "singleShot", lambda ms, job: armed.append((ms, job)))
    return armed


def test_a_page_hidden_ten_minutes_rests_and_wakes_when_shown(timers, monkeypatch, caplog):
    import logging
    import time

    page = FakePage()
    browser_host.arm_rest(page)
    assert timers[-1][0] == page_rest.REST_AFTER_S * 1000 == 600_000
    timers[-1][1]()  # too early (hidden again since): nothing
    assert not page.resting and page.states == []
    page._hidden_at = time.monotonic() - page_rest.REST_AFTER_S
    with caplog.at_level(logging.INFO):
        timers[-1][1]()
    assert page.resting and page.states == [page_rest.FROZEN]
    assert "browser-1: off screen 10 min, resting until it is shown again" in caplog.text
    browser_host.rest_if_still_hidden(page)  # already resting: not twice
    assert page.states == [page_rest.FROZEN]
    page.paused = False
    browser_host.unrest(page)
    assert not page.resting and page.states == [page_rest.FROZEN, page_rest.ACTIVE] and page.pushed == 1
    browser_host.unrest(page)  # awake already
    assert page.pushed == 1


def test_pages_that_must_go_on_never_rest(timers, monkeypatch):
    import time

    def hidden_long() -> FakePage:
        page = FakePage()
        page._hidden_at = time.monotonic() - page_rest.REST_AFTER_S
        return page

    page = hidden_long()
    page.sound = object()  # its sound goes to the mixer: heard whatever is on screen
    browser_host.rest_if_still_hidden(page)
    page2 = hidden_long()
    page2.interacting = object()  # its window for clicking in it is open
    browser_host.rest_if_still_hidden(page2)
    page3 = hidden_long()
    page3.settings = {"never_rest": True}
    browser_host.rest_if_still_hidden(page3)
    page4 = hidden_long()
    page4.paused = False  # shown again in the meantime
    browser_host.rest_if_still_hidden(page4)
    assert not any(p.resting or p.states for p in (page, page2, page3, page4))
    page5 = hidden_long()
    page5.audible = True  # still making sound: looked at again in a minute
    browser_host.rest_if_still_hidden(page5)
    assert not page5.resting and timers[-1][0] == page_rest.AUDIBLE_RETRY_S * 1000
    page5.audible = False
    timers[-1][1]()
    assert page5.resting
    monkeypatch.setenv("SPRIGCAST_KEEP_PAGES_AWAKE", "1")
    page6 = hidden_long()
    browser_host.rest_if_still_hidden(page6)
    assert not page6.resting


def test_a_resting_page_given_new_settings_or_loaded_again_wakes_and_counts_from_there(timers):
    page = FakePage()
    page.resting = True
    browser_host.stir(page)
    assert not page.resting and page.states == [page_rest.ACTIVE]
    assert timers and timers[-1][0] == page_rest.REST_AFTER_S * 1000  # still hidden: its rest is counted again
    count = len(timers)
    browser_host.stir(page)  # awake: nothing to do
    assert len(timers) == count


def test_both_kinds_of_page_rest_the_same_way():
    from sprigcast.app import browser_quick

    for kind in (browser_host._Page, browser_quick.QuickPage):
        assert all(hasattr(kind, name) for name in ("measure", "unrest", "_rest_if_still_hidden", "_lifecycle",
                                                    "_audible")) and kind.resting is False


REAL = """
import sys
from PySide6.QtWebEngineQuick import QtWebEngineQuick, QQuickWebEngineProfile
QtWebEngineQuick.initialize()
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
app = QGuiApplication(sys.argv)
from sprigcast.app import browser_quick, page_rest
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(browser_quick.QML, QUrl())
root = component.create()
assert root is not None, [e.toString() for e in component.errors()]
window = QQuickWindow()
root.setParentItem(window.contentItem())
root.setWidth(64)
root.setHeight(64)
profile = QQuickWebEngineProfile("sprigcast-test")
root.setProperty("profile", profile)
from PySide6.QtCore import Q_ARG, QMetaObject, Qt
def call(name, *args):
    QMetaObject.invokeMethod(root, name, Qt.ConnectionType.DirectConnection, *(Q_ARG("QVariant", a) for a in args))
got = []
root.measured.connect(got.append)
COUNT = "String(window.n)"
def step1():
    call("measure", page_rest.MEASURE_JS)
    call("measure", COUNT)
    root.setProperty("pageVisible", False)
    QTimer.singleShot(300, step2)
def step2():
    call("rest", True)
    got.append("resting %s" % root.property("resting"))
    got.append("audible %s" % root.property("audible"))
    QTimer.singleShot(1500, step3)
def step3():
    call("rest", False)
    root.setProperty("pageVisible", True)
    got.append("resting %s" % root.property("resting"))
    QTimer.singleShot(100, lambda: (call("measure", COUNT), QTimer.singleShot(1200, lambda: (call("measure", COUNT),
                                    QTimer.singleShot(300, app.quit)))))
root.loaded.connect(lambda ok: QTimer.singleShot(1000, step1))
root.setProperty("url", QUrl("data:text/html,<p>a</p><p>b</p><script>window.n=0;setInterval(()=>{window.n++},50)</script>"))
QTimer.singleShot(30000, app.quit)
app.exec()
print(repr(got))
"""


@pytest.mark.skipif(importlib.util.find_spec("PySide6.QtWebEngineQuick") is None, reason="needs Qt WebEngine for Qt Quick")
def test_a_real_page_says_what_it_holds_stops_while_resting_and_goes_on_after():
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen",
           "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox --enable-precise-memory-info",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    out = subprocess.run([sys.executable, "-c", REAL], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert out.stdout.strip(), out.stderr[-2000:]
    got = eval(out.stdout.strip().splitlines()[-1])  # noqa: S307 (our own script's list of strings)
    assert len(got) == 7, (got, out.stderr[-1500:])
    held, before, frozen, audible, active, after, later = got
    elements, heap, media, frames = (int(v) for v in held.split(","))
    assert elements >= 5 and heap > 0 and (media, frames) == (0, 0)
    assert (frozen, active, audible) == ("resting True", "resting False", "audible False")
    # 1.9 s passed between the two counts, 1.5 s of it at rest: the page's timer (20 a second) hardly ran...
    assert int(after) - int(before) <= 16, got
    assert int(later) - int(after) >= 10, got  # ...and runs on afterwards
