"""PNGtuber handed to the engine as a list of pictures (store/pngtuber.PngtuberSource._listed): the list paints what
the buffer showed, a bounce or a held mouth sends no new pictures, and a fade sends only the part that blends."""

from __future__ import annotations

import numpy as np
import pytest
from test_pngtuber import _model, _picture, _ready, tuber  # noqa: F401

from sprigcast.core import drawn
from sprigcast.core.painted import Frame


def _both(source, clock, seconds, step=1 / 60):
    """Each frame both ways: (the buffer's frame, the list), stepping one clock for two sources."""
    painted, listed = source
    out = []
    end = clock.now + seconds
    while clock.now < end:
        clock.now += step
        a = painted.video_frame()
        drawn.ENGINE = True
        try:
            b = listed.video_frame()
        finally:
            drawn.ENGINE = False
        assert (a is None) == (b is None)
        if a is not None:
            out.append((a.copy(), b))
    return out


def _pair(tuber, monkeypatch, folder, **settings):  # noqa: F811
    painted, clock, level = _ready(tuber, monkeypatch, folder, **settings)
    listed, _clock, _level = _ready(tuber, monkeypatch, folder, **settings)
    monkeypatch.setattr(tuber.time, "monotonic", clock)
    monkeypatch.setattr(tuber, "_sound_level", lambda sid: level[sid])
    listed._model = painted._model  # the same pictures (and the same diffs)
    return (painted, listed), clock, level


def _check(frames) -> None:
    for frame, listed in frames:
        assert isinstance(listed, drawn.Drawn)
        got = Frame()(listed.paint()).astype(int)
        assert np.abs(got - frame.astype(int)).max() <= 1


def test_the_list_paints_what_the_buffer_showed(tuber, monkeypatch, tmp_path):  # noqa: F811
    folder = _model(tmp_path / "m")
    _picture(folder / "happy_idle.png", (90, 200, 120, 255), size=(80, 140))  # another shape: a whole fade
    _picture(folder / "happy_talk.png", (90, 200, 120, 255), size=(80, 140), mouth=(20, 20, 60, 255))
    pair, clock, level = _pair(tuber, monkeypatch, folder)
    frames = _both(pair, clock, 0.1)
    level["mic"] = 0.3
    frames += _both(pair, clock, 0.4)  # the mouth fades open, a bounce
    level["mic"] = 0.0
    frames += _both(pair, clock, 0.6)
    for source in pair:
        source.set_expression("happy")
    frames += _both(pair, clock, 0.4)
    assert len(frames) > 20
    kinds = {tuple(sorted({"crop" if p.src else p.key.split(":")[0] for p in d.places})) for _f, d in frames}
    assert ("crop", "fade") in kinds and ("fade",) in kinds and ("pic",) in kinds  # mouth fade, whole fade, still
    _check(frames)


def test_a_bounce_sends_no_new_pictures_and_a_fade_only_the_mouth(tuber, monkeypatch, tmp_path):  # noqa: F811
    pair, clock, level = _pair(tuber, monkeypatch, _model(tmp_path / "m"))
    _both(pair, clock, 0.1)
    level["mic"] = 0.3
    frames = _both(pair, clock, 0.4)
    arts = {p.key for _f, d in frames for p in d.places}
    assert {k for k in arts if k.startswith("pic:")} and len(arts) <= 3  # quiet, talking and the fade
    images = {id(p.image) for _f, d in frames for p in d.places if p.key != "fade"}
    assert len(images) <= 2  # the same two pictures every frame: sent to the card once each
    fades = [p for _f, d in frames for p in d.places if p.key == "fade"]
    whole = [p.w * p.h for _f, d in frames for p in d.places if p.key != "fade" and p.src is None]
    assert fades and all(p.w * p.h < max(whole) / 10 for p in fades)  # just the mouth
    still = [d for _f, d in frames[-5:]]
    assert all(len(d.places) == 1 for d in still)


@pytest.mark.skipif(not __import__("sprigcast.sprigcore", fromlist=["x"]).available(),
                    reason="sprigcore.dll isn't built here")
def test_the_engine_draws_the_tuber_like_the_buffer(tuber, monkeypatch, tmp_path):  # noqa: F811
    from sprigcast import sprigcore
    from sprigcast.core.scene import Scene, SceneItem
    from sprigcast.render import native_snapshot

    pair, clock, level = _pair(tuber, monkeypatch, _model(tmp_path / "m"))
    _both(pair, clock, 0.1)
    level["mic"] = 0.3
    frames = _both(pair, clock, 0.3)
    picked = [f for f in frames if any(p.key == "fade" for p in f[1].places)][:1] + frames[-1:]
    with sprigcore.Device(warp=True) as device:
        for frame, listed in picked:
            items = []
            for key, image, x, y, w, h, rot, crop, opacity in listed.items():
                device.upload(f"art/{key}", Frame()(image), "pBGRA")
                items.append((f"art/{key}", x, y, w, h, rot, crop, opacity, image.width(), image.height()))
            device.draws("tuber", listed.width, listed.height, items)
            scene = Scene(id="b", name="b", canvas="h", items=[SceneItem(source="tuber", align="top_left")])
            built = native_snapshot.build([native_snapshot.SceneDraw("b", scene, (listed.width, listed.height))], {})
            device.set_snapshot(built.struct)
            with sprigcore.Canvas(device, listed.width, listed.height) as c:
                c.render("b")
                got = np.frombuffer(c.read_bgra(), np.uint8).reshape(listed.height, listed.width, 4).astype(int)
            assert np.abs(got - frame.astype(int)).max(axis=2).mean() <= 0.5
            assert (np.abs(got - frame.astype(int)).max(axis=2) > 8).mean() <= 0.002
