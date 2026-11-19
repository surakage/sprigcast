"""The native engine's Python side (sprigcast.sprigcore), and pixel checks of the engine against a reference
drawn here in NumPy. The pixel checks need sprigcore.dll (Windows; CI builds it, see .github/workflows/core.yml)
and run on Windows' software renderer, so they give the same answer on every PC."""

import ctypes
import sys
import time

import numpy as np
import pytest

from sprigcast import sprigcore


@pytest.fixture
def fresh(monkeypatch):
    """A clean slate: the DLL is looked up again and nothing in the environment chooses an engine."""
    monkeypatch.delenv("SPRIGCAST_ENGINE", raising=False)
    monkeypatch.setattr(sprigcore, "_software", [])
    sprigcore._lib.cache_clear()
    yield monkeypatch
    sprigcore._lib.cache_clear()


def test_the_native_engine_is_the_default_on_windows(fresh):
    fresh.setattr(sys, "platform", "linux")
    assert sprigcore.engine_choice() == "python"  # not built here
    fresh.setattr(sys, "platform", "win32")
    assert sprigcore.engine_choice() == "native"
    fresh.setenv("SPRIGCAST_ENGINE", "rust")
    assert sprigcore.engine_choice() == "native"  # not a choice: the default
    fresh.setenv("SPRIGCAST_ENGINE", " Python ")
    assert sprigcore.engine_choice() == "python"
    fresh.setattr(sys, "platform", "linux")
    fresh.setenv("SPRIGCAST_ENGINE", "native")
    assert sprigcore.engine_choice() == "native"
    fresh.setattr(sprigcore, "_python_only", False)
    sprigcore.use_python_engine()  # safe mode
    assert sprigcore.engine_choice() == "python" and not sprigcore.use_native()


def test_only_a_software_renderer_means_the_python_engine(fresh):
    basic = {"name": "Microsoft Basic Render Driver", "vendor_id": 0x1414, "device_id": 0x8C, "software": False}
    fresh.setattr(sys, "platform", "win32")
    fresh.setattr(sprigcore, "adapters", lambda: [basic, dict(basic, software=True)])
    assert sprigcore.engine_choice() == "python"
    assert sprigcore.why_python() == "no graphics driver here, only a software renderer"
    fresh.setenv("SPRIGCAST_ENGINE", "native")
    assert sprigcore.engine_choice() == "native"  # still asked for by name (CI's engine checks)
    fresh.delenv("SPRIGCAST_ENGINE")
    fresh.setattr(sprigcore, "_software", [])
    card = {"name": "NVIDIA GeForce", "vendor_id": 0x10DE, "device_id": 0x2684, "software": False}
    fresh.setattr(sprigcore, "adapters", lambda: [card, basic])
    assert sprigcore.engine_choice() == "native"


def test_a_missing_dll_means_the_python_engine(fresh, tmp_path):
    fresh.setenv("SPRIGCAST_CORE_DLL", str(tmp_path / "nope.dll"))
    fresh.setenv("SPRIGCAST_ENGINE", "native")
    assert sprigcore.dll_path() == tmp_path / "nope.dll"
    assert not sprigcore.available()
    assert not sprigcore.use_native()
    assert sprigcore.adapters() == []
    assert sprigcore.drain_log() == 0
    with pytest.raises(sprigcore.NativeError):
        sprigcore.Device(warp=True)
    with pytest.raises(sprigcore.NativeError):
        sprigcore.Engine(warp=True)


def test_a_dll_that_isnt_one_is_refused(fresh, tmp_path, monkeypatch):
    bad = tmp_path / "sprigcore.dll"
    bad.write_bytes(b"not a dll")
    fresh.setenv("SPRIGCAST_CORE_DLL", str(bad))
    monkeypatch.setattr(sprigcore.sys, "platform", "win32")
    assert not sprigcore.available()


def test_the_dll_is_looked_for_in_the_package(fresh):
    fresh.delenv("SPRIGCAST_CORE_DLL", raising=False)
    path = sprigcore.dll_path()
    assert path.parent.name == "engine" and path.parent.parent.name == "sprigcast" and path.name == "sprigcore.dll"


# ---- with the engine -------------------------------------------------------------------------------------------

needs_engine = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


@pytest.fixture
def canvas():
    with sprigcore.Device(warp=True) as dev, sprigcore.Canvas(dev, 64, 36) as c:
        yield c


def _read(c) -> np.ndarray:
    return np.frombuffer(c.read_bgra(), np.uint8).reshape(c.height, c.width, 4).astype(int)


