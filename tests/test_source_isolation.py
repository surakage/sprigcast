"""One source failing is that source's problem only (as in OBS, where each source is ticked on its own): the canvas,
the other sources and the sound carry on, and closing a source never makes the render thread wait."""

import threading
import time

import numpy as np

from sprigcast.audio.mixer import BLOCK, Mixer
from sprigcast.core import retire
from sprigcast.core.events import EngineError
from sprigcast.render.engine import FAIL_RETRY_S, Engine
from test_sleeping_sources import FakeCompositor, Sleeper, _engine


class Broken(Sleeper):
    def __init__(self, source_id, fail_in=("video_frame",)):
        super().__init__(source_id)
        self.fail_in, self.asked = set(fail_in), 0

    def video_frame(self):
        self.asked += 1
        if "video_frame" in self.fail_in:
            raise RuntimeError("plugin bug")
        return super().video_frame()

    def wake(self):
        super().wake()
        if "wake" in self.fail_in:
            raise RuntimeError("won't wake")

    def sleep(self):
        super().sleep()
        if "sleep" in self.fail_in:
            raise RuntimeError("won't sleep")


def _errors(engine):
    out = []
    while not engine.events.empty():
        event = engine.events.get_nowait()
        if isinstance(event, EngineError):
            out.append(event)
    return out


def test_a_source_that_fails_doesnt_stop_the_others_or_the_canvas():
    engine, _col = _engine()
    bad = engine._sources["game"] = Broken("game")
    fake = FakeCompositor()
    engine._tick(fake, 100.0)  # no exception reaches the loop
    assert "cam" in fake.uploaded and "game" not in fake.uploaded
    assert bad.status.startswith("failed: RuntimeError")
    for i in range(1, 20):  # a frame's worth of ticks: said once, asked again only after FAIL_RETRY_S
        engine._tick(fake, 100.0 + i / 60)
    assert bad.asked == 1 and len(_errors(engine)) == 1
    engine._tick(fake, 100.0 + FAIL_RETRY_S + 0.01)
    assert bad.asked == 2 and not _errors(engine)  # the same problem isn't said again
    bad.fail_in.clear()  # it recovers
    engine._tick(fake, 100.0 + 2 * FAIL_RETRY_S + 0.02)
    assert "game" in fake.uploaded and bad.status == "ok"


def test_a_source_that_fails_to_sleep_or_wake_doesnt_break_the_tick():
    engine, col = _engine()
    engine._sources["game"] = Broken("game", fail_in=("sleep", "wake"))
    fake = FakeCompositor()
    engine._tick(fake, 100.0)
    col.active_scenes["h"] = "b"
    engine._tick(fake, 101.0)  # game sleeps (and fails to)
    assert engine._sources["game"].asleep and "game" in fake.forgotten
    col.active_scenes["h"] = "a"
    engine._tick(fake, 102.0)  # and wakes (and fails to): still asked for a picture, the canvas still drawn
    assert not engine._sources["game"].asleep and "cam" in fake.uploaded
    assert len(_errors(engine)) == 2


class SlowStop(Sleeper):
    def __init__(self, source_id, seconds):
        super().__init__(source_id)
        self.seconds, self.stopped = seconds, threading.Event()

    def stop(self):
        time.sleep(self.seconds)
        self.stopped.set()


def test_removing_a_source_never_waits_for_it_to_close():
    engine, col = _engine()
    slow = engine._sources["game"] = SlowStop("game", 0.5)
    col.sources = [s for s in col.sources if s.id != "game"]
    col.scenes[0].items = [i for i in col.scenes[0].items if i.source != "game"]
    began = time.perf_counter()
    engine._sync_sources(FakeCompositor())
    assert time.perf_counter() - began < 0.2 and "game" not in engine._sources
    assert retire.RETIRER.wait(3.0) and slow.stopped.is_set()


def test_a_device_change_opens_a_new_source_while_the_old_one_closes(monkeypatch):
    from sprigcast.render import engine as module

    class Device(SlowStop):
        def needs_new(self, settings):
            return settings.get("device") != self.settings.get("device")

    engine, col = _engine()
    old = engine._sources["game"] = Device("game", 0.3)
    made = []
    monkeypatch.setattr(module, "create_source", lambda kind, sid, settings: made.append(Sleeper(sid)) or made[-1])
    next(s for s in col.sources if s.id == "game").settings = {"device": "other"}
    began = time.perf_counter()
    engine._sync_sources(FakeCompositor())
    assert time.perf_counter() - began < 0.2
    assert engine._sources["game"] is made[0] and made[0].log == ["start"]
    assert retire.RETIRER.wait(3.0) and old.stopped.is_set()


def test_quitting_closes_sources_side_by_side():
    sources = [SlowStop(f"s{i}", 0.3) for i in range(5)]
    began = time.perf_counter()
    assert retire.stop_all(sources, 3.0) == []
    assert time.perf_counter() - began < 1.0  # 5 x 0.3 s one after another would be 1.5 s
    late = retire.stop_all([SlowStop("hung", 1.0)], 0.1)
    assert late == ["hung"]


def test_the_closing_helper_ends_when_idle_and_comes_back():
    helper = retire.Retirer(idle_s=0.05)
    first = SlowStop("a", 0.0)
    helper.retire(first)
    assert helper.wait(2.0) and first.stopped.is_set()
    time.sleep(0.3)
    assert helper._thread is None  # no thread kept for nothing
    second = SlowStop("b", 0.0)
    helper.retire(second)
    assert helper.wait(2.0) and second.stopped.is_set()


class Sound:
    def __init__(self, sid, value=0.25, fail=False, block=None):
        self.id, self.settings, self.value, self.fail, self.block = sid, {}, value, fail, block

    gain = 1.0

    def read_block(self, n):
        if self.fail:
            raise OSError("device unplugged")
        if self.block is not None:
            return self.block
        return np.full((n, 2), self.value, dtype=np.float32)


def test_one_bad_sound_is_silent_and_the_mix_carries_on(caplog):
    mixer = Mixer(report=lambda levels: None)
    got = []
    mixer.add_tap(lambda block: (_ for _ in ()).throw(ValueError("encoder gone")))  # a failing output
    mixer.add_tap(got.append)
    mixer.set_sources([Sound("mic"), Sound("bad", fail=True), Sound("odd", block=np.zeros(3, np.float32))])
    for _ in range(3):
        out = mixer.tick()
    assert np.allclose(out, 0.25) and len(got) == 3  # the good mic still mixed, the good output still fed
    said = [r for r in caplog.records if "failed" in r.getMessage()]
    assert len(said) == 3  # bad source, odd block, failing output: each said once, not every block


def test_the_sound_thread_survives_a_failing_block(monkeypatch):
    mixer = Mixer(report=lambda levels: None)
    calls = []

    def tick(measure=True):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return np.zeros((BLOCK, 2), np.float32)

    monkeypatch.setattr(mixer, "tick", tick)
    mixer.start()
    time.sleep(0.15)
    mixer.stop()
    assert len(calls) > 2
