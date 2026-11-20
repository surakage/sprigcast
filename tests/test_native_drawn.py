"""Store plugins' animations handed to the engine as lists of pictures (core/drawn.py): the list paints what the
plugin painted before, its geometry is what the engine draws, art goes to the card once, and on Windows the engine
draws a list (Snap's polaroid, the gravestone) like Python paints it."""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from test_store_effects import Host, _dc_setup, _shot, added, app  # noqa: E402, F401

from sprigcast import plugin_hooks, sprigcore  # noqa: E402
from sprigcast.control.native import NativeApi  # noqa: E402
from sprigcast.core import drawn  # noqa: E402
from sprigcast.core.painted import Frame  # noqa: E402
from sprigcast.core.scene import SourceConfig  # noqa: E402
from sprigcast.core.source import create_source  # noqa: E402

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


def _array(image) -> np.ndarray:
    return Frame()(image).astype(int)


def _close(a: np.ndarray, b: np.ndarray, mean: float = 1.0, most_off: float = 0.01) -> None:
    """Mostly the same pixels: edges may differ a little (how a picture's edge is smoothed)."""
    diff = np.abs(a - b).max(axis=2)
    assert diff.mean() <= mean, diff.mean()
    assert (diff > 24).mean() <= most_off, (diff > 24).mean()


def _picture(w: int, h: int, colour=(200, 60, 40, 255)):
    from PySide6.QtGui import QColor, QImage

    image = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor(*colour))
    return image


@pytest.fixture
def snap(added, tmp_path):  # noqa: F811
    added("snap")
    from test_control_api import _collection

    col = _collection()
    col.sources.append(SourceConfig(id="snap-1", kind="snap", name="Snap", settings={}))
    host = Host(col, tmp_path)
    source = host.instances["snap-1"] = create_source("snap", "snap-1", {"seconds": 1.0})
    source.video_frame()
    plugin_hooks.screenshot_taken(NativeApi(host), _shot(tmp_path))
    import time

    deadline = time.monotonic() + 10  # the card is made on a worker (it no longer loads fonts: see the test below)
    while source._job is None or source._job.card is None:
        source.video_frame()
        assert time.monotonic() < deadline
        time.sleep(0.01)
    yield source
    source.clear()


def test_the_list_paints_what_snap_painted(snap):
    job = snap._job
    for t in (0.1, 0.35, 0.6, 1.2, 2.0, 3.6, snap._length() - 0.2):
        listed = snap.paint(job, t, listed=True)
        assert isinstance(listed, drawn.Drawn) and listed.places
        _close(_array(listed.paint()), _array(snap.paint(job, t)))


def test_the_list_paints_what_the_gravestone_painted(added, tmp_path):  # noqa: F811
    _col, _host, dc, _api = _dc_setup(added, tmp_path, 1)
    job = sys.modules["sprigcast_plugin_death_count"]._Job(1, 2)
    for t in (0.2, 0.5, 0.9, job.flip_at + 0.05, job.flip_at + 0.2, job.flip_at + 1.0, dc._sink_at(job) + 0.3):
        listed = dc.paint(job, t, listed=True)
        assert isinstance(listed, drawn.Drawn)
        _close(_array(listed.paint()), _array(dc.paint(job, t)), mean=1.5, most_off=0.02)


def test_a_source_hands_over_a_list_only_while_the_engine_draws(snap, monkeypatch):
    monkeypatch.setattr(drawn, "ENGINE", False)
    snap._still = None
    assert isinstance(snap.video_frame(), np.ndarray)
    monkeypatch.setattr(drawn, "ENGINE", True)
    snap._still = None
    snap._job.started -= 0.5  # moving: a new frame every tick
    assert isinstance(snap.video_frame(), drawn.Drawn)


def test_where_each_picture_goes():
    d = drawn.Drawn(400, 300)
    card = _picture(100, 50)
    d.save()
    d.translate(200, 150)
    d.rotate(30)
    d.scale(2, 1)
    d.opacity(0.5)
    d.image("card", card, -50, -25, 100, 50)
    d.restore()
    d.clip(0, 0, 400, 120)
    d.image("card", card, 10, 100, 100, 50, src=(10, 0, 80, 50))  # half clipped away, and only part of it
    d.fill((0, 0, 0, 255), 0, 200, 10, 10)  # below the clip: left out
    (key, image, x, y, w, h, rot, crop, opacity), (_, _, x2, y2, w2, h2, rot2, crop2, _) = d.items()
    assert key == "card" and image is card and opacity == 0.5
    assert (x + w / 2, y + h / 2) == pytest.approx((200, 150)) and (w, h) == pytest.approx((200, 50))
    assert rot == pytest.approx(30) and crop == (0, 0, 0, 0)
    assert (x2, y2, w2, h2) == pytest.approx((10, 100, 100, 20)) and rot2 == pytest.approx(0)
    assert crop2 == (10, 0, 10, 30)  # 80 x 50 picture pixels shown over 100 x 50: the bottom 30 cut
    assert len(d.items()) == 2
    assert d.opacity() == 1.0  # restored


