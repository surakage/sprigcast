"""Sharp sources: text and the card plugins draw at the size they're shown, so they stay crisp made big
(and get lighter made small), while their layout box (`size`) never changes."""

import numpy as np
import pytest

from sprigcast import plugin_store
from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
from sprigcast.core.source import create_source, known_kinds
from sprigcast.render.engine import detail_step, shown_detail


def _garden():
    return SceneCollection(
        canvases=[Canvas(id="h", width=1920, height=1080)],
        sources=[
            SourceConfig(id="clock", kind="text", name="Clock"),
            SourceConfig(id="sprig", kind="scene", name="Corner", settings={"scene": "p"}),
        ],
        scenes=[
            Scene(id="show", name="Show", canvas="h", items=[SceneItem(source="sprig", scale=0.5)]),
            Scene(id="p", name="Corner", canvas="h", kind="part", items=[SceneItem(source="clock", scale=3.0)]),
        ],
        active_scenes={"h": "show"},
    )


def test_detail_is_the_biggest_it_is_shown_anywhere_including_inside_sprigs():
    col = _garden()
    # In Build the sprig is edited at its own size (3x); in the bloom it's placed at half size (1.5x).
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(3.0)}
    col.scene("p").items[0].scale = 0.2
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(0.2)}
    col.scene("show").items[0].scale = 4.0  # the sprig made big: what's in it is shown bigger too
    assert shown_detail(col, {"clock": (200, 50)}) == {"clock": pytest.approx(0.8)}


def test_bounds_decide_the_detail_when_on():
    col = _garden()
    item = col.scene("p").items[0]
    item.bounds, item.bounds_size = "fit", (800.0, 100.0)
    assert shown_detail(col, {"clock": (200, 50)})["clock"] == pytest.approx(2.0)  # 100 / 50


def test_a_sprig_that_holds_itself_does_not_loop_forever():
    col = _garden()
    col.sources.append(SourceConfig(id="again", kind="scene", name="Corner", settings={"scene": "p"}))
    col.scene("p").items.append(SceneItem(source="again", scale=2.0))
    assert shown_detail(col, {"clock": (200, 50)})["clock"] == pytest.approx(3.0)


def test_detail_steps_round_up_and_stay_in_range():
    assert detail_step(1.0, (640, 220)) == 1.0
    assert detail_step(1.01, (640, 220)) == pytest.approx(2 ** 0.25, abs=1e-3)  # never drawn smaller than shown
    assert detail_step(1.9, (640, 220)) == 2.0
    assert detail_step(0.5, (640, 220)) == 0.5
    assert detail_step(0.001, (640, 220)) == 0.125  # tiny: still a few pixels to draw with
    assert detail_step(50.0, (640, 220)) == 4.0
    assert detail_step(50.0, (1600, 260)) == pytest.approx(4096 / 1600, abs=1e-3)  # never over 4096 pixels wide
    steps = {detail_step(1.0 + i / 100, (640, 220)) for i in range(101)}
    assert len(steps) <= 5  # dragging a corner from 1x to 2x redraws it a handful of times, not 100


def test_text_draws_more_pixels_but_keeps_its_box():
    source = create_source("text", "t", {"text": "Hello", "size": 40})
    box = source.size
    first = source.video_frame()
    assert first.shape[:2] == (box[1], box[0])
    source.set_detail(2.0)
    big = source.video_frame()
    assert big is not None and source.size == box  # a new picture straight away, the layout untouched
    assert big.shape[1] == pytest.approx(box[0] * 2, abs=4) and big.shape[0] == pytest.approx(box[1] * 2, abs=4)
    assert source.video_frame() is None  # and nothing again until something changes
    source.set_detail(0.5)
    assert source.video_frame().shape[1] == pytest.approx(box[0] / 2, abs=3) and source.size == box


def test_pictures_and_cameras_are_not_sharp():
    assert not create_source("color", "c", {"color": "#ff0000ff", "width": 4, "height": 4}).sharp
    assert create_source("text", "t", {}).sharp


CARDS = {"local_time": (640, 220), "stream_timer": (640, 220), "now_playing": (1100, 200)}


