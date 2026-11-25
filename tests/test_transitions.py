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


@pytest.mark.parametrize("kind", ["move", "slide", "swipe", "wipe", "leaf", "bloom"])
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
        kept = compositor.targets[canvas.id].in_fbo
        assert kept is not None  # kept for the next switch: making them each time cost a frame
        compositor.render(canvas, old, sizes, new, 0.5, Transition(kind=kind))  # reused, not made again
        assert compositor.targets[canvas.id].in_fbo is kept
        compositor.end_transition(canvas.id)
        compositor.prune(now=time.monotonic() + compositor.TRANSITION_KEEP_S + 1)  # no switch for a while
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


def test_a_stinger_clip_is_opened_ahead_of_the_switch(compositor, tmp_path):
    """User 2026-11-21: the stinger was "a bit spotty, a slight slow down". Opened at the switch, the clip's file,
    decoder and first pictures came all at once with the bloom's cameras waking; now it waits, opened ahead."""
    clip = make_clip(tmp_path / "s.webm", alpha=255)
    t = Transition(kind="stinger", stinger=Stinger(path=clip, cut_ms=200))
    engine = Engine(collection("a", t))
    engine._warm_stingers(engine._collection)
    engine._wait_warm()  # opened on a thread of its own, never on the render thread
    assert len(engine._warm) == 1
    ahead = next(iter(engine._warm.values()))
    assert ahead.ready.wait(5)
    engine._start_transitions(collection("a", t), collection("b", t))
    running = engine._transitions["h"]
    assert running.stinger is ahead and engine._warm == {}  # the switch takes the one waiting
    now = time.perf_counter()
    assert engine._transition_step(compositor, "h", now)["overlay"] == ahead.id
    assert engine._transition_step(compositor, "h", now + 1.0) is None
    assert not ahead._thread.is_alive()
    engine._wait_warm()
    again = next(iter(engine._warm.values()))  # opened again for the next switch
    assert again is not ahead
    engine._warm_stingers(collection("a"))  # no stinger any more: let go of
    assert engine._warm == {} and not again._thread.is_alive()


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

    from PySide6.QtWidgets import QWidget

    t = Transition()
    host = QWidget()  # a card inside the window, as in the app: Windows holds a window itself to its layout's
    host.resize(500, 400)  # minimum size, so a top-level panel never got as narrow as 220 there
    panel = TransitionsPanel(t, lambda: None)
    panel.setParent(host)
    panel.setGeometry(0, 0, 220, 400)
    host.show()
    app.processEvents()
    assert panel.width() == 220
    assert panel._columns == 3 and panel.form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.WrapAllRows
    panel.setGeometry(0, 0, 400, 400)
    app.processEvents()
    assert panel._columns == 3 and panel.form.rowWrapPolicy() == QFormLayout.RowWrapPolicy.DontWrapRows
    host.close()
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
    assert panel.rules_count.text() == "None"
    dialog = panel.rules_dialog()  # the table opens in a pop-up
    table = dialog.table
    assert table.hint.isVisibleTo(dialog)
    table.add()
    rule = col.transition_rules[0]
    assert rule.to_scene == "a" and rule.transition == col.transition and edits  # starts as the card's own
    assert rule.transition is not col.transition
    row = table._rule_rows()[0]
    assert isinstance(row, RuleRow) and row.to_pick.text().startswith("A")
    assert panel.rules_count.text() == "1 rule"
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



