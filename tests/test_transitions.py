import time

import numpy as np
import pytest

from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig, Stinger, Transition
from sprigcast.render.compositor import cover_rect, shape_reach
from sprigcast.render.engine import Engine, stinger_path
from sprigcast.render.stinger import StingerPlayer, fit_size


def collection(active: str, transition: Transition | None = None, vertical: bool = False) -> SceneCollection:
    canvases = [Canvas(id="h", width=64, height=36)]
    scenes = [Scene(id="a", name="A", canvas="h", items=[SceneItem(source="red")]),
              Scene(id="b", name="B", canvas="h", items=[SceneItem(source="green")])]
    active_scenes = {"h": active}
    if vertical:
        canvases.append(Canvas(id="v", width=36, height=64))
        scenes += [Scene(id="va", name="VA", canvas="v", items=[SceneItem(source="red")]),
                   Scene(id="vb", name="VB", canvas="v", items=[SceneItem(source="green")])]
        active_scenes["v"] = "v" + active
    sources = [SourceConfig(id=c, kind="color", name=c) for c in ("red", "green")]
    return SceneCollection(canvases=canvases, sources=sources, scenes=scenes, active_scenes=active_scenes,
                           transition=transition or Transition())


def test_switching_scene_starts_a_fade_from_the_old_scene():
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("b"))
    running = engine._transitions["h"]
    assert running.outgoing.id == "a" and running.duration == 0.3 and running.settings.kind == "fade"


def test_other_edits_and_cuts_do_not_transition():
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("a"))
    assert engine._transitions == {}
    engine._start_transitions(collection("a"), collection("b", Transition(kind="cut")))
    assert engine._transitions == {}


@pytest.mark.parametrize("kind", ["slide", "swipe", "wipe", "leaf", "bloom"])
def test_every_kind_runs_for_its_duration(kind):
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("b", Transition(kind=kind, duration_ms=500)))
    assert engine._transitions["h"].settings.kind == kind and engine._transitions["h"].duration == 0.5


def test_old_transition_files_still_load():
    assert Transition.model_validate({"kind": "fade", "duration_ms": 300}).stinger.path == ""


def test_stinger_without_a_clip_falls_back_to_a_fade():
    engine = Engine(collection("a"))
    engine._start_transitions(collection("a"), collection("b", Transition(kind="stinger")))
    assert engine._transitions["h"].settings.kind == "fade"


def test_vertical_canvas_uses_its_own_clip_when_set():
    t = Transition(kind="stinger", stinger=Stinger(path="wide.webm", vertical_path="tall.webm"))
    assert stinger_path(t, Canvas(id="h", width=1920, height=1080)) == "wide.webm"
    assert stinger_path(t, Canvas(id="v", width=1080, height=1920)) == "tall.webm"
    t.stinger.vertical_path = ""
    assert stinger_path(t, Canvas(id="v", width=1080, height=1920)) == "wide.webm"


def test_cover_rect_fills_the_canvas():
    assert cover_rect((1920, 1080), (1920, 1080)) == (0.0, 0.0, 1.0, 1.0)
    x, y, w, h = cover_rect((1920, 1080), (1080, 1920))  # a wide clip on a tall canvas: crop the sides
    assert y == 0.0 and h == 1.0 and w == pytest.approx((1080 / 1920) / (1920 / 1080)) and x == pytest.approx((1 - w) / 2)


def test_fit_size_only_shrinks():
    assert fit_size((1920, 1080), (3840, 3840)) == (1920, 1080)
    assert fit_size((3840, 2160), (1920, 1920)) == (1920, 1080)


def test_leaf_and_bloom_reach_every_corner():
    for kind in ("leaf", "bloom"):
        for size in ((1920, 1080), (1080, 1920)):
            assert shape_reach(kind, size, 0.0) > 0.5


# Stinger clips --------------------------------------------------------------------------------------


