"""Your own plugins' sources in the plugin helper (app/plugin_helper.py): drawn in another process, so a broken one
can't freeze or crash SprigCast."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from sprigcast import plugins
from sprigcast.app import plugin_helper
from sprigcast.core import source as source_mod
from sprigcast.core.source import create_source, source_class

PLUGIN = '''
import os

import numpy as np

from sprigcast.core.source import Source, register


@register("helper_test_{tag}")
class Painted(Source):
    label = "Helper test"
    defaults = {{"mode": "red"}}
    channel_order = "BGRA"

    def __init__(self, source_id, settings):
        super().__init__(source_id, settings)
        self.drawn = None

    @property
    def size(self):
        return (8, 4)

    def video_frame(self):
        mode = self.settings.get("mode")
        if mode == "crash":
            os._exit(3)  # takes the whole helper down
        if mode == "raise":
            raise RuntimeError("plugin bug")
        if self.drawn == mode:
            return None
        self.drawn = mode
        frame = np.zeros((4, 8, 4), np.uint8)
        frame[..., 2] = 255 if mode == "red" else 0  # B, G, R, A: red
        frame[..., 0] = 255 if mode == "blue" else 0
        frame[..., 3] = 255
        return frame
'''


def wait_for(check, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return check()


@pytest.fixture
def helper(monkeypatch, tmp_path):
    monkeypatch.delenv(plugin_helper.ENV_OFF, raising=False)
    monkeypatch.setattr(plugin_helper, "RESTART_AFTER_S", 0.2)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    made = []

    def load(tag: str) -> str:
        path = tmp_path / f"helper_test_{tag}.py"
        path.write_text(PLUGIN.format(tag=tag), encoding="utf-8")
        assert plugins.load_plugin_file(path) is None
        made.append(f"sprigcast_plugin_{path.stem}")
        return f"helper_test_{tag}"

    yield load
    plugin_helper.shutdown()
    for module in made:
        source_mod.unregister_module(module)
        plugin_helper.forget(module)


def test_pictures_are_made_straight_rgba():
    bgra = np.array([[[10, 20, 30, 255]]], np.uint8)
    assert plugin_helper._to_rgba(bgra, "BGRA").tolist() == [[[30, 20, 10, 255]]]
    assert plugin_helper._to_rgba(np.array([[[10, 20, 30, 0]]], np.uint8), "BGRX").tolist() == [[[30, 20, 10, 255]]]
    half = np.array([[[50, 25, 0, 128]]], np.uint8)  # premultiplied B, G, R at half alpha
    assert plugin_helper._to_rgba(half, "pBGRA").tolist() == [[[0, 49, 99, 128]]]
    assert plugin_helper._to_rgba(np.array([[[1, 2, 3]]], np.uint8), "RGB").tolist() == [[[1, 2, 3, 255]]]


def test_your_own_plugins_sources_run_in_the_helper(helper):
    kind = helper("own")
    src = create_source(kind, "p1", {"mode": "red"})
    assert isinstance(src, plugin_helper.HelperSource)
    assert source_class(kind).__name__ == "Painted" and src.label == "Helper test"  # the UI still sees the plugin
    src.start()
    try:
        got = []
        assert wait_for(lambda: got.append(src.video_frame()) or got[-1] is not None)
        assert got[-1][0, 0].tolist() == [255, 0, 0, 255] and src.size == (8, 4)
        assert src.engine_player is not None  # the native engine takes the pictures from the block itself
        src.update({"mode": "blue"})
        assert wait_for(lambda: got.append(src.video_frame()) or (got[-1] is not None and got[-1][0, 0, 2] == 255))
    finally:
        src.stop()


def test_a_plugin_that_raises_only_shows_an_error(helper):
    kind = helper("raises")
    src = create_source(kind, "p2", {"mode": "raise"})
    src.start()
    try:
        assert wait_for(lambda: src.status.startswith("error: ") and "plugin bug" in src.status)
        src.update({"mode": "red"})
        assert wait_for(lambda: src.video_frame() is not None)
        assert wait_for(lambda: src.status == "ok")
    finally:
        src.stop()


def test_a_plugin_that_crashes_the_helper_doesnt_take_sprigcast_down(helper):
    kind = helper("crash")
    src = create_source(kind, "p3", {"mode": "red"})
    src.start()
    try:
        assert wait_for(lambda: src.video_frame() is not None)
        host = plugin_helper.host()
        src.update({"mode": "crash"})
        assert wait_for(lambda: host.starts >= 2)  # started again, with the same sources
        src.update({"mode": "red"})
        assert wait_for(lambda: src.video_frame() is not None)
    finally:
        src.stop()


def test_store_plugins_and_the_switch_keep_sources_here(helper, monkeypatch):
    monkeypatch.setattr("sprigcast.plugin_store.is_store_file", lambda path: True)
    kind = helper("store")
    assert not isinstance(create_source(kind, "s", {}), plugin_helper.HelperSource)
    monkeypatch.setattr("sprigcast.plugin_store.is_store_file", lambda path: False)
    monkeypatch.setenv(plugin_helper.ENV_OFF, "1")
    kind = helper("off")
    assert not isinstance(create_source(kind, "o", {}), plugin_helper.HelperSource)
    assert Path(plugins.__file__).exists()
