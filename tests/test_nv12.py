import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl")
av = pytest.importorskip("av")

from sprigcast.core.scene import Canvas, Scene, SceneItem  # noqa: E402


@pytest.fixture(scope="module")
def compositor():
    from sprigcast.render.compositor import Compositor

    try:
        comp = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    yield comp
    comp.release()


def _picture(compositor, w=64, h=36):
    rng = np.random.default_rng(3)
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[: h // 2, : w // 2] = (255, 40, 40, 255)  # red top-left quarter
    img[h // 2 :, w // 2 :] = (40, 200, 255, 255)  # light blue bottom-right
    img[: h // 2, w // 2 :, :3] = rng.integers(0, 255, (h // 2, w - w // 2, 3))
    img[: h // 2, w // 2 :, 3] = 255
    compositor.upload("pic", img)
    canvas = Canvas(id="c", width=w, height=h)
    scene = Scene(id="s", name="S", canvas="c", items=[SceneItem(source="pic")])
    compositor.render(canvas, scene, {"pic": (w, h)})
    return canvas


def _swscale_nv12(rgba: bytes, w, h, out_w, out_h) -> np.ndarray:
    from av.video.reformatter import ColorRange, Colorspace

    frame = av.VideoFrame.from_ndarray(np.frombuffer(rgba, np.uint8).reshape(h, w, 4), format="rgba")
    out = frame.reformat(width=out_w, height=out_h, format="nv12", dst_colorspace=Colorspace.ITU709,
                         dst_color_range=ColorRange.MPEG, interpolation="BILINEAR")
    y = np.frombuffer(out.planes[0], np.uint8).reshape(out_h, out.planes[0].line_size)[:, :out_w]
    uv = np.frombuffer(out.planes[1], np.uint8).reshape(out_h // 2, out.planes[1].line_size)[:, :out_w]
    return np.concatenate([y.ravel(), uv.ravel()])


def test_gpu_nv12_matches_the_cpu_conversion_at_canvas_size(compositor):
    canvas = _picture(compositor)
    gpu = np.frombuffer(compositor.read_nv12("c", (64, 36)), np.uint8)
    cpu = _swscale_nv12(compositor.read("c"), 64, 36, 64, 36)
    assert gpu.size == 64 * 36 * 3 // 2
    luma = np.abs(gpu[: 64 * 36].astype(int) - cpu[: 64 * 36])
    assert luma.max() <= 2  # same picture, same BT.709 limited-range maths
    y = gpu[: 64 * 36].reshape(36, 64)
    assert y[0, 0] == pytest.approx(16 + 219 * (0.2126 + 0.7152 * 40 / 255 + 0.0722 * 40 / 255), abs=2)  # red, top row
    chroma = np.abs(gpu[64 * 36 :].astype(int) - cpu[64 * 36 :])
    assert np.median(chroma) <= 2  # edges differ slightly: different chroma filters


def test_gpu_nv12_scales_on_the_way(compositor):
    canvas = _picture(compositor)
    data = compositor.read_nv12(canvas.id, (32, 18))
    assert len(data) == 32 * 18 * 3 // 2
    y = np.frombuffer(data, np.uint8)[: 32 * 18].reshape(18, 32)
    assert abs(int(y[2, 2]) - 90) <= 2 and abs(int(y[15, 29]) - 162) <= 2  # red and light blue, still in place


@pytest.mark.skipif("libx264" not in av.codecs_available, reason="needs libx264")
def test_gpu_frames_record_with_the_right_colours(compositor, tmp_path):
    import time

    from sprigcast.output.recorder import Recorder
    from sprigcast.render.engine import Frame

    w, h = 64, 64
    compositor.upload("pink", np.full((h, w, 4), (232, 164, 184, 255), dtype=np.uint8))
    canvas = Canvas(id="p", width=w, height=h)
    compositor.render(canvas, Scene(id="s", name="S", canvas="p", items=[SceneItem(source="pink")]), {"pink": (w, h)})
    rec = Recorder(tmp_path / "gpu.mkv", w, h, 30, 2000, "libx264", 0.0)
    assert rec.gpu_size == (64, 64)
    data = compositor.read_nv12("p", rec.gpu_size)
    rec.start()
    for i in range(10):
        rec.push_frame(Frame("p", w, h, data, i / 30, "nv12"))
        time.sleep(0.002)
    rec.stop()
    assert rec.error is None and rec.frames == 10
    with av.open(str(tmp_path / "gpu.mkv")) as f:
        pixel = next(f.decode(video=0)).to_ndarray(format="rgb24")[32, 32]
    assert np.allclose(pixel, (232, 164, 184), atol=4)


def test_scaled_preview_matches_the_canvas_shrunk(compositor):
    canvas = _picture(compositor, 128, 72)
    full = np.frombuffer(compositor.read("c"), np.uint8).reshape(72, 128, 4).astype(float)
    small = np.frombuffer(compositor.read_scaled("c", (32, 18)), np.uint8).reshape(18, 32, 4).astype(float)
    box = full.reshape(18, 4, 32, 4, 4).mean(axis=(1, 3))  # the same picture averaged 4x4
    # Same orientation and colours (top-left red, bottom-right light blue); the noisy quarter averages out
    assert abs(small[2, 2] - (255, 40, 40, 255)).max() < 2 and abs(small[15, 29] - (40, 200, 255, 255)).max() < 2
    assert np.abs(small - box).mean() < 25
    assert canvas.width == 128


def test_engine_picks_preview_sizes():
    from sprigcast.render.engine import Engine

    eng = Engine.__new__(Engine)
    eng.preview_sizes = {"horizontal": (600, 338), "vertical": (1080, 1920)}
    assert eng._preview_size("horizontal", (1920, 1080)) == (720, 405)  # rounded up in 1/16 steps
    assert eng._preview_size("vertical", (1080, 1920)) is None  # shown at full size: no shrinking
    assert eng._preview_size("next:horizontal", (1920, 1080)) is None  # size unknown


def test_wait_free_reads_hand_back_the_previous_frame(compositor):
    canvas = _picture(compositor)
    want_nv12 = bytes(compositor.read_nv12("c", (32, 18)))
    want_small = compositor.read_scaled("c", (32, 18))
    assert compositor.read_nv12_later("c", (32, 18), "first", 1.0) == (None, None)  # copying, nothing back yet
    assert compositor.read_scaled_later("c", (32, 18), "first", 1.0) == (None, None)
    compositor.upload("pic", np.zeros((canvas.height, canvas.width, 4), dtype=np.uint8))  # next frame differs
    compositor.render(canvas, Scene(id="s", name="S", canvas="c", items=[SceneItem(source="pic")]), {"pic": (64, 36)})
    data, tag = compositor.read_nv12_later("c", (32, 18), "second", 1.03)
    assert tag == "first" and bytes(data) == want_nv12  # the first frame, copied without waiting
    data, tag = compositor.read_scaled_later("c", (32, 18), "second", 1.03)
    assert tag == "first" and data == want_small
    data, tag = compositor.read_nv12_later("c", (32, 18), "third", 5.0)
    assert (data, tag) == (None, None)  # "second" waited too long (a hidden preview): thrown away


def test_a_full_size_copy_also_comes_back_a_tick_late(compositor):
    _picture(compositor)
    want = compositor.read("c")
    assert compositor.read_full_later("c", "first", 1.0) == (None, None)
    data, tag = compositor.read_full_later("c", "second", 1.03)
    assert tag == "first" and bytes(data) == want
    compositor.read_scaled_later("c", (32, 18), "small", 1.03)  # a small preview beside it keeps the full copy
    assert compositor.read_full_later("c", "third", 1.06)[1] == "second"


class _Buffer:
    def __init__(self):
        self.data = b""

    def read(self):
        return self.data

    def release(self):
        pass


class _Ctx:
    def buffer(self, reserve):
        return _Buffer()


class _Fences:
    fences_ok = True

    def __init__(self):
        self.made, self.finished, self.forgotten = 0, set(), []

    def fence(self):
        self.made += 1
        return self.made

    def done(self, sync):
        return sync in self.finished

    def forget(self, sync):
        self.forgotten.append(sync)


def test_a_copy_the_card_hasnt_finished_is_left_for_a_later_tick_instead_of_waited_for():
    from sprigcast.render.compositor import _Pbos

    fences = _Fences()
    pbos = _Pbos(_Ctx(), 16, fences)

    def fill(tag, now):
        pbos.fill(tag, now, lambda buffer: setattr(buffer, "data", tag.encode()))

    fill("a", 1.0)
    assert pbos.take(1.0) == (None, None)  # only this tick's
    fill("b", 1.016)
    assert pbos.take(1.016) == (None, None) and pbos.late == 1  # "a" isn't copied yet: no waiting for it
    fences.finished.add(1)
    fill("c", 1.033)
    assert pbos.take(1.033) == (b"a", "a") and fences.forgotten == [1]  # a tick later, nothing lost
    fill("d", 1.05)  # the card is behind on all three ("b", "c", "d"): the next fill makes room
    fill("e", 1.066)
    assert 2 in fences.forgotten  # "b" gave way
    fences.finished.update({3, 4})
    assert pbos.take(1.066) == (b"c", "c")
    pbos.release()
    assert sorted(fences.forgotten) == [1, 2, 3, 4, 5]


def test_without_fences_the_previous_frame_is_fetched_as_before():
    from sprigcast.render.compositor import _Pbos

    pbos = _Pbos(_Ctx(), 16, None)
    pbos.fill("a", 1.0, lambda buffer: setattr(buffer, "data", b"a"))
    pbos.fill("b", 1.016, lambda buffer: setattr(buffer, "data", b"b"))
    assert pbos.take(1.016) == (b"a", "a") and pbos.late == 0


def test_behind_means_an_older_copy_than_the_last_isnt_done():
    from sprigcast.render.compositor import _Pbos

    fences = _Fences()
    pbos = _Pbos(_Ctx(), 16, fences)
    pbos.fill("a", 1.0, lambda buffer: None)
    assert not pbos.behind()  # the last copy still going is normal (one tick in flight)
    pbos.fill("b", 1.016, lambda buffer: None)
    assert pbos.behind()  # "a" isn't done a tick later: the card is busy
    fences.finished.add(1)
    assert not pbos.behind()


def test_an_optional_preview_copy_isnt_asked_for_while_the_card_is_behind(compositor, monkeypatch):
    _picture(compositor)
    compositor.read_scaled_later("c", (32, 18), "a", 1.0, optional=True)
    target = compositor.scaled_targets[("c", (32, 18))]
    monkeypatch.setattr(target.pbos, "behind", lambda: True)
    draws = []
    real = compositor._draw_scaled
    monkeypatch.setattr(compositor, "_draw_scaled", lambda *a: draws.append(a) or real(*a))
    compositor.read_scaled_later("c", (32, 18), "b", 1.016, optional=True)
    assert draws == [] and target.pbos.late == 1  # nothing new drawn or queued
    compositor.read_scaled_later("c", (32, 18), "c", 1.033)  # an output's copy is always made
    assert len(draws) == 1


def test_stuck_means_a_copy_older_than_the_limit_isnt_done():
    from sprigcast.render.compositor import _Pbos

    fences = _Fences()
    pbos = _Pbos(_Ctx(), 16, fences)
    pbos.fill("a", 1.0, lambda buffer: None)
    assert not pbos.stuck(1.05, 0.1)  # busy, not stuck
    assert pbos.stuck(1.2, 0.1)
    fences.finished.add(1)
    assert not pbos.stuck(1.2, 0.1)


def test_an_output_copy_isnt_asked_for_while_the_card_is_stuck(compositor, monkeypatch):
    _picture(compositor)
    compositor.read_nv12_later("c", (32, 18), "a", 1.0)
    target = compositor.nv12_targets[("c", (32, 18))]
    monkeypatch.setattr(target.pbos, "stuck", lambda now, after_s=0.1: True)
    draws = []
    real = compositor._draw_nv12
    monkeypatch.setattr(compositor, "_draw_nv12", lambda *a: draws.append(a) or real(*a))
    compositor.read_nv12_later("c", (32, 18), "b", 1.3)
    assert draws == [] and target.pbos.late == 1
    monkeypatch.setattr(target.pbos, "stuck", lambda now, after_s=0.1: False)
    compositor.read_nv12_later("c", (32, 18), "c", 1.33)
    assert len(draws) == 1