@pytest.fixture
def qt():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("kind", sorted(CARDS))
def test_cards_draw_at_their_detail_and_keep_their_box(kind, qt, tmp_path):
    plugin = next(p for p in plugin_store.catalog() if p.id == kind)
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    try:
        import sys

        module = sys.modules[f"sprigcast_plugin_{kind}"]
        source = create_source(kind, "s", {})
        assert source.sharp and source.size == CARDS[kind]
        if kind == "now_playing":
            image = module.paint(module.Track(title="Song", artist="Someone", playing=True), {}, 2.0)
        elif kind == "local_time":
            image = module.paint("8:03 PM", "Sat, Nov 14", {}, 2.0)
        else:
            image = module.paint("1:23", "", {}, True, 2.0)
        assert (image.width(), image.height()) == (CARDS[kind][0] * 2, CARDS[kind][1] * 2)
        rows = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
        assert rows.any()  # something drawn
    finally:
        plugin_store.remove(tmp_path / "plugins", kind)
    assert kind not in known_kinds()


def test_clock_card_is_drawn_again_when_its_detail_steps(qt, tmp_path):
    plugin = next(p for p in plugin_store.catalog() if p.id == "local_time")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    try:
        source = create_source("local_time", "s", {})
        assert source.video_frame().shape[:2] == (220, 640)
        assert source.video_frame() is None  # same minute: nothing drawn
        source.set_detail(2.0)
        assert source.video_frame().shape[:2] == (440, 1280)
        assert source.size == (640, 220)
    finally:
        plugin_store.remove(tmp_path / "plugins", "local_time")


def test_cards_start_readable_in_their_corner():
    col = SceneCollection(canvases=[Canvas(id="h", width=1920, height=1080)],
                          scenes=[Scene(id="b", name="B", canvas="h")], active_scenes={"h": "b"})
    col.sources.append(SourceConfig(id="t", kind="stream_timer", name="Stream timer"))
    part = col.own_sprig(col.scene("b"), "t", (640, 220), "Stream timer", "top_left", 0.14)
    item = part.items[0]
    assert 220 * item.scale == pytest.approx(1080 * 0.14)  # the card's box a seventh of the canvas tall
    assert item.x == pytest.approx(1080 * 0.035) and item.y == pytest.approx(1080 * 0.035)


def test_the_engine_tells_sharp_sources_their_detail_only_when_it_steps():
    from sprigcast.render.engine import Engine

    col = _garden()
    engine = Engine(col)
    told = []
    clock = create_source("text", "clock", {"text": "Hi"})
    real = clock.set_detail
    clock.set_detail = lambda d: (told.append(d), real(d))
    engine._sources = {"clock": clock, "sprig": create_source("color", "sprig", {})}
    engine._sync_detail()
    step = detail_step(3.0, clock.size)
    assert told == [step] and clock.detail == step
    engine._sync_detail()
    col = engine._collection
    col.scene("p").items[0].scale = 2.95  # a small drag: same step, nothing redrawn
    engine._sync_detail()
    assert told == [step]
    col.scene("p").items[0].scale = 1.0
    engine._sync_detail()
    assert told == [step, 1.0]


STARTS = {  # kind: (box, corner, height share, band share), as the plugins say
    "stream_timer": ((640, 220), "top_left", 0.14, None), "local_time": ((640, 220), "top_right", 0.14, None),
    "now_playing": ((1100, 200), "bottom_left", 0.14, None),
    "audio_visualizer": ((1280, 400), "bottom_center", None, 0.5), "pngtuber": ((640, 640), "bottom_right", None, None),
    "snap": ((800, 1000), None, None, None), "death_count": ((800, 1000), None, None, None), "instant_replay": ((1920, 1080), None, None, None)}


def _starts(cw, ch):
    """Each plugin's starting box as (left, top, right, bottom) shares of a cw x ch canvas, in pixels too."""
    shares, pixels = {}, {}
    for kind, (box, corner, share, band) in STARTS.items():
        col = SceneCollection(canvases=[Canvas(id="c", width=cw, height=ch)],
                              scenes=[Scene(id="b", name="B", canvas="c")], active_scenes={"c": "b"})
        col.sources.append(SourceConfig(id=kind, kind=kind, name=kind))
        item = col.own_sprig(col.scene("b"), kind, box, kind, corner, share, band).items[0]
        x0, y0, w, h = item.x, item.y, box[0] * item.scale, box[1] * item.scale
        pixels[kind] = (x0, y0, w, h)
        shares[kind] = (x0 / cw, y0 / ch, (x0 + w) / cw, (y0 + h) / ch)
    return shares, pixels


