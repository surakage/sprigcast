"""Source record: a second recording without the sources you pick (picture and sound)."""

import time

import numpy as np
import pytest

from sprigcast.audio.mixer import Mixer
from sprigcast.core.scene import Canvas, RecordingSettings, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.render.engine import Engine


class Fake:
    def __init__(self, sid, value, kind="audio_output", **settings):
        self.id, self.value, self.kind, self.gain, self.name = sid, value, kind, 1.0, sid
        self.settings = settings

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


def test_clean_track_is_the_recording_without_the_left_out_sounds():
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([Fake("game", 0.25), Fake("alerts", 0.5), Fake("bgm", 0.125, tracks=["stream"])])
    mixer.clean_out = frozenset({"alerts"})
    got = []
    mixer.add_tap(got.append, ("recording", "clean"))
    mixer.tick()
    recording, clean = got[0]
    assert recording[0, 0] == pytest.approx(0.75) and clean[0, 0] == pytest.approx(0.25)


class FakeCompositor:
    def __init__(self):
        self.rendered, self.parts = [], {}

    def upload(self, *a):
        pass

    def render(self, canvas, scene, sizes, **kw):
        self.rendered.append((canvas.id, [i.source for i in scene.items if i.visible] if scene else []))

    def read(self, key):
        return key.encode()


def _collection():
    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id=i, kind="color", name=i) for i in ("game", "cam", "alerts")],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source=i) for i in ("game", "cam", "alerts")])],
        active_scenes={"h": "a"},
    )


def test_engine_draws_a_copy_without_the_left_out_sources_for_its_taps():
    engine, fake = Engine(_collection()), FakeCompositor()
    engine.previews = frozenset()
    main, clean = [], []
    engine.add_frame_tap("h", main.append)
    engine.add_frame_tap("clean:h", clean.append)
    engine._tick(fake, 10.0)
    assert ("clean:h", ["game", "cam", "alerts"]) not in fake.rendered and not clean  # not asked for yet
    engine.set_clean("h", {"alerts"})
    engine._tick(fake, 10.1)
    assert ("clean:h", ["game", "cam"]) in fake.rendered and ("h", ["game", "cam", "alerts"]) in fake.rendered
    assert clean[0].data == b"clean:h" and main[-1].data == b"h"
    engine.set_clean("h", None)
    fake.rendered.clear()
    engine._tick(fake, 10.2)
    assert [r[0] for r in fake.rendered] == ["h"]


def test_settings_and_the_picker():
    assert not RecordingSettings().source_record and RecordingSettings().source_record_hide == []
    from sprigcast.app.source_record_box import pickable

    col = _collection()
    col.sources.append(SourceConfig(id="mic", kind="audio_input", name="Mic"))
    col.sources.append(SourceConfig(id="unused", kind="color", name="Unused"))
    assert [s.id for s in pickable(col)] == ["game", "cam", "alerts", "mic"]


def test_source_recording_writes_its_own_file_without_them(tmp_path):
    pytest.importorskip("av")
    from test_shared_encoder import CANVASES, FakeEngine, _audio_tracks

    from sprigcast.render.engine import Frame

    from sprigcast.output.source_record import SourceRecording

    class CleanEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.mixer = self
            self.clean_out, self.clean = frozenset(), {}

        def set_clean(self, canvas_id, hidden):
            if hidden is None:
                self.clean.pop(canvas_id, None)
            else:
                self.clean[canvas_id] = hidden

    engine = CleanEngine()
    rec = SourceRecording.start(engine, CANVASES[:1], tmp_path, {"alerts"}, 800, codec="libx264")
    assert engine.clean == {"horizontal": frozenset({"alerts"})} and engine.clean_out == {"alerts"}
    assert list(engine.taps) == ["clean:horizontal"]
    start = time.perf_counter()
    for i in range(20):
        for cb in engine.taps["clean:horizontal"]:
            cb(Frame("clean:horizontal", 64, 36, bytes(64 * 36 * 4), start + 0.01 + i / 30))
        for cb in engine.audio:
            cb(np.full((1600, 2), 0.25, dtype=np.float32))
        time.sleep(0.005)
    time.sleep(0.3)
    rec.stop()
    assert rec.errors == [] and engine.clean == {} and engine.clean_out == frozenset()
    assert "horizontal source" in rec.paths[0].name
    assert _audio_tracks(rec.paths[0]) == [("", 0.25)]