def make_clip(path, frames=15, fps=30, size=(64, 36), alpha=128):
    av = pytest.importorskip("av")
    if "libvpx-vp9" not in av.codecs_available or "libopus" not in av.codecs_available:
        pytest.skip("no VP9/Opus encoder")
    out = av.open(str(path), "w")
    video = out.add_stream("libvpx-vp9", rate=fps)
    video.width, video.height, video.pix_fmt = size[0], size[1], "yuva420p"
    video.options = {"auto-alt-ref": "0", "deadline": "realtime", "cpu-used": "8"}
    audio = out.add_stream("libopus", rate=48000)
    audio.layout = "stereo"
    for i in range(frames):
        picture = np.zeros((size[1], size[0], 4), np.uint8)
        picture[...] = (0, 0, 255, alpha)
        frame = av.VideoFrame.from_ndarray(picture, format="rgba").reformat(format="yuva420p")
        frame.pts = i
        for packet in video.encode(frame):
            out.mux(packet)
    for packet in video.encode():
        out.mux(packet)
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(int(48000 * frames / fps)) / 48000)).astype(np.float32)
    data = np.stack([tone, tone], 1).reshape(1, -1)
    for start in range(0, data.shape[1] // 2 - 960, 960):
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(data[:, start * 2:(start + 960) * 2]),
                                           format="flt", layout="stereo")
        frame.sample_rate, frame.pts = 48000, start
        for packet in audio.encode(frame):
            out.mux(packet)
    for packet in audio.encode():
        out.mux(packet)
    out.close()
    return str(path)


def test_player_keeps_see_through_parts_and_sound(tmp_path):
    player = StingerPlayer(make_clip(tmp_path / "s.webm"))
    player.start()
    try:
        assert player.ready.wait(5) and player.error is None
        assert player.duration == pytest.approx(0.5, abs=0.05)
        frame = player.frame_at(0.0)
        assert frame.shape == (36, 64, 4) and 110 < frame[0, 0, 3] < 150 and frame[0, 0, 2] > 240
        assert player.frame_at(0.0) is None  # nothing new
        time.sleep(0.2)  # let it decode ahead
        assert player.frame_at(0.2) is not None
        deadline = time.time() + 2
        while time.time() < deadline and np.abs(player.read_block(1024)).max() < 0.1:
            time.sleep(0.02)
        assert np.abs(player.read_block(1024)).max() > 0.1  # the clip's sound
    finally:
        player.close()
    assert not player._thread.is_alive()


def test_player_reports_a_bad_file(tmp_path):
    bad = tmp_path / "nope.webm"
    bad.write_bytes(b"not a video")
    player = StingerPlayer(str(bad))
    player.start()
    assert player.ready.wait(5) and player.error


@pytest.fixture(scope="module")
def compositor():
    pytest.importorskip("moderngl")
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def pixel(comp, canvas_id, w, h, x, y):
    return tuple(np.frombuffer(comp.read(canvas_id), dtype=np.uint8).reshape(h, w, 4)[y, x])


def _upload_colours(comp, size):
    comp.upload("red", np.full((size[1], size[0], 4), (255, 0, 0, 255), np.uint8))
    comp.upload("green", np.full((size[1], size[0], 4), (0, 255, 0, 255), np.uint8))


@pytest.mark.parametrize("kind", ["fade", "slide", "swipe", "wipe", "leaf", "bloom"])
def test_kinds_go_from_old_to_new_on_both_shapes(compositor, kind):
    for canvas in (Canvas(id="h", width=64, height=36), Canvas(id="v", width=36, height=64)):
        size = (canvas.width, canvas.height)
        _upload_colours(compositor, size)
        old = Scene(id="a", name="A", canvas=canvas.id, items=[SceneItem(source="red")])
        new = Scene(id="b", name="B", canvas=canvas.id, items=[SceneItem(source="green")])
        sizes = {"red": size, "green": size}
        greens = []
        for progress in (0.0, 0.5, 0.999):
            compositor.render(canvas, new, sizes, old, progress, Transition(kind=kind))
            img = np.frombuffer(compositor.read(canvas.id), dtype=np.uint8).reshape(size[1], size[0], 4)
            greens.append(round(float(img[..., 1].mean()) / 255, 3))
        assert greens[0] == 0.0 and 0.0 < greens[1] < 1.0 and greens[2] == 1.0, (kind, canvas.id, greens)
        compositor.end_transition(canvas.id)
        assert compositor.targets[canvas.id].in_fbo is None