def test_live_has_blooms_sound_and_transitions_cards_kept_in_step(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("LiveBottomRowTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = collection("a")
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        stage = w.live_stage
        assert w.live_side.sound_card.parentWidget() is stage.bottom
        assert w.live_tiles.parentWidget() is stage.blooms_card
        w.live_transitions.set_kind("leaf")  # the Live card and the Build panel stay in step
        assert col.transition.kind == "leaf" and w.transitions_panel.kind_buttons["leaf"].isChecked()
        w.transitions_panel.set_kind("wipe")
        assert w.live_transitions.kind_buttons["wipe"].isChecked()
        assert "live" not in next(p for k, p in w.panels.panels.items() if k == "transitions").modes
    finally:
        w.close()
        QSettings().clear()


def test_live_card_picks_the_transition_from_a_drop_down():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import TransitionsPanel

    t = Transition()
    panel = TransitionsPanel(t, lambda: None, compact=True)
    assert panel.kind_pick.text().startswith("Fade")
    assert not panel.kind_buttons["fade"].parentWidget().isVisibleTo(panel)  # no pill grid on the short card
    next(a for a in panel.kind_pick.menu().actions() if a.text() == "Stinger").trigger()
    assert t.kind == "stinger" and panel.kind_pick.text().startswith("Stinger")


def test_live_card_keeps_deeper_settings_in_a_pop_up():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import TransitionsPanel

    t = Transition(kind="slide")
    seen = []
    panel = TransitionsPanel(t, lambda: seen.append(t.kind), compact=True, collection=collection("a", t))
    panel.resize(300, 400)
    panel.show()
    assert panel.length.isVisibleTo(panel)
    assert not panel.direction_buttons["left"].isVisibleTo(panel)  # direction lives in the pop-up
    assert not panel.softness.isVisibleTo(panel) and not panel.clip.isVisibleTo(panel)
    dialog = panel.settings_dialog()
    assert set(dialog.tabs) == {"transition", "rules"} and dialog.tabs["transition"].isChecked()
    assert not hasattr(dialog, "table")  # the other tab is made when it's first shown (190 ms on the PC)
    rules = panel.rules_dialog()
    assert rules.tabs["rules"].isChecked() and hasattr(rules, "table")  # Between blooms' Edit opens on the table
    assert not hasattr(rules, "panel")
    rules.tabs["transition"].click()
    assert rules.panel.picture.wide and rules.stack.currentWidget().layout().count() == 1
    assert dialog.panel.picture.wide
    dialog.panel.direction_buttons["up"].click()
    assert t.direction == "up" and seen
    dialog.panel.set_kind("stinger")
    assert panel.kind_pick.text().startswith("Stinger")  # the card follows the pop-up
    assert panel.clip_note.isVisibleTo(panel) and not panel.length.isVisibleTo(panel)
    assert dialog.panel.clip.isVisibleTo(dialog.panel) or not dialog.isVisible()
    panel.close()


def test_picture_runs_cleanly_from_a_to_b_for_every_kind():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import numpy as np
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app import transitions_panel
    from sprigcast.app.transitions_panel import KINDS, TransitionPicture

    def pixels(image):
        image = image.convertToFormat(image.Format.Format_RGBA8888)
        bits = np.frombuffer(image.constBits(), np.uint8, image.sizeInBytes())
        return bits.reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4].reshape(
            image.height(), image.width(), 4).astype(int)

    for kind, _label, _tip in KINDS:
        t = Transition(kind=kind, duration_ms=300)
        picture = TransitionPicture(lambda t=t: t, wide=True)
        picture.resize(320, 180)
        a, b = pixels(picture.frame(0.0)), pixels(picture.frame(1.0))
        assert (a != b).any(), kind
        middle = pixels(picture.frame(0.5))
        assert middle.shape == a.shape
        assert picture.progress_at(0) == 0.0 and picture.progress_at(picture.loop_ms() - 1) == 1.0
        if kind == "fade":  # a true crossfade: every pixel halfway between A and B
            assert np.abs(middle[..., :3] - (a[..., :3] + b[..., :3]) / 2).max() <= 2
    # The preview is a picture of the change, not a screen size: no "1280×720" badge.
    assert "1280×720" not in open(transitions_panel.__file__, encoding="utf-8").read()


def test_pop_up_screen_height_never_follows_its_width():
    """PC 2026-11-19: the gear on Live's Transitions card closed SprigCast (stack overflow in Qt6Widgets). The
    pop-up's screen set its height from its width inside a scroll area, so at some pop-up heights the scroll bar
    and the screen's height chased each other inside Qt. Run before the fix, this test ends the test run."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import TransitionPicture, TransitionsPanel

    t = Transition(kind="fade")
    picture = TransitionPicture(lambda: t, wide=True)
    for width in (300, 373, 384, 600):
        picture.resize(width, picture.height())
        app.processEvents()
        assert picture.height() == TransitionPicture.WIDE_HEIGHT
    panel = TransitionsPanel(t, lambda: None, compact=True, collection=collection("a", t))
    dialog = panel.settings_dialog()
    dialog.show()
    app.processEvents()
    for height in range(360, 800, 2):  # every height where the page just fits or just doesn't
        for width in (481, 470):
            dialog.resize(width, height)
            app.processEvents()
            app.processEvents()
    assert dialog.panel.picture.height() == TransitionPicture.WIDE_HEIGHT
    dialog.close()
    panel.close()


def test_a_stinger_rule_shows_its_whole_file_name_and_its_vertical_clip(monkeypatch):
    """User 2026-11-19: the stinger's name was cut ("hidden-slo…inger.webm") in a pop-up with room for it, the
    pop-up couldn't be resized, and the clip for vertical canvases was only inside the rule's own pop-up."""
    from PySide6.QtWidgets import QApplication, QFileDialog

    QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import PopUpGrip, RuleRow, TransitionsPanel
    from sprigcast.core.scene import ANY, Canvas, Scene, SceneCollection, Stinger, Transition, TransitionRule

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id="a", name="Starting Soon", canvas="horizontal")])
    col.transition_rules = [
        TransitionRule(from_scene=ANY, to_scene="a", transition=Transition(
            kind="stinger", stinger=Stinger(path="C:/clips/hidden-slothbloom-stinger.webm"))),
        TransitionRule(from_scene="a", to_scene=ANY, transition=Transition(kind="fade")),
    ]
    changed = []
    panel = TransitionsPanel(col.transition, lambda: changed.append(1), collection=col, compact=True)
    dialog = panel.popup("rules")
    dialog.show()
    QApplication.processEvents()
    try:
        rows = {id(row.rule): row for row in dialog.table._rule_rows()}
        stinger, fade = (rows[id(rule)] for rule in col.transition_rules)
        assert isinstance(stinger, RuleRow)
        assert stinger.how.text() == "Stinger · hidden-slothbloom-stinger.webm"  # whole, at the size it opens
        assert stinger.tall.isVisibleTo(dialog) and stinger.tall.text() == "Same clip"
        assert not stinger.tall_clear.isVisibleTo(dialog)
        assert not fade.tall.isVisibleTo(dialog)  # only a stinger has a clip
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("C:/clips/tall.webm", ""))
        stinger.tall.click()
        assert col.transition_rules[0].transition.stinger.vertical_path == "C:/clips/tall.webm" and changed
        assert stinger.tall.text() == "tall.webm" and stinger.tall_clear.isVisibleTo(dialog)
        stinger.tall_clear.click()
        assert col.transition_rules[0].transition.stinger.vertical_path == "" and stinger.tall.text() == "Same clip"
        # Resizable by its corner; pulled as small as it goes, the name shortens in the middle and nothing is cut off.
        grip = dialog.findChild(PopUpGrip)
        assert grip is not None and grip.isVisibleTo(dialog)
        assert dialog.minimumWidth() < dialog.width() < dialog.maximumWidth()
        dialog.resize(dialog.minimumSize())
        QApplication.processEvents()
        assert "…" in stinger.how.text() and stinger.how.full.endswith("hidden-slothbloom-stinger.webm")
        assert stinger.how.toolTip().endswith("C:/clips/hidden-slothbloom-stinger.webm")
        right = stinger.mapTo(dialog.table, stinger.rect().topRight()).x()
        assert right <= dialog.table.width()
    finally:
        dialog.close()


def test_a_switch_sends_only_the_blooms_on_air(compositor):
    """PC live 2026-11-21: a whole collection for a switch had the engine compare every source in the switch frame
    ("commands 31.4 ms"). A switch alone is SwitchBlooms: the engine's own copy takes the blooms on air."""
    from types import SimpleNamespace

    from sprigcast.app.main_window import MainWindow
    from sprigcast.core.events import ReplaceCollection, SwitchBlooms

    t = Transition(kind="fade", duration_ms=300)
    engine = Engine(collection("a", t))
    engine._sync_sources(compositor)
    sources = dict(engine._sources)
    engine.commands.put(SwitchBlooms({"h": "b"}))
    assert engine._drain_commands(compositor)
    assert engine._collection.active_scenes["h"] == "b" and "h" in engine._transitions
    assert engine._sources == sources  # nothing opened or compared again

    sent = []
    ui = SimpleNamespace(collection=collection("a", t), engine=SimpleNamespace(send=sent.append))
    MainWindow._send_to_engine(ui)
    ui.collection.set_active("b")
    MainWindow._send_to_engine(ui)
    ui.collection.sources[0].settings["color"] = "#ffffff"
    ui.collection.set_active("a")
    MainWindow._send_to_engine(ui)
    assert [type(m) for m in sent] == [ReplaceCollection, SwitchBlooms, ReplaceCollection]
    assert sent[1].active_scenes["h"] == "b"


def test_a_bloom_pointed_at_wakes_its_sources_without_drawing_them():
    engine = Engine(collection("a"))
    assert "green" not in engine._shown_sources()
    engine.peek = frozenset({"b"})
    assert "green" in engine._shown_sources() and "green" not in engine._visible_sources()


def test_between_blooms_groups_rules_by_bloom_and_searches_a_long_list():
    """User 2026-11-25: with a lot of blooms one long list "will look crazy long". Rules are grouped by the bloom
    they go to (A-Z, "Into any bloom" first), and a long list gets a search."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from sprigcast.app.transitions_panel import RulesTable
    from sprigcast.core.scene import ANY, Canvas, Scene, SceneCollection, Transition, TransitionRule

    names = ["Starting Soon", "Be Right Back", "Gameplay", "Just Chatting", "Ending Soon"]
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)],
                          scenes=[Scene(id=f"s{i}", name=n, canvas="horizontal") for i, n in enumerate(names)])
    col.transition_rules = [TransitionRule(from_scene=ANY, to_scene=f"s{i}", transition=Transition(kind="leaf"))
                            for i in range(5)]
    col.transition_rules += [TransitionRule(from_scene="s2", to_scene="s1", transition=Transition(kind="cut")),
                             TransitionRule(from_scene="s0", to_scene=ANY, transition=Transition(kind="fade"))]
    table = RulesTable(col, lambda: None)
    groups = [(title, [r.from_scene for r in rules]) for title, rules in table.groups()]
    assert groups == [("Into any bloom", ["s0"]), ("Into Be Right Back", [ANY, "s2"]), ("Into Ending Soon", [ANY]),
                      ("Into Gameplay", [ANY]), ("Into Just Chatting", [ANY]), ("Into Starting Soon", [ANY])]
    assert table.rows.count() == 6  # one section card per bloom
    assert not table.search.isHidden()  # 7 rules: a search
    assert table._rule_rows() == []  # and every group starts folded to its title
    table.toggle(ANY)
    assert [row.rule.to_scene for row in table._rule_rows()] == [ANY]
    table.toggle(ANY)
    table.search.setText("gameplay")
    assert [title for title, _rules in table.groups()] == ["Into Be Right Back", "Into Gameplay"]
    assert len(table._rule_rows()) == 2
    table.search.setText("nothing like it")
    assert table.rows.count() == 0 and not table.none_found.isHidden()
    table.search.clear()
    col.transition_rules = col.transition_rules[:2]
    table = RulesTable(col, lambda: None)
    assert table.search.isHidden() and len(table._rule_rows()) == 2  # a short list needs none, and shows open
    # A new rule copies the card's transition into the first bloom (A-Z) that has no rule yet.
    table.add()
    assert col.transition_rules[-1].to_scene == "s4" and col.transition_rules[-1].transition.kind == "fade"
