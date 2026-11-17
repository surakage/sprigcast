"""The plugin guide's example (docs/plugins/shoutout.py) loads and works as the guide says."""

import sys
from pathlib import Path

import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import ApiError, NativeApi, unregister_actions
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, source_class, unregister_module
from sprigcast.plugins import load_plugin_file

from test_control_api import FakeHost, _collection

EXAMPLE = Path(__file__).resolve().parents[1] / "docs" / "plugins" / "shoutout.py"


@pytest.fixture
def shoutout():
    assert load_plugin_file(EXAMPLE) is None
    yield
    unregister_module("sprigcast_plugin_shoutout")
    unregister_actions("sprigcast_plugin_shoutout")
    sys.modules.pop("sprigcast_plugin_shoutout", None)


def test_the_example_adds_a_source_actions_events_and_a_hotkey(shoutout):
    assert source_class("shoutout").label == "Shout-out"
    col = _collection()
    col.sources.append(SourceConfig(id="so", kind="shoutout", name="Shout-out", settings={}))
    api = NativeApi(FakeHost(col))
    sent = []
    api.plugin_event = lambda name, data: sent.append((name, data))

    assert "shoutout.show" in api.all_actions()
    assert api.run("shoutout.show", {"name": "@Ana"}) == {"name": "Ana"}
    assert col.sources[-1].settings["name"] == "Ana"
    assert sent == [("shoutout.shown", {"name": "Ana"})]
    card = create_source("shoutout", "so", {**source_class("shoutout").defaults, "name": "Ana"})
    assert card._current_text() == "Go follow Ana!"

    with pytest.raises(ApiError) as err:
        api.run("shoutout.show", {"name": " "})
    assert err.value.code == "bad_request"

    assert ("plugin:shoutout:clear", "Shout-out: Clear") in plugin_hooks.hotkey_actions(col)
    assert plugin_hooks.run_hotkey(api, "plugin:shoutout:clear")
    assert col.sources[-1].settings["name"] == ""


def test_the_example_says_what_to_add_first(shoutout):
    with pytest.raises(ApiError) as err:
        NativeApi(FakeHost(_collection())).run("shoutout.clear", {})
    assert err.value.code == "not_found"