def test_slide_moves_the_way_it_says(compositor):
    canvas = Canvas(id="h", width=64, height=36)
    _upload_colours(compositor, (64, 36))
    old = Scene(id="a", name="A", canvas="h", items=[SceneItem(source="red")])
    new = Scene(id="b", name="B", canvas="h", items=[SceneItem(source="green")])
    sizes = {"red": (64, 36), "green": (64, 36)}
    compositor.render(canvas, new, sizes, old, 0.5, Transition(kind="slide", direction="left"))
    assert pixel(compositor, "h", 64, 36, 60, 18)[1] == 255  # new comes in from the right
    assert pixel(compositor, "h", 64, 36, 2, 18)[0] == 255
    compositor.render(canvas, new, sizes, old, 0.5, Transition(kind="slide", direction="down"))
    assert pixel(compositor, "h", 64, 36, 30, 2)[1] == 255  # moving down: new comes from the top
    assert pixel(compositor, "h", 64, 36, 30, 33)[0] == 255


def test_engine_plays_a_stinger_over_the_switch(compositor, tmp_path):
    clip = make_clip(tmp_path / "s.webm", alpha=255)
    t = Transition(kind="stinger", stinger=Stinger(path=clip, cut_ms=200))
    engine = Engine(collection("a", t, vertical=True))
    _upload_colours(compositor, (64, 64))
    sizes = {"red": (64, 64), "green": (64, 64)}
    engine._start_transitions(collection("a", t, vertical=True), collection("b", t, vertical=True))
    running = engine._transitions["h"]
    assert running.stinger is engine._transitions["v"].stinger  # one player for both shapes
    assert running.stinger.ready.wait(5)
    now = time.perf_counter()
    extra = engine._transition_step(compositor, "h", now)
    assert extra["overlay"] == running.stinger.id and not extra["cut"]
    assert running.stinger in engine.mixer._extras  # its sound goes to the mixer
    canvas = engine._collection.canvases[0]
    compositor.render(canvas, engine._collection.scenes[1], sizes, **extra)
    assert pixel(compositor, "h", 64, 36, 32, 18)[2] > 240  # the opaque blue clip covers everything
    extra = engine._transition_step(compositor, "h", now + 0.25)
    assert extra["cut"]
    assert engine._transition_step(compositor, "h", now + 1.0) is None  # done
    assert "h" not in engine._transitions and running.stinger in engine.mixer._extras  # vertical still plays
    assert engine._transition_step(compositor, "v", now + 1.0) is None
    assert engine.mixer._extras == [] and running.stinger.id not in compositor.textures
    assert not running.stinger._thread.is_alive()


def test_engine_falls_back_to_a_fade_when_the_clip_will_not_open(compositor, tmp_path):
    bad = tmp_path / "bad.webm"
    bad.write_bytes(b"nope")
    t = Transition(kind="stinger", stinger=Stinger(path=str(bad)))
    engine = Engine(collection("a", t))
    engine._start_transitions(collection("a", t), collection("b", t))
    assert engine._transitions["h"].stinger.ready.wait(5)
    extra = engine._transition_step(compositor, "h", time.perf_counter())
    assert extra["transition"].kind == "fade"


# The panel and the Live chip ------------------------------------------------------------------------


