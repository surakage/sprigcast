"""Bloom switches back to sleeping sources stay light (PC heavy run, 2026-11-18)."""

import threading
import time

import numpy as np

from sprigcast.core import painted
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig


def test_clock_cards_are_painted_on_the_pictures_lane(monkeypatch):
    monkeypatch.setattr(painted, "DRAW_ASIDE", True)
    names = []

    class Image:
        pass

    frame = painted.Frame()
    monkeypatch.setattr(painted.Frame, "__call__", lambda self, image: np.zeros((2, 2, 4), np.uint8))
    card = painted.Aside(frame)

    def paint(text):
        names.append(threading.current_thread().name)
        return Image()

    assert card.picture(("12:00",), paint, "12:00") is None  # painting: the old card stays a moment
    deadline = time.monotonic() + 5
    got = None
    while got is None and time.monotonic() < deadline:
        time.sleep(0.005)
        got = card.picture(("12:00",), paint, "12:00")
    assert got is not None and names == ["sprigcast-pictures_0"]
    assert card.picture(("12:00",), paint, "12:00") is None  # nothing changed: nothing painted
    card.forget()
    assert card.picture(("12:00",), paint, "12:00") is None and card._job is not None  # a new look: again
    card.drop()
    assert card._job is None


def test_the_pngtuber_folder_is_read_again_off_the_window_thread(monkeypatch, tmp_path):
    from sprigcast.store import pngtuber

    pngtuber._SCANS.clear()
    (tmp_path / "idle.png").write_bytes(b"")
    reads = []
    real = pngtuber.scan
    monkeypatch.setattr(pngtuber, "scan", lambda *a: reads.append(threading.current_thread().name) or real(*a))
    settings = {"folder": str(tmp_path)}
    pngtuber.expressions_in(settings)
    assert reads == [threading.current_thread().name]  # the very first time only
    key = pngtuber.model_key(settings)
    stamp, found = pngtuber._SCANS[key]
    pngtuber._SCANS[key] = (stamp - 60, found)  # old now
    assert pngtuber.expressions_in(settings) == pngtuber.expression_order(found)  # answered from what it had
    deadline = time.monotonic() + 5
    while len(reads) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert reads[1] != threading.current_thread().name


def test_on_air_walks_each_sprig_once():
    from sprigcast.plugin_hooks import on_air, on_air_sources

    sprig = Scene(id="p", name="Sprig", kind="part", canvas="h", items=[SceneItem(source="cam")])
    col = SceneCollection(
        canvases=[Canvas(id="h", name="H", width=64, height=36)],
        sources=[SourceConfig(id="cam", kind="color", name="Cam"),
                 SourceConfig(id="s1", kind="scene", name="S1", settings={"scene": "p"}),
                 SourceConfig(id="s2", kind="scene", name="S2", settings={"scene": "p"}),
                 SourceConfig(id="off", kind="color", name="Off")],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="s1"), SceneItem(source="s2"),
                                                            SceneItem(source="off", visible=False)]), sprig],
        active_scenes={"h": "a"})
    assert on_air_sources(col) == {"s1", "s2", "cam"}
    assert on_air(col, "cam") and not on_air(col, "off")


def test_a_slideshow_keeps_its_slide_while_asleep():
    from sprigcast.store.slide_show import SlideShowSource

    assert SlideShowSource.keeps_picture
