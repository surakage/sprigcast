"""The starter plugin (docs/plugins/hello_sprig.py) loads and works as its comments say."""

import sys
from pathlib import Path

import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import ApiError, NativeApi, unregister_actions
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds, source_class, unregister_module
from sprigcast.plugins import load_plugin_file

from test_control_api import FakeHost, _collection

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "plugins" / "hello_sprig.py"
MODULE = "sprigcast_plugin_hello_sprig"


@pytest.fixture
def hello():
    before = set(known_kinds())
    assert load_plugin_file(EXAMPLE) is None
    yield before
    unregister_module(MODULE)
    unregister_actions(MODULE)
    sys.modules.pop(MODULE, None)


def test_its_kind_is_its_own(hello):
    assert "example_hello_sprig" not in hello  # never the same id as a built-in or store plugin
    assert source_class("example_hello_sprig").label == "Hello sprig"


def test_it_draws_once_and_again_only_when_settings_change(hello):
    cls = source_class("example_hello_sprig")
    card = create_source("example_hello_sprig", "h1", dict(cls.defaults))
    frame = card.video_frame()
    assert frame is not None and frame.shape == (160, 640, 4) and frame[..., 3].max() == 255
    assert card.video_frame() is None  # nothing changed: nothing drawn
    card.update({**cls.defaults, "name": "Ana", "color": "not a colour"})  # a bad colour falls back
    assert card.video_frame() is not None


def test_the_action_and_hotkey_wave(hello):
    col = _collection()
    col.sources.append(SourceConfig(id="h1", kind="example_hello_sprig", name="Hello sprig", settings={}))
    api = NativeApi(FakeHost(col))
    sent = []
    api.plugin_event = lambda name, data: sent.append((name, data))

    assert api.run("hello_sprig.wave", {}) == {"waves": 1}
    assert col.sources[-1].settings["waves"] == 1
    assert sent == [("hello_sprig.waved", {"waves": 1})]

    assert ("plugin:hello_sprig:wave", "Hello sprig: Wave") in plugin_hooks.hotkey_actions(col)
    assert plugin_hooks.run_hotkey(api, "plugin:hello_sprig:wave")
    assert col.sources[-1].settings["waves"] == 2


def test_waving_without_a_card_says_what_to_add(hello):
    with pytest.raises(ApiError) as err:
        NativeApi(FakeHost(_collection())).run("hello_sprig.wave", {})
    assert err.value.code == "not_found"
