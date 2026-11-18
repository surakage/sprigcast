"""Now playing handed to the engine as a list of pictures (store/now_playing.frame_listed): the list paints what
frame() painted, only its time part is painted (and only when it changes), and the source hands lists over only
while the engine draws."""

from __future__ import annotations

import time

import numpy as np
import pytest
from test_now_playing import _png, fake, np_mod  # noqa: F401
from test_store_effects import added, app  # noqa: F401

from sprigcast.core import drawn
from sprigcast.core.painted import Frame

LONG = 'Lose Yourself (From "8 Mile" Soundtrack) - Remastered 2022 Extended Version'


def _array(image) -> np.ndarray:
    return Frame()(image).astype(int)


def _close(a, b, mean=1.0, most_off=0.01) -> None:
    diff = np.abs(a - b).max(axis=2)
    assert diff.mean() <= mean, diff.mean()
    assert (diff > 24).mean() <= most_off, (diff > 24).mean()


@pytest.mark.parametrize("style", ["band", "canopy", "glow", "ring"])
@pytest.mark.parametrize("title", ["Song", LONG])
def test_the_list_paints_what_the_card_painted(np_mod, style, title):  # noqa: F811
    settings = {**np_mod.NowPlayingSource.defaults, "style": style}
    track = np_mod.Track(title, "Eminem", art=_png(), length=200.0, position=50.0)
    bands = np.linspace(0.0, 1.0, 64, dtype=np.float32)
    for detail in (1.0, 1.5):
        base, lay, words = np_mod.still(track, settings, detail, True)
        for t in (0.0, np_mod.SLIDE_REST + 0.7, np_mod.SLIDE_REST + 4.3):
            for position in (0.0, 50.4, 199.0):
                painted = np_mod.frame(base, lay, words, settings, detail, t, position, track.length, bands)
                listed = np_mod.frame_listed(base, lay, words, settings, detail, t, position, track.length, bands)
                assert isinstance(listed, drawn.Drawn) and (listed.width, listed.height) == (painted.width(),
                                                                                             painted.height())
                _close(_array(listed.paint()), _array(painted), mean=1.2, most_off=0.01)


def test_only_the_time_is_painted_and_only_when_it_changes(np_mod):  # noqa: F811
    settings = {**np_mod.NowPlayingSource.defaults, "style": "canopy"}
    track = np_mod.Track(LONG, "Eminem", length=200.0, position=50.0)
    base, lay, words = np_mod.still(track, settings, 1.0, True)
    memo = {}
    first = np_mod.frame_listed(base, lay, words, settings, 1.0, 3.0, 50.0, 200.0, None, memo)
    again = np_mod.frame_listed(base, lay, words, settings, 1.0, 3.1, 50.01, 200.0, None, memo)
    time_of = lambda d: [p.image for p in d.places if p.key == "time"]  # noqa: E731
    assert time_of(first)[0] is time_of(again)[0]  # the same second, the bar not half a pixel on: not painted again
    later = np_mod.frame_listed(base, lay, words, settings, 1.0, 3.2, 51.0, 200.0, None, memo)
    assert time_of(later)[0] is not time_of(first)[0]
    part = time_of(first)[0]
    assert part.width() * part.height() < np_mod.W * np_mod.H / 4  # a strip, not the card
    assert len(first.items()) < 600


def test_the_source_hands_over_a_list_only_while_the_engine_draws(fake, monkeypatch):  # noqa: F811
    from test_now_playing import FakeWatcher

    source = fake.NowPlayingSource("np-1", {"sound_bars": False})
    source.start()
    FakeWatcher.made[-1].on_track(fake.Track(LONG, "Band", app="Spotify.exe", length=200.0, position=30.0))
    time.sleep(fake.SETTLE + 0.05)
    source._fade = (1.0, 1.0, 0.0)  # faded in already
    monkeypatch.setattr(drawn, "ENGINE", True)
    got = None
    for _ in range(10):
        source._last = 0.0
        got = source.video_frame() or got
    assert isinstance(got, drawn.Drawn)
    monkeypatch.setattr(drawn, "ENGINE", False)
    source._last, source._shown = 0.0, None
    assert isinstance(source.video_frame(), np.ndarray)
    source.stop()


@pytest.mark.skipif(not __import__("sprigcast.sprigcore", fromlist=["x"]).available(),
                    reason="sprigcore.dll isn't built here")
def test_the_engine_draws_the_card_like_python_paints_it(np_mod):  # noqa: F811
    from sprigcast import sprigcore
    from sprigcast.core.scene import Scene, SceneItem
    from sprigcast.render import native_snapshot

    with sprigcore.Device(warp=True) as device:
        for style in ("glow", "ring"):
            settings = {**np_mod.NowPlayingSource.defaults, "style": style}
            track = np_mod.Track(LONG, "Eminem", art=_png(), length=200.0, position=50.0)
            base, lay, words = np_mod.still(track, settings, 1.0, True)
            listed = np_mod.frame_listed(base, lay, words, settings, 1.0, np_mod.SLIDE_REST + 1.3, 80.0, 200.0,
                                         np.linspace(0.0, 1.0, 64, dtype=np.float32))
            items = []
            for key, image, x, y, w, h, rot, crop, opacity in listed.items():
                device.upload(f"art/{key}", Frame()(image), "pBGRA")
                items.append((f"art/{key}", x, y, w, h, rot, crop, opacity, image.width(), image.height()))
            device.draws("np-1", listed.width, listed.height, items)
            scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="np-1", align="top_left")])
            built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (listed.width, listed.height))], {})
            device.set_snapshot(built.struct)
            with sprigcore.Canvas(device, listed.width, listed.height) as c:
                c.render("b")
                got = np.frombuffer(c.read_bgra(), np.uint8).reshape(listed.height, listed.width, 4).astype(int)
            _close(got, _array(listed.paint()), mean=2.0, most_off=0.02)
