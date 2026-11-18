"""A graphics driver reset (a driver update, a game crashing the card, Windows' hang recovery) loses every texture
on the card. Like OBS rebuilding its device, SprigCast starts its graphics again and has every source send its
picture afresh, instead of showing a black canvas until it restarts."""

import time

import pytest

from sprigcast.core.events import EngineError
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.render import compositor as compositor_module
from sprigcast.render import engine as engine_module
from sprigcast.render.engine import Engine


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36, fps=60)],
        sources=[SourceConfig(id="bg", kind="color", name="Background", settings={"color": "#336699"})],
        scenes=[Scene(id="a", name="Main", canvas="h", items=[SceneItem(source="bg")])],
        active_scenes={"h": "a"},
    )


def _until(check, seconds=8.0):
    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.02)
    return check()


@pytest.fixture
def made(monkeypatch):
    real = compositor_module.Compositor
    try:
        real().release()
    except Exception as exc:  # no OpenGL here
        pytest.skip(f"no OpenGL: {exc}")
    made = []

    def counting(*a, **k):
        made.append(real(*a, **k))
        return made[-1]

    monkeypatch.setattr(compositor_module, "Compositor", counting)
    return made


def _drain(engine):
    out = []
    while not engine.events.empty():
        out.append(engine.events.get_nowait())
    return out


def test_frames_failing_in_a_row_start_the_graphics_again(made, monkeypatch):
    engine = Engine(_collection())
    real_tick = engine._tick
    lost = {"on": False}

    def tick(compositor, now):
        if lost["on"] and compositor is made[0]:  # only the old card's work is lost
            raise RuntimeError("GL_CONTEXT_LOST")
        real_tick(compositor, now)

    monkeypatch.setattr(engine, "_tick", tick)
    engine.start()
    try:
        assert _until(lambda: len(made) == 1 and "bg" in engine._sources)
        first_source = engine._sources["bg"]
        lost["on"] = True  # the driver reset: every frame fails from here
        assert _until(lambda: len(made) >= 2)
        assert _until(lambda: engine._sources.get("bg") is not None and engine._sources["bg"] is not first_source)
        assert _until(lambda: "bg" in made[-1].textures)  # the colour sent its picture to the new card
        said = []
        assert _until(lambda: said.extend(e.message for e in _drain(engine) if isinstance(e, EngineError))
                      or any("graphics card restarted" in m for m in said))
        assert len([m for m in said if m.startswith("A frame failed")]) == 1  # said once, not every tick
    finally:
        engine.stop()


def test_the_drivers_own_reset_signal_starts_it_again_at_once(made, monkeypatch):
    engine = Engine(_collection())
    signalled = {"reset": False}
    monkeypatch.setattr(Engine, "_graphics_reset", staticmethod(lambda compositor: signalled["reset"]))
    engine.start()
    try:
        assert _until(lambda: len(made) == 1 and "bg" in made[0].textures)
        signalled["reset"] = True
        assert _until(lambda: len(made) >= 2)
        signalled["reset"] = False
        assert _until(lambda: "bg" in made[-1].textures)
        time.sleep(1.5)
        assert len(made) == 2  # once the driver says it's fine, no more restarts
    finally:
        engine.stop()


def test_a_card_that_wont_start_yet_is_tried_again_not_every_tick(made, monkeypatch):
    engine = Engine(_collection())
    real_tick = engine._tick
    monkeypatch.setattr(engine_module, "GRAPHICS_RETRY_S", 0.5)
    attempts = []

    def tick(compositor, now):
        if len(made) == 1:
            raise RuntimeError("GL_CONTEXT_LOST")
        real_tick(compositor, now)

    monkeypatch.setattr(engine, "_tick", tick)
    counting = compositor_module.Compositor

    def flaky(*a, **k):
        attempts.append(time.monotonic())
        if len(attempts) <= 3:  # the first start, then two more fail while the card comes back
            if len(attempts) == 1:
                return counting(*a, **k)
            raise RuntimeError("no device yet")
        return counting(*a, **k)

    monkeypatch.setattr(compositor_module, "Compositor", flaky)
    engine.start()
    try:
        assert _until(lambda: len(made) == 2, 10)
        gaps = [b - a for a, b in zip(attempts[1:], attempts[2:])]
        assert gaps and min(gaps) >= 0.45  # waited between tries
        assert _until(lambda: "bg" in made[-1].textures)
    finally:
        engine.stop()


def test_restarts_close_together_wait_longer_each_time(made, monkeypatch):
    """Native simple run (2026-11-18): a picture that failed every frame restarted the graphics ~10 times a
    minute for 5 minutes, each time making every source again, and memory climbed ~1 GB a minute. Restarts
    close together now wait 2, 4, 8 ... s (here scaled down)."""
    engine = Engine(_collection())
    monkeypatch.setattr(engine_module, "GRAPHICS_LOST_TICKS", 2)
    monkeypatch.setattr(engine_module, "GRAPHICS_RETRY_S", 0.1)
    real_tick = engine._tick
    broken = {"on": False}

    def tick(compositor, now):
        if broken["on"]:
            raise RuntimeError("access violation")  # fails on every card, old or new
        real_tick(compositor, now)

    monkeypatch.setattr(engine, "_tick", tick)
    engine.start()
    try:
        assert _until(lambda: len(made) == 1 and "bg" in engine._sources)
        broken["on"] = True
        time.sleep(2.0)
        # Without waiting longer each time: a restart every 2 ticks, ~60. With it: 0.1, 0.2, 0.4, 0.8 s apart.
        assert 2 <= len(made) <= 7, len(made)
    finally:
        engine.stop()
