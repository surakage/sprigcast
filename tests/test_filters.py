"""Filters: the model, the shader uniforms, the GPU result and the audio processors."""

import numpy as np
import pytest

from sprigcast.core.filters import (
    AudioChain,
    Compressor,
    Field,
    NoiseGate,
    filter_kind,
    filter_kinds,
    register_filter,
    settings_for,
    video_effects,
)
from sprigcast.core.scene import Canvas, Filter, Scene, SceneCollection, SceneItem


def flt(kind, enabled=True, **settings):
    return Filter(id=f"f-{kind}", kind=kind, name=kind, enabled=enabled, settings=settings)


def test_filters_are_saved_with_the_source():
    collection = SceneCollection.model_validate({
        "canvases": [{"id": "h", "width": 64, "height": 36}],
        "sources": [{"id": "cam", "kind": "color", "name": "Cam",
                     "filters": [{"id": "f1", "kind": "chroma_key", "name": "Chroma Key"}]}],
    })
    again = SceneCollection.model_validate_json(collection.model_dump_json())
    assert again.sources[0].filters[0].kind == "chroma_key"
    assert again.sources[0].filters[0].enabled


def test_kinds_are_split_by_media():
    video = {k.kind for k in filter_kinds("video")}
    audio = {k.kind for k in filter_kinds("audio")}
    assert {"color_correction", "chroma_key", "luma_key", "sharpen", "scroll"} <= video
    assert {"gain", "noise_gate", "compressor", "limiter"} <= audio
    assert settings_for("chroma_key", {"similarity": 300, "junk": 1}) == {
        "key_color": "#00ff00", "similarity": 300, "smoothness": 80, "spill": 100}


def test_no_filters_leave_the_image_untouched():
    fx = video_effects([])
    assert not fx.active and fx.key_mode == 0 and fx.gamma == 1.0


def test_disabled_filters_are_ignored():
    assert not video_effects([flt("chroma_key", enabled=False)]).active


def test_color_correction_maps_obs_ranges():
    fx = video_effects([flt("color_correction", gamma=1.0, contrast=-0.5, saturation=-1.0, opacity=50, hue_shift=180)])
    assert fx.gamma == pytest.approx(0.5)
    assert fx.contrast == pytest.approx(0.5)
    assert fx.saturation == 0.0
    assert fx.opacity == 0.5
    assert fx.hue == pytest.approx(np.pi)


def test_plugin_audio_filter_registers():
    @register_filter("test_half", "Half", "audio", [Field("mix", "Mix", 0.5, 0.0, 1.0, 0.01)])
    class Half:
        def __init__(self, settings):
            self.settings = settings

        def process(self, block):
            return block * self.settings["mix"]

    assert not filter_kind("test_half").builtin
    chain = AudioChain([flt("test_half")])
    out = chain.process(np.ones((1024, 2), dtype=np.float32))
    assert np.allclose(out, 0.5)
    from sprigcast.core import filters

    filters._KINDS.pop("test_half")


# Audio --------------------------------------------------------------------------------------