def test_plugins_match_what_their_classes_say(qt, tmp_path):
    for kind, (box, corner, share, band) in STARTS.items():
        plugin = next(p for p in plugin_store.catalog() if p.id == kind)
        assert plugin_store.install(tmp_path / "plugins", plugin) is None
        try:
            from sprigcast.core.source import source_class

            cls = source_class(kind)
            assert (tuple(cls.own_sprig), getattr(cls, "own_sprig_corner", None), getattr(cls, "own_sprig_height", None),
                    getattr(cls, "own_sprig_band", None)) == (box, corner, share, band), kind
        finally:
            plugin_store.remove(tmp_path / "plugins", kind)


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (1080, 1920), (720, 1280)])
def test_every_plugin_starts_on_the_canvas_readable_and_clear_of_the_others(size):
    """Corner cards, bands and overlays scale with the canvas, stay on it, and the bottom band sits above
    the bottom corner card. Portrait keeps out of TikTok's tabs and captions."""
    cw, ch = size
    boxes, pixels = _starts(cw, ch)
    for kind, (x0, y0, w, h) in pixels.items():
        assert x0 >= -0.5 and y0 >= -0.5 and x0 + w <= cw + 0.5 and y0 + h <= ch + 0.5, kind
        if ch > cw and STARTS[kind][1]:
            assert y0 >= 0.12 * ch - 0.5 and y0 + h <= 0.775 * ch + 0.5, kind  # clear of TikTok's own bars
    for card in ("stream_timer", "local_time", "now_playing"):
        assert pixels[card][3] == pytest.approx(0.14 * min(cw, ch))  # readable, not huge
    if cw > ch:
        assert boxes["audio_visualizer"][2] <= boxes["pngtuber"][0]  # the band beside the avatar


def test_720p_and_1080p_start_alike():
    small, big = _starts(1280, 720)[0], _starts(1920, 1080)[0]
    for kind in STARTS:
        assert small[kind] == pytest.approx(big[kind]), kind


@pytest.fixture
def all_plugins(qt, tmp_path):
    for kind in STARTS:
        assert plugin_store.install(tmp_path / "plugins", next(p for p in plugin_store.catalog() if p.id == kind)) is None
    yield
    for kind in STARTS:
        plugin_store.remove(tmp_path / "plugins", kind)


def _add_all(col, bloom_id, kinds):
    from sprigcast.core.source import source_class

    for kind in kinds:
        cls = source_class(kind)
        col.sources.append(SourceConfig(id=kind, kind=kind, name=kind))
        col.own_sprig(col.scene(bloom_id), kind, tuple(cls.own_sprig), kind, getattr(cls, "own_sprig_corner", None),
                      getattr(cls, "own_sprig_height", None), getattr(cls, "own_sprig_band", None))
    return col


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (1080, 1920)])
def test_plugins_added_together_never_start_on_top_of_each_other(all_plugins, size):
    cw, ch = size
    kinds = ["stream_timer", "local_time", "now_playing", "pngtuber", "audio_visualizer"]
    if ch > cw:  # a phone screen has room for five of them; with all six the last one may sit on another
        kinds.remove("audio_visualizer")
    for order in (kinds, kinds[::-1]):
        col = _add_all(SceneCollection(canvases=[Canvas(id="c", width=cw, height=ch)],
                                       scenes=[Scene(id="b", name="B", canvas="c")], active_scenes={"c": "b"}),
                       "b", order)
        boxes = col.overlay_boxes(col.scene("b"), "c")
        assert len(boxes) == len(kinds)
        for i, a in enumerate(boxes):
            assert a[0] >= -0.5 and a[1] >= -0.5 and a[2] <= cw + 0.5 and a[3] <= ch + 0.5
            if ch > cw:
                assert a[1] >= 0.12 * ch - 0.5 and a[3] <= 0.775 * ch + 0.5  # clear of TikTok's own bars
            for b in boxes[i + 1:]:
                overlap = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
                assert overlap[0] <= 0.5 or overlap[1] <= 0.5, (order, a, b)


def test_a_second_clock_takes_the_next_free_corner(all_plugins):
    col = _add_all(SceneCollection(canvases=[Canvas(id="c", width=1920, height=1080)],
                                   scenes=[Scene(id="b", name="B", canvas="c")], active_scenes={"c": "b"}),
                   "b", ["local_time", "now_playing"])
    col.sources.append(SourceConfig(id="t2", kind="local_time", name="Second clock"))
    item = col.own_sprig(col.scene("b"), "t2", (640, 220), "Second clock", "top_right", 0.14).items[0]
    assert item.x < 100 and item.y < 100  # top right is taken: top left
    col.sources.append(SourceConfig(id="t3", kind="local_time", name="Third clock"))
    item = col.own_sprig(col.scene("b"), "t3", (640, 220), "Third clock", "top_right", 0.14).items[0]
    assert item.x > 960 and item.y > 540  # then bottom right; bottom left is Now playing's