def test_art_goes_to_the_card_once(monkeypatch):
    from sprigcast.render import native_bridge

    calls = []

    class Engine:
        def upload(self, source_id, frame, order, size, keep=False):
            calls.append(("upload", source_id, frame.shape, order, keep))

        def draws(self, source_id, w, h, items):
            calls.append(("draws", source_id, w, h, items))

        def forget(self, source_id):
            calls.append(("forget", source_id))

    bridge = native_bridge.NativeCanvases.__new__(native_bridge.NativeCanvases)
    bridge.engine, bridge.failed = Engine(), False
    bridge._arts, bridge._last_upload, bridge._media, bridge._lists = {}, {}, {}, 0
    bridge._held = set()
    card = _picture(60, 40)
    for frame in range(3):
        d = drawn.Drawn(200, 100)
        d.image("card", card, 10 + frame, 10, 60, 40)
        bridge.draw_list("s", d)
    uploads = [c for c in calls if c[0] == "upload"]
    assert len(uploads) == 1 and uploads[0][1:] == ("s/art/card", (40, 60, 4), "pBGRA", True)
    draws = [c for c in calls if c[0] == "draws"]
    assert len(draws) == 3 and draws[-1][4][0][:3] == ("s/art/card", 12, 10)
    d = drawn.Drawn(200, 100)
    d.image("card", _picture(60, 40, (1, 2, 3, 255)), 0, 0, 60, 40)  # a new picture under the same key
    bridge.draw_list("s", d)
    assert len([c for c in calls if c[0] == "upload"]) == 2
    monkeypatch.setattr(bridge, "SPARE_ARTS", 2)
    for i in range(4):
        d = drawn.Drawn(200, 100)
        d.image(f"art{i}", card, 0, 0, 60, 40)
        bridge.draw_list("s", d)
    assert set(bridge._arts["s"]) == {"art1", "art2", "art3"} and ("forget", "s/art/card") in calls  # oldest went
    monkeypatch.setattr(bridge, "SPARE_BYTES", 0)
    bridge.draw_list("s", d)
    assert set(bridge._arts["s"]) == {"art3"}
    bridge.forget_gone([])
    assert not bridge._arts and ("forget", "s") in calls


@needs_engine
def test_the_engine_draws_a_list_like_python_paints_it(snap):
    from sprigcast.core.scene import Scene, SceneItem
    from sprigcast.render import native_snapshot

    job = snap._job
    with sprigcore.Device(warp=True) as device:
        for t in (0.35, 1.2, 3.6):
            listed = snap.paint(job, t, listed=True)
            items = []
            for key, image, x, y, w, h, rot, crop, opacity in listed.items():
                device.upload(f"art/{key}", Frame()(image), "pBGRA")
                items.append((f"art/{key}", x, y, w, h, rot, crop, opacity, image.width(), image.height()))
            device.draws("snap-1", listed.width, listed.height, items)
            scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="snap-1", align="top_left")])
            built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (800, 1000))], {})
            device.set_snapshot(built.struct)
            with sprigcore.Canvas(device, 800, 1000) as c:
                c.render("b")
                got = np.frombuffer(c.read_bgra(), np.uint8).reshape(1000, 800, 4).astype(int)
            _close(got, _array(listed.paint()), mean=2.0, most_off=0.02)
        device.draws("snap-1", 0, 0, None)  # dropped: nothing drawn for it
        with sprigcore.Canvas(device, 800, 1000) as c:
            c.render("b")
            assert not np.frombuffer(c.read_bgra(), np.uint8).any()


@needs_engine
def test_the_engine_takes_lists_through_the_bridge(snap):
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig as Config
    from sprigcast.render import native_bridge

    col = SceneCollection(
        canvases=[Canvas(id="h", width=800, height=1000)],
        sources=[Config(id="snap-1", kind="snap", name="Snap", settings={})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="snap-1", x=0, y=0, align="top_left")])],
        active_scenes={"h": "a"},
    )
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        assert drawn.wanted()
        listed = snap.paint(snap._job, 3.6, listed=True)
        bridge.draw_list("snap-1", listed)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (800, 1000), 0)
        import time

        deadline, got = time.monotonic() + 5, None
        while time.monotonic() < deadline:
            taken = bridge.take("h")
            if taken is not None:
                got = np.frombuffer(taken[0], np.uint8).reshape(1000, 800, 4)
                if got[500, 400, 3] > 200:
                    break
            time.sleep(0.02)
        assert got is not None and got[500, 400, 3] > 200 and not bridge.failed  # the polaroid, drawn by the engine
    finally:
        bridge.close()
    assert not drawn.wanted()
    assert math.isfinite(listed.nbytes)


def test_the_polaroid_card_needs_no_fonts(added):  # noqa: F811
    """The CI flake behind the snap fixture's wait: drawing the print as text made Qt load the PC's fonts on the
    worker first (over 3 s on a cold Windows runner, and as late a first polaroid on a cold PC). The print is
    outlines now, so making a card never touches fonts, and Pillow is imported ahead of the first screenshot."""
    added("snap")
    snap = sys.modules["sprigcast_plugin_snap"]
    svg = snap.frame_svg((900, 506)).decode()
    assert "<text" not in svg and "font" not in svg
    assert snap.print_path().startswith("M") and len(snap.print_path()) > 1000 and snap.print_path() in svg
    assert snap.print_path() not in snap.frame_svg((300, 900)).decode()  # too narrow for the print: the mark only
    create_source("snap", "snap-2", {})
    assert snap._WARM.is_set()