def _picture(w, h, seed=7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (h, w, 4), dtype=np.uint8)
    img[..., 3] = 255
    return img


@needs_engine
def test_cards_and_device():
    cards = sprigcore.adapters()
    assert cards and all(c["name"] for c in cards)
    with sprigcore.Device(warp=True) as dev:
        info = dev.info()
        assert info["software"] and info["feature_level"] >= "10.0"


@needs_engine
def test_clear(canvas):
    canvas.clear(1.0, 0.0, 0.0)
    img = _read(canvas)
    assert (img[..., 2] == 255).all() and (img[..., :2] == 0).all() and (img[..., 3] == 255).all()


@needs_engine
def test_drawing_at_its_own_size_is_exact(canvas):
    pic = _picture(20, 10)
    canvas.clear(0, 0, 0)
    canvas.draw_bgra(pic.tobytes(), 20, 10, 5, 7, 20, 10)
    img = _read(canvas)
    assert (img[7:17, 5:25] == pic).all()
    assert (img[:7] == [0, 0, 0, 255]).all() and (img[:, :5] == [0, 0, 0, 255]).all()


@needs_engine
def test_scaling_matches_bilinear(canvas):
    """2x up-scaling against a NumPy bilinear reference (pixel centres, edges clamped)."""
    pic = _picture(16, 9, seed=3).astype(float)
    canvas.clear(0, 0, 0)
    canvas.draw_bgra(pic.astype(np.uint8).tobytes(), 16, 9, 0, 0, 32, 18)
    got = _read(canvas)[:18, :32]
    ys = np.clip((np.arange(18) + 0.5) / 2 - 0.5, 0, 8)
    xs = np.clip((np.arange(32) + 0.5) / 2 - 0.5, 0, 15)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, 8), np.minimum(x0 + 1, 15)
    fy, fx = (ys - y0)[:, None, None], (xs - x0)[None, :, None]
    top = pic[y0][:, x0] * (1 - fx) + pic[y0][:, x1] * fx
    bottom = pic[y1][:, x0] * (1 - fx) + pic[y1][:, x1] * fx
    want = np.rint(top * (1 - fy) + bottom * fy)
    assert np.abs(got - want).max() <= 2


@needs_engine
def test_opacity_blends_over(canvas):
    pic = _picture(8, 8, seed=11)
    canvas.clear(1.0, 0.0, 0.0)  # red underneath (clear takes r, g, b; the picture is B, G, R)
    canvas.draw_bgra(pic.tobytes(), 8, 8, 0, 0, 8, 8, opacity=0.25)
    got = _read(canvas)[:8, :8, :3]
    want = np.rint(pic[..., :3] * 0.25 + np.array([0, 0, 255]) * 0.75)
    assert np.abs(got - want).max() <= 2


@needs_engine
def test_bad_calls_raise_and_log(canvas):
    with pytest.raises(sprigcore.NativeError) as e:
        canvas.draw_bgra(b"\0" * 16, 4, 4, 0, 0, 4, 4)
    assert e.value.code == -1
    dev = canvas.device
    with pytest.raises(sprigcore.NativeError):
        sprigcore.Canvas(dev, 0, 10)


@needs_engine
def test_closing_the_device_closes_its_canvases():
    dev = sprigcore.Device(warp=True)
    c = sprigcore.Canvas(dev, 8, 8)
    dev.close()
    with pytest.raises(sprigcore.NativeError):
        c.clear(0, 0, 0)
    c.close()
    dev.close()


def test_engine_stats_match_the_header():
    """sc_engine_stats: four 64-bit counts, four doubles, eight ints (include/sprigcore.h)."""
    assert ctypes.sizeof(sprigcore.EngineStats) == 4 * 8 + 4 * 8 + 8 * 4


@needs_engine
def test_an_engine_that_gave_up_says_so_and_refuses_work():
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        eng.set_canvas(0, 8, 8)
        deadline = time.monotonic() + 20
        while not eng.stats()["faulted"] and time.monotonic() < deadline:
            before = eng.stats()["rebuilds"]
            eng.debug_fault(3)
            while eng.stats()["rebuilds"] == before and not eng.stats()["faulted"] and time.monotonic() < deadline:
                time.sleep(0.02)
        assert eng.stats()["faulted"]
        with pytest.raises(sprigcore.NativeError) as e:
            eng.upload("a", _picture(4, 4))
        assert e.value.code == sprigcore.FAULTED