def test_panel_fits_narrow_cards_and_live_chip_picks_a_transition(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QFormLayout

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import TransitionsPanel, summary

    t = Transition()
    panel = TransitionsPanel(t, lambda: None)
    panel.resize(220, 400)
    panel.show()
    app.processEvents()
    assert panel._columns == 2 and panel.form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapAllRows
    panel.resize(400, 400)
    app.processEvents()
    assert panel._columns == 4 and panel.form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.DontWrapRows
    panel.close()
    assert summary(Transition(kind="leaf", duration_ms=500)) == "Leaf · 0.5 s"
    assert summary(Transition(kind="stinger")) == "Stinger"

    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("TransitionChipTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = collection("a")
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        chip = w.live_panel.transition_chip
        assert chip.text().startswith("Fade · 0.3 s")
        bloom = next(a for a in chip.menu().actions() if a.text() == "Bloom")
        bloom.trigger()
        assert col.transition.kind == "bloom" and chip.text().startswith("Bloom")
        assert w.transitions_panel.kind_buttons["bloom"].isChecked()
    finally:
        w.close()
        QSettings().clear()


# Transition table -------------------------------------------------------------------------------------


def test_rules_pick_the_transition_for_each_switch():
    from sprigcast.core.scene import TransitionRule

    col = collection("a", vertical=True)
    col.scenes[3].linked_to = "b"  # vb is b's vertical twin
    col.scenes[2].linked_to = "a"
    col.transition_rules = [
        TransitionRule(from_scene="", to_scene="b", transition=Transition(kind="stinger")),
        TransitionRule(from_scene="b", to_scene="a", transition=Transition(kind="bloom")),
    ]
    assert col.transition_for("a", "b").kind == "stinger"
    assert col.transition_for("va", "vb").kind == "stinger"  # the twin follows its bloom's rule
    assert col.transition_for("b", "a").kind == "bloom"
    col.transition_rules.append(TransitionRule(from_scene="a", to_scene="b", transition=Transition(kind="cut")))
    assert col.transition_for("a", "b").kind == "cut"  # from and to both named beats "any"
    col.transition_rules = []
    assert col.transition_for("a", "b") is col.transition


def test_engine_uses_the_rule_and_rules_for_deleted_blooms_go(tmp_path):
    from sprigcast.core.scene import TransitionRule

    old = collection("a")
    new = collection("b")
    new.transition_rules = [TransitionRule(from_scene="a", to_scene="b", transition=Transition(kind="leaf"))]
    engine = Engine(old)
    engine._start_transitions(old, new)
    assert engine._transitions["h"].settings.kind == "leaf"
    data = new.model_dump()
    data["transition_rules"].append({"from_scene": "gone", "to_scene": "b"})
    assert len(SceneCollection.model_validate(data).transition_rules) == 1


def test_rules_table_adds_edits_and_removes():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import RuleRow, TransitionsPanel

    col = collection("a")
    edits = []
    panel = TransitionsPanel(col.transition, lambda: edits.append(1), collection=col)
    table = panel.rules
    assert table.hint.isVisibleTo(panel)
    table.add()
    rule = col.transition_rules[0]
    assert rule.to_scene == "a" and rule.transition.kind == "stinger" and edits
    row = table.rows.itemAt(0).widget()
    assert isinstance(row, RuleRow) and row.to_pick.text().startswith("A")
    row._set_end("from_scene", "b")
    assert rule.from_scene == "b" and row.from_pick.text().startswith("B")
    table.remove(rule)
    assert col.transition_rules == [] and table.rows.count() == 0


def test_a_long_sound_list_scrolls_instead_of_pushing_panels_away():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.live_stage import FitScroll

    body = QWidget()
    rows = QVBoxLayout(body)
    rows.addWidget(QLabel("one"))
    scroll = FitScroll(body)
    short = scroll.maximumHeight()
    for i in range(30):
        rows.addWidget(QLabel(f"sound {i}"))
    app.processEvents()
    assert scroll.maximumHeight() > short  # grows with the list...
    assert scroll.minimumSizeHint().height() <= FitScroll.SHORTEST  # ...but can shrink and scroll
