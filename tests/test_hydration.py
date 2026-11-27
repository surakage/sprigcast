"""The Hydration store plugin (user, 2026-11-27): a "Time for a sip" card every so often while live, with a count."""

from types import SimpleNamespace

import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def hy(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "hydration")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    module = sys.modules["sprigcast_plugin_hydration"]
    module.SCHEDULE = module.Schedule()
    yield module
    plugin_hooks.output_changed("stream", None)
    plugin_store.remove(tmp_path / "plugins", "hydration")
    assert "hydration" not in known_kinds()


def test_it_comes_on_every_so_often_while_live_and_counts(hy):
    s, sched = {"every": 45, "show_for": 10}, hy.SCHEDULE
    assert not sched.tick(s, None, 100.0)  # off air: never
    assert not sched.tick(s, 0.0, 100.0)  # live, not yet
    assert sched.tick(s, 0.0, 45 * 60 + 1) and sched.sips == 1
    assert not sched.tick(s, 0.0, 45 * 60 + 11)  # shown for 10 s
    assert sched.tick(s, 0.0, 90 * 60 + 3) and sched.sips == 2
    assert not sched.tick(s, 0.0, 200 * 60)  # long past while off screen: not shown late, just put on
    assert sched.sips == 2 and sched.next_at == 200 * 60 + 45 * 60
    assert not sched.tick(s, 9999.0, 10000.0) and sched.sips == 0  # a new stream starts the count over
    assert hy.words(s, 0) == ("Time for a sip", "") and hy.words(s, 3) == ("Time for a sip", "Sip 3 this stream")
    assert hy.words({"count_sips": False, "label": "Water!"}, 3) == ("Time for a sip", "Water!")


def test_show_now_and_snooze(hy):
    from sprigcast.control.native import ApiError, _PLUGIN_ACTIONS

    api = SimpleNamespace(col=SimpleNamespace(sources=[]))
    with pytest.raises(ApiError):
        _PLUGIN_ACTIONS["hydrate.show"].run(api, {})
    api.col.sources = [SimpleNamespace(kind="hydration", settings={})]
    got = _PLUGIN_ACTIONS["hydrate.show"].run(api, {})
    assert got["showing"] and got["sips"] == 1
    got = _PLUGIN_ACTIONS["hydrate.snooze"].run(api, {"minutes": 10})
    assert not got["showing"] and got["nextInSeconds"] == 600
    with pytest.raises(ApiError):
        _PLUGIN_ACTIONS["hydrate.snooze"].run(api, {"minutes": "soon"})


def test_drawn_only_when_it_comes_on_or_goes(hy, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(hy.time, "monotonic", lambda: clock[0])
    source = create_source("hydration", "h", {"every": 5, "show_for": 10})
    clear = source.video_frame()
    assert clear is not None and not clear[..., 3].any()
    assert source.video_frame() is None
    plugin_hooks.output_changed("stream", 1000.0)
    clock[0] = 1000.0 + 5 * 60
    card = source.video_frame()
    assert card is not None and card.shape[0] == 220 and card[..., 3].any()
    assert source.video_frame() is None  # nothing changed
    clock[0] += 11
    gone = source.video_frame()
    assert gone is not None and not gone[..., 3].any()