def test_the_pngtuber_keeps_clear_of_the_side_buttons_on_vertical(all_plugins):
    col = _add_all(SceneCollection(canvases=[Canvas(id="c", width=1080, height=1920)],
                                   scenes=[Scene(id="b", name="B", canvas="c")], active_scenes={"c": "b"}),
                   "b", ["pngtuber"])
    (box,) = col.overlay_boxes(col.scene("b"), "c")
    assert box[2] <= 1080 * 0.85 + 0.5  # left of TikTok's like / comment / share column


def _small_sprig(cw, ch, k=0.4):
    """A bloom showing a sprig at k of the size near its top left (like a corner overlay)."""
    col = SceneCollection(canvases=[Canvas(id="c", width=cw, height=ch)],
                          scenes=[Scene(id="b", name="B", canvas="c"), Scene(id="p", name="Corner", canvas="c", kind="part")],
                          active_scenes={"c": "b"})
    col.sources.append(SourceConfig(id="ps", kind="scene", name="Corner", settings={"scene": "p"}))
    col.scene("b").items.append(SceneItem(source="ps", scale=k, x=cw * 0.04, y=ch * 0.06))
    return col


def _shown_boxes(col, k, ox, oy):
    """Each plugin in sprig p as it shows on the canvas: (left, top, right, bottom)."""
    from sprigcast.core.source import source_class

    configs = {s.id: s for s in col.sources}
    boxes = {}
    for placed in col.scene("p").items:
        shape = col.shape_for(col.part_of(configs[placed.source]), "c")
        inner = shape.items[0]
        w, h = inner.drawn_size(tuple(source_class(inner.source).own_sprig))
        x, y = ox + k * (placed.x + inner.x * placed.scale), oy + k * (placed.y + inner.y * placed.scale)
        boxes[inner.source] = (x, y, x + k * w * placed.scale, y + k * h * placed.sy)
    return boxes


@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (1080, 1920)])
def test_plugins_dropped_in_a_sprig_shown_small_start_readable_inside_it(all_plugins, size):
    """PC test 2026-11-17: plugins added to a sprig shown at 40% came out at 40% of their size ("miniscule").
    They start sized for the canvas, as far as the sprig's part of it allows: inside it, clear of each other."""
    cw, ch = size
    col = _small_sprig(cw, ch)
    assert col.shown_view(col.scene("p")) == pytest.approx((0.4, cw * 0.04, ch * 0.06))
    assert col.shown_view(col.scene("b")) == (1.0, 0.0, 0.0)
    kinds = ["stream_timer", "local_time", "now_playing", "pngtuber", "audio_visualizer"]
    _add_all(col, "p", kinds)
    boxes = _shown_boxes(col, 0.4, cw * 0.04, ch * 0.06)
    left, top, right, bottom = cw * 0.04, ch * 0.06, cw * 0.44, ch * 0.46
    short = min(cw, ch)
    for kind, b in boxes.items():
        assert b[0] >= left - 0.5 and b[1] >= top - 0.5 and b[2] <= right + 0.5 and b[3] <= bottom + 0.5, kind
    for card in ("stream_timer", "local_time"):
        tall = boxes[card][3] - boxes[card][1]
        assert tall > 0.4 * 0.14 * short * 1.5  # well past the 40% it used to start at
    for i, a in enumerate(kinds):
        for b in kinds[i + 1:]:
            p, q = boxes[a], boxes[b]
            assert min(p[2], q[2]) - max(p[0], q[0]) <= 0.5 or min(p[3], q[3]) - max(p[1], q[1]) <= 0.5, (a, b)


def test_text_and_api_plugins_in_a_sprig_shown_small(all_plugins):
    col = _small_sprig(1920, 1080, 0.5)
    item = SceneItem(source="t")
    col.sources.append(SourceConfig(id="t", kind="text", name="Hello"))
    col.starting_place(item, "text", col.scene("p"))
    assert item.scale == pytest.approx(2.0)  # shows at 1:1 on the canvas through the half-size sprig
    col.sources.append(SourceConfig(id="np", kind="now_playing", name="Song"))
    item = SceneItem(source="np")
    col.starting_place(item, "now_playing", col.scene("p"))
    shown = 0.5 * item.scale * 200  # its box's height on the canvas
    assert 0.5 * 1080 * 0.14 * 1.4 < shown <= 1080 * 0.14 + 0.5  # much more than the half size it started at
    assert 0 <= item.x and item.x + item.scale * 1100 <= 1920 + 0.5  # inside the sprig