def tone(level, n=1024):
    t = np.arange(n) / 48000
    wave = (level * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    return np.stack([wave, wave], axis=1)


def test_gain_filter():
    chain = AudioChain([flt("gain", db=-6.0)])
    out = chain.process(tone(0.5))
    assert np.max(np.abs(out)) == pytest.approx(0.5 * 10 ** (-6 / 20), rel=0.01)


def test_noise_gate_mutes_quiet_audio_and_opens_for_loud():
    gate = NoiseGate(settings_for("noise_gate", {"attack_ms": 1, "release_ms": 1, "hold_ms": 0}))
    for _ in range(5):
        quiet = gate.process(tone(0.001))  # about -60 dB
    assert np.max(np.abs(quiet)) < 1e-4
    for _ in range(3):
        loud = gate.process(tone(0.5))
    assert np.max(np.abs(loud)) > 0.45


def test_compressor_turns_loud_audio_down():
    comp = Compressor(settings_for("compressor", {"ratio": 4.0, "threshold_db": -20.0}))
    for _ in range(10):
        out = comp.process(tone(1.0))
    # 0 dB in, 20 dB over the threshold at 4:1 -> 5 dB over: about -15 dB
    assert 20 * np.log10(np.max(np.abs(out[-256:]))) == pytest.approx(-15, abs=1.5)


def test_limiter_never_exceeds_threshold():
    chain = AudioChain([flt("limiter", threshold_db=-6.0)])
    out = chain.process(tone(1.0))
    assert np.max(np.abs(out)) <= 10 ** (-6 / 20) + 1e-6


def test_chain_keeps_state_when_only_settings_change():
    filters = [flt("noise_gate")]
    chain = AudioChain(filters)
    gate = chain.processors[0]
    changed = [flt("noise_gate", open_db=-20.0)]
    assert not chain.matches(changed)
    assert chain.update(changed)
    assert chain.processors[0] is gate and gate.settings["open_db"] == -20.0
    assert not chain.update([flt("gain")])  # different kinds need a new chain


def test_mixer_applies_audio_filters():
    from sprigcast.audio.mixer import Mixer

    class Src:
        id, gain = "mic", 1.0
        audio_chain = AudioChain([flt("gain", db=-6.0)])

        def read_block(self, n):
            return np.full((n, 2), 0.5, dtype=np.float32)

    mixer = Mixer(lambda levels: None)
    mixer.set_sources([Src()])
    assert np.allclose(mixer.tick(), 0.5 * 10 ** (-6 / 20), rtol=1e-3)


# GPU ----------------------------------------------------------------------------------------

moderngl = pytest.importorskip("moderngl")


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def render_one(comp, rgba, filters, size=(16, 16)):
    canvas = Canvas(id="f", width=size[0], height=size[1])
    comp.upload("src", np.full((size[1], size[0], 4), rgba, dtype=np.uint8))
    fx = video_effects(filters)
    comp.effects = {"src": fx} if fx.active else {}
    scene = Scene(id="s", name="s", canvas="f", items=[SceneItem(source="src")])
    comp.render(canvas, scene, {"src": size})
    img = np.frombuffer(comp.read("f"), dtype=np.uint8).reshape(size[1], size[0], 4)
    return tuple(int(v) for v in img[8, 8])


def test_gpu_no_filter_is_identity(compositor):
    assert render_one(compositor, (200, 100, 50, 255), []) == (200, 100, 50, 255)


def test_gpu_chroma_key_removes_green_and_keeps_red(compositor):
    assert render_one(compositor, (0, 255, 0, 255), [flt("chroma_key")]) == (0, 0, 0, 0)  # empty canvas shows through
    r, g, b, _ = render_one(compositor, (255, 0, 0, 255), [flt("chroma_key")])
    assert r > 240 and g < 10 and b < 10


def test_gpu_color_correction(compositor):
    r, g, b, _ = render_one(compositor, (200, 100, 50, 255), [flt("color_correction", saturation=-1.0)])
    assert abs(r - g) <= 2 and abs(g - b) <= 2  # grey
    r, g, b, _ = render_one(compositor, (200, 100, 50, 255), [flt("color_correction", opacity=50)])
    assert 95 <= r <= 105  # half blended over black
    r, g, b, _ = render_one(compositor, (100, 100, 100, 255), [flt("color_correction", brightness=0.2)])
    assert r == pytest.approx(151, abs=2)
    r, g, b, _ = render_one(compositor, (255, 255, 255, 255), [flt("color_correction", color="#ff0000")])
    assert (r, g, b) == (255, 0, 0)


def test_gpu_luma_key_removes_dark(compositor):
    assert render_one(compositor, (10, 10, 10, 255), [flt("luma_key", luma_min=0.2)])[:3] == (0, 0, 0)
    assert render_one(compositor, (200, 200, 200, 255), [flt("luma_key", luma_min=0.2)])[:3] == (200, 200, 200)


def test_gpu_scroll_moves_the_image(compositor):
    canvas = Canvas(id="f", width=16, height=16)
    img = np.zeros((16, 16, 4), dtype=np.uint8)
    img[:, :8] = (255, 0, 0, 255)
    img[:, 8:] = (0, 0, 255, 255)
    compositor.upload("src", img)
    compositor.effects = {"src": video_effects([flt("scroll", speed_x=8.0)])}
    scene = Scene(id="s", name="s", canvas="f", items=[SceneItem(source="src")])
    compositor.time = 0.0
    compositor.render(canvas, scene, {"src": (16, 16)})
    before = np.frombuffer(compositor.read("f"), dtype=np.uint8).reshape(16, 16, 4)[8, 2]
    compositor.time = 1.0  # 8 px to the side: the left half now shows blue
    compositor.render(canvas, scene, {"src": (16, 16)})
    after = np.frombuffer(compositor.read("f"), dtype=np.uint8).reshape(16, 16, 4)[8, 2]
    compositor.effects, compositor.time = {}, 0.0
    assert before[0] == 255 and after[2] == 255


def render_full(comp, img, filters):
    h, w = img.shape[:2]
    canvas = Canvas(id="f", width=w, height=h)
    comp.upload("src", img)
    fx = video_effects(filters)
    comp.effects = {"src": fx} if fx.active else {}
    scene = Scene(id="s", name="s", canvas="f", items=[SceneItem(source="src")])
    comp.render(canvas, scene, {"src": (w, h)})
    out = np.frombuffer(comp.read("f"), dtype=np.uint8).reshape(h, w, 4).copy()
    comp.effects = {}
    return out


def test_lut_filter_settings():
    fx = video_effects([flt("lut", look="mono", amount=50)])
    assert fx.active and fx.lut == "look:mono" and fx.lut_amount == 0.5
    assert video_effects([flt("lut", look="file", file="C:/luts/a.cube")]).lut == "C:/luts/a.cube"
    assert not video_effects([flt("lut", look="file", file="")]).active  # no file picked yet: nothing to do


def test_gpu_lut_mono_and_from_file(compositor, tmp_path):
    from sprigcast.core import looks

    r, g, b, _ = render_one(compositor, (200, 60, 30, 255), [flt("lut", look="mono")])
    assert abs(r - g) <= 2 and abs(g - b) <= 2
    swap = looks.identity(9)[..., ::-1].copy()  # a file that swaps red and blue
    path = looks.write_cube(tmp_path / "swap.cube", swap)
    assert render_one(compositor, (200, 60, 30, 255), [flt("lut", look="file", file=str(path))])[:3] == \
        pytest.approx((30, 60, 200), abs=2)
    half = render_one(compositor, (200, 60, 30, 255), [flt("lut", look="file", file=str(path), amount=50)])
    assert half[:3] == pytest.approx((115, 60, 115), abs=2)
    assert render_one(compositor, (200, 60, 30, 255), [flt("lut", look="file", file=str(tmp_path / "x.cube"))])[:3] \
        == (200, 60, 30)  # a missing file leaves the picture alone
    assert str(tmp_path / "x.cube") in compositor.load_errors


def test_gpu_shapes_cut_the_corners(compositor):
    img = np.full((64, 96, 4), (200, 100, 50, 255), np.uint8)
    for shape in ("circle", "hexagon", "leaf", "heart", "star"):
        out = render_full(compositor, img, [flt("shape", shape=shape)])
        assert out[32, 48, 3] == 255, shape  # the middle stays
        assert out[1, 1, 3] == 0 and out[62, 94, 3] == 0, shape  # corners are cut away
        assert out[32, 4, 3] == 0, shape  # sits in the middle at the short side's size
    out = render_full(compositor, img, [flt("shape", shape="circle", stretch=True)])
    assert out[32, 4, 3] == 255  # an ellipse reaches the sides
    out = render_full(compositor, img, [flt("shape", shape="rounded", roundness=50)])
    assert out[1, 1, 3] == 0 and out[32, 1, 3] == 255 and out[1, 48, 3] == 255
    out = render_full(compositor, img, [flt("shape", shape="circle", border=4, border_color="#00ff00")])
    assert tuple(out[32, 18]) == (0, 255, 0, 255) and tuple(out[32, 48][:3]) == (200, 100, 50)


def test_gpu_shape_from_picture(compositor, tmp_path):
    from PIL import Image

    pic = np.zeros((10, 10, 4), np.uint8)
    pic[:, :5] = (255, 255, 255, 255)  # keep the left half
    Image.fromarray(pic).save(tmp_path / "half.png")
    img = np.full((32, 32, 4), (200, 100, 50, 255), np.uint8)
    out = render_full(compositor, img, [flt("shape", shape="image", mask_file=str(tmp_path / "half.png"))])
    assert out[16, 4, 3] == 255 and out[16, 28, 3] == 0
    opaque = np.zeros((10, 10, 3), np.uint8)
    opaque[:, 5:] = 255  # no transparency: white keeps (the right half)
    Image.fromarray(opaque).save(tmp_path / "white.png")
    out = render_full(compositor, img, [flt("shape", shape="image", mask_file=str(tmp_path / "white.png"))])
    assert out[16, 4, 3] == 0 and out[16, 28, 3] == 255


def test_new_video_filter_settings():
    fx = video_effects([flt("blur", size=6.0)])
    assert fx.active and fx.blur == 6.0 and fx.pixelate == 0.0
    assert video_effects([flt("blur", mode="pixelate", size=10.0)]).pixelate == 10.0
    assert not video_effects([flt("blur", size=0.0)]).active
    assert video_effects([flt("color_key", key_color="#ff00ff")]).key_mode == 3
    halos = video_effects([flt("stroke"), flt("shadow"), flt("glow")]).halos
    assert [h.mode for h in halos] == [1, 1, 2] and halos[0].offset == (8.0, 8.0)  # shadow, glow, then stroke
    assert not video_effects([flt("shadow", opacity=0)]).active


def render_small_item(comp, filters, colour=(200, 100, 50, 255)):
    """A 16x16 square in the middle of a 48x48 canvas."""
    canvas = Canvas(id="f", width=48, height=48)
    comp.upload("src", np.full((16, 16, 4), colour, dtype=np.uint8))
    fx = video_effects(filters)
    comp.effects = {"src": fx} if fx.active else {}
    scene = Scene(id="s", name="s", canvas="f", items=[SceneItem(source="src", x=16, y=16)])
    comp.render(canvas, scene, {"src": (16, 16)})
    out = np.frombuffer(comp.read("f"), dtype=np.uint8).reshape(48, 48, 4).copy()
    comp.effects = {}
    return out


def test_gpu_stroke_shadow_and_glow_draw_outside_the_source(compositor):
    plain = render_small_item(compositor, [])
    assert plain[24, 13, 3] == 0  # nothing left of the square
    out = render_small_item(compositor, [flt("stroke", width=4.0, color="#00ff00")])
    assert tuple(out[24, 13]) == (0, 255, 0, 255)  # a green ring just outside
    assert tuple(out[24, 24][:3]) == (200, 100, 50)  # the source still on top
    assert out[24, 6, 3] == 0  # and nothing far away
    out = render_small_item(compositor, [flt("shadow", offset_x=6.0, offset_y=6.0, blur=0.0, opacity=100)])
    assert out[35, 35, 3] > 200 and out[35, 35, 0] < 10  # a dark copy, down and to the right
    assert out[13, 13, 3] == 0
    out = render_small_item(compositor, [flt("glow", size=8.0, color="#ff0000")])
    r, g, b, a = (int(v) for v in out[24, 12])
    assert a > 40 and abs(r - a) <= 2 and g < 5 and b < 5  # red light round the edge, fading out
    assert out[24, 12, 3] > out[24, 9, 3]


def test_gpu_blur_pixelate_and_color_key(compositor):
    img = np.zeros((16, 32, 4), np.uint8)
    img[:, :16] = (255, 0, 0, 255)
    img[:, 16:] = (0, 0, 255, 255)
    out = render_full(compositor, img, [flt("blur", size=4.0)])
    assert 40 < out[8, 16, 0] < 215 and 40 < out[8, 16, 2] < 215  # the edge is soft
    assert out[8, 8, 0] > 240  # far from it: as it was
    out = render_full(compositor, img, [flt("blur", mode="pixelate", size=8.0)])
    assert tuple(out[3, 2]) == tuple(out[6, 6])  # one flat block
    assert render_one(compositor, (255, 0, 255, 255), [flt("color_key", key_color="#ff00ff")])[3] == 0
    assert render_one(compositor, (0, 200, 0, 255), [flt("color_key", key_color="#ff00ff")])[3] == 255
