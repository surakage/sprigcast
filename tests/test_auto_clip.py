"""The Auto clip store plugin: spotting loud moments and chat bursts, saving suggestions from the clips
buffer, keeping and deleting them, the tidy-up after a few days, the API and the window."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from sprigcast import models, plugin_hooks, plugin_store
from sprigcast.chat import ChatMessage
from sprigcast.control.native import ApiError, NativeApi

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import FakeHost, _collection  # noqa: E402


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def ac(tmp_path, app):
    plugin = next(p for p in plugin_store.catalog() if p.id == "auto_clip")
    assert plugin.name == "Auto clip" and plugin.adds == "Tool"
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    models.set_folder(tmp_path / "settings" / "models")
    yield sys.modules["sprigcast_plugin_auto_clip"]
    models.set_folder(None)
    plugin_store.remove(tmp_path / "plugins", "auto_clip")
    assert not [fn for fn in plugin_hooks._LEVEL_WATCHERS if plugin_hooks._alive(fn)]
    assert not any(label == "Suggested clips…" for label, _ in plugin_hooks.tools())


class Hub:
    """The bits of chat/hub.py Auto clip uses."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _H(QObject):
            message = Signal(object)

        self.q = _H()
        self.message = self.q.message
        self.wanted = set()

    def want(self, reason, on):
        (self.wanted.add if on else self.wanted.discard)(reason)


class Host(FakeHost):
    def __init__(self, collection, folder):
        super().__init__(collection)
        self.folder = folder
        self.said = []
        self.hub = Hub()
        self.saves = 0
        self.fail = False

    def recordings_folder(self):
        return self.folder

    def save_clip_into(self, folder):
        if self.fail:
            raise RuntimeError("the encoder has stopped")
        self.saves += 1
        day = Path(folder) / "2026-11-16"
        day.mkdir(parents=True, exist_ok=True)
        out = []
        for shape in ("horizontal", "vertical"):
            path = day / f"SprigCast Replay {self.saves} {shape}.mkv"
            path.write_bytes(b"clip")
            out.append(path)
        return out

    def chat_hub(self):
        return self.hub

    def say(self, text, ms=6000):
        self.said.append(text)


@pytest.fixture
def setup(ac, tmp_path):
    col = _collection()
    col.recording.replay_seconds = 30
    host = Host(col, tmp_path / "Recordings")
    host.clips = True
    api = NativeApi(host)
    events = []
    api.plugin_event = lambda name, data: events.append((name, data))
    auto = ac.controller(api)
    auto._later = lambda seconds, fn: fn()  # no waiting in tests
    auto._in_background = lambda work, then: then(work())
    return ac, auto, api, host, events


def _talk(det, seconds, db, start=0.0, rate=15):
    """Feed a steady level; returns the first spike (dB over) and when, or (None, None)."""
    t = start
    for _ in range(int(seconds * rate)):
        t += 1 / rate
        over = det.feed(db, t)
        if over is not None:
            return over, t
    return None, t


def test_a_shout_over_normal_talking_is_a_spike_but_talking_is_not(ac):
    det = ac.SpikeDetector("medium")
    over, t = _talk(det, 40, -28)  # talking along
    assert over is None
    over, at = _talk(det, 2, -12, start=t)  # a shout, 16 dB up
    assert over is not None and over > 10 and at - t >= det.HOLD_S
    over, t = _talk(det, 1, -12, start=at)
    assert over is None  # one spike per shout


def test_a_short_knock_quiet_room_and_warm_up_never_count(ac):
    det = ac.SpikeDetector("high")
    over, t = _talk(det, 5, -40)
    over, t = _talk(det, 2, -10, start=t)  # still learning the usual level
    assert over is None
    det = ac.SpikeDetector("high")
    _, t = _talk(det, 30, -40)
    over, t = _talk(det, 0.2, -5, start=t)  # a desk knock: too short
    assert over is None
    det = ac.SpikeDetector("high")
    _, t = _talk(det, 30, -70)  # silence, then a little noise: under LOUD_DB
    over, _ = _talk(det, 2, -45, start=t)
    assert over is None


def test_sensitivity_changes_how_much_louder_it_takes(ac):
    for sens, expect in (("low", None), ("high", True)):
        det = ac.SpikeDetector(sens)
        _, t = _talk(det, 30, -28)
        over, _ = _talk(det, 2, -19, start=t)  # 9 dB up
        assert (over is not None) == bool(expect), sens


def test_a_game_that_gets_louder_and_stays_becomes_the_usual(ac):
    det = ac.SpikeDetector("medium")
    _, t = _talk(det, 30, -35)
    over, t = _talk(det, 2, -15, start=t)
    assert over is not None
    _, t = _talk(det, 60, -15, start=t)  # it stays that loud
    over, t = _talk(det, 2, -15, start=t)
    assert over is None
    _, t = _talk(det, 2, -30, start=t)  # quieter for a bit, then a real jump over the new usual
    over, _ = _talk(det, 2, 0, start=t)
    assert over is not None


def test_chat_bursts_against_its_usual_pace(ac):
    burst = ac.ChatBurst("medium")
    t = 0.0
    for _ in range(120 * 10):  # a calm chat: a message every 10 s, checked 10 times a second
        t += 0.1
        if round(t * 10) % 100 == 0:
            burst.message(t)
        assert burst.check(t) is None
    for i in range(12):  # 12 messages in 3 seconds
        burst.message(t + i * 0.25)
    assert burst.check(t + 3) >= 8
    assert burst.check(t + 3.1) is None
    # The deque never grows without a check
    for i in range(1000):
        burst.message(t + 100 + i)
    assert len(burst._at) <= burst.WINDOW + 1


def _levels(mic=-80.0, desk=-80.0):
    return {"mic": (mic + 3, mic), "desk": (desk + 3, desk), "master": (-10.0, -12.0)}


def _run(auto, seconds, start=0.0, **levels):
    t = start
    for _ in range(int(seconds * 15)):
        t += 1 / 15
        auto.on_levels(_levels(**levels), now=t)
    return t


def test_a_loud_moment_saves_a_suggestion_and_tells_apps(setup):
    ac, auto, api, host, events = setup
    t = _run(auto, 30, mic=-28)
    assert host.saves == 0
    t = _run(auto, 2, start=t, mic=-8)
    assert host.saves == 1
    items = auto.suggestions.items
    assert len(items) == 1 and items[0]["reason"] == "mic" and len(items[0]["files"]) == 2
    assert all(Path(f).parent.parent == host.folder / "Suggested clips" for f in items[0]["files"])
    assert events[-1][0] == "autoclip.suggested" and events[-1][1]["label"] == "Loud mic"
    assert host.said[-1] == "Suggested clip: Loud mic"
    index = json.loads((host.folder / "Suggested clips" / "suggested.json").read_text())
    assert index["clips"][0]["id"] == items[0]["id"]
    # Inside the gap a second shout is let go; after it, it counts again.
    t = _run(auto, 2, start=t, mic=-28)
    t = _run(auto, 2, start=t, mic=-8)
    assert host.saves == 1
    t = _run(auto, 60, start=t, mic=-28)
    t = _run(auto, 2, start=t, mic=-8)
    assert host.saves == 1  # still inside the 2-minute gap
    t = _run(auto, 70, start=t, mic=-28)
    _run(auto, 2, start=t, mic=-8)
    assert host.saves == 2  # a voice doesn't loop: the same shout counts again after the gap


def test_nothing_happens_while_clips_are_off_or_it_is_switched_off(setup):
    ac, auto, api, host, events = setup
    host.clips = False
    t = _run(auto, 30, mic=-28)
    _run(auto, 2, start=t, mic=-5)
    assert host.saves == 0 and auto.detectors["mic"].heard_s == 0  # not even learning
    host.clips = True
    auto.set(on=False)
    t = _run(auto, 30, mic=-28)
    _run(auto, 2, start=t, mic=-5)
    assert host.saves == 0
    auto.set(on=True, mic=False)
    t = _run(auto, 30, mic=-28)
    _run(auto, 2, start=t, mic=-5)
    assert host.saves == 0  # mic not listened to
    t = _run(auto, 30, desk=-35)
    _run(auto, 2, start=t, desk=-10)
    assert host.saves == 1 and auto.suggestions.items[-1]["reason"] == "game"


def test_music_never_counts_as_game(setup):
    from sprigcast.core.scene import SourceConfig

    ac, auto, api, host, events = setup
    api.col.sources.append(SourceConfig(id="spot", kind="app_audio", name="Spotify"))
    auto._groups_of = None
    groups = auto._source_groups()
    assert "spot" not in groups["game"] and "desk" in groups["game"] and "mic" in groups["mic"]


def test_a_failed_save_lets_the_next_moment_try_again(setup):
    ac, auto, api, host, events = setup
    host.fail = True
    assert auto.suggest("mic") is True
    assert not auto.pending and not auto.suggestions.items and not events


def test_chat_is_read_only_while_ticked_and_clips_are_ready(setup):
    ac, auto, api, host, events = setup
    _run(auto, 1, mic=-80)
    assert host.hub.wanted == set()
    auto.set(chat=True)
    assert host.hub.wanted == {"auto_clip"}
    host.hub.message.emit(ChatMessage(platform="twitch", user="a", text="hi"))
    host.hub.message.emit(ChatMessage(platform="twitch", user="me", text="hi", badges=("broadcaster",)))
    assert len(auto.detectors["chat"]._at) == 1  # your own messages don't count
    host.clips = False
    _run(auto, 0.2)
    assert host.hub.wanted == set()
    host.hub.message.emit(ChatMessage(platform="twitch", user="a", text="hi"))  # disconnected
    assert len(auto.detectors["chat"]._at) == 0


def test_keep_moves_to_clips_and_delete_removes(setup):
    ac, auto, api, host, events = setup
    auto.suggest("mic")
    auto.suggest("mark")
    a, b = auto.suggestions.items
    moved = auto.keep(a["id"])
    assert len(moved) == 2 and all(p.is_file() and p.parent.parent == host.folder / "Clips" for p in moved)
    assert not any(Path(f).exists() for f in a["files"])
    assert auto.delete(b["id"]) is True
    assert not any(Path(f).exists() for f in b["files"]) and auto.suggestions.items == []
    assert not any((host.folder / "Suggested clips").glob("*/"))  # empty day folders tidied
    assert auto.delete("nope") is False


def test_unkept_suggestions_go_after_the_days_and_at_most_are_held(ac, tmp_path):
    store = ac.Suggestions(tmp_path / "S")
    old = tmp_path / "S" / "d" / "old.mkv"
    new = tmp_path / "S" / "d" / "new.mkv"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    now = datetime(2026, 11, 16, 12, 0)
    store.add([old], "mic", when=now - timedelta(days=4))
    store.add([new], "game", when=now - timedelta(days=1))
    store.add([tmp_path / "gone.mkv"], "chat", when=now)  # deleted by hand
    assert store.expire(3, now) == 2
    assert [i["reason"] for i in store.items] == ["game"] and not old.exists() and new.exists()
    assert store.expire(0, now + timedelta(days=99)) == 0  # 0 days: kept until you delete
    for n in range(ac.MOST + 5):
        f = tmp_path / "S" / "d" / f"{n}.mkv"
        f.write_bytes(b"x")
        store.add([f], "mic", when=now)
    assert len(store.items) == ac.MOST and not (tmp_path / "S" / "d" / "0.mkv").exists()
    assert len({i["id"] for i in store.items}) == ac.MOST  # same second, still unique ids
    assert len(ac.Suggestions(tmp_path / "S").items) == ac.MOST  # read back


def test_a_broken_index_starts_empty(ac, tmp_path):
    (tmp_path / "S").mkdir()
    (tmp_path / "S" / "suggested.json").write_text("{nope", encoding="utf-8")
    assert ac.Suggestions(tmp_path / "S").items == []


def test_settings_are_kept_and_cleaned(ac):
    assert ac.load_settings() == ac.DEFAULTS
    ac.save_settings({"on": False, "sensitivity": "loud", "gap": 2, "days": 99, "chat": True})
    s = ac.load_settings()
    assert s["on"] is False and s["chat"] is True and s["sensitivity"] == "medium" and s["gap"] == 60 and s["days"] == 30


def test_the_api(setup):
    ac, auto, api, host, events = setup
    status = api.run("autoclip.status", {})
    assert status["watching"] is True and status["suggestions"] == 0
    out = api.run("autoclip.set", {"sensitivity": "high", "chat": "true", "gap": "60"})
    assert out["sensitivity"] == "high" and out["chat"] is True and out["gap"] == 60
    assert auto.detectors["mic"].margin == ac.SpikeDetector.MARGIN["high"]
    with pytest.raises(ApiError):
        api.run("autoclip.set", {"sensitivity": "max"})
    with pytest.raises(ApiError):
        api.run("autoclip.set", {"on": "maybe"})
    assert api.run("autoclip.mark", {})["in"] == 5.0
    clips = api.run("autoclip.list", {})["clips"]
    assert clips[0]["reason"] == "mark" and clips[0]["label"] == "Marked"
    kept = api.run("autoclip.keep", {"id": clips[0]["id"]})
    assert len(kept["files"]) == 2
    with pytest.raises(ApiError):
        api.run("autoclip.keep", {"id": clips[0]["id"]})
    with pytest.raises(ApiError):
        api.run("autoclip.delete", {})
    host.clips = False
    with pytest.raises(ApiError):
        api.run("autoclip.mark", {})


def test_the_tools_entry_opens_one_window_that_follows_changes(setup, app):
    ac, auto, api, host, events = setup
    entry = dict(plugin_hooks.tools())["Suggested clips…"]
    entry(api)
    window = ac._WINDOW
    assert window.isVisible()
    entry(api)
    assert ac._WINDOW is window  # brought to the front, not a second one
    labels = lambda: [w.text() for w in window.findChildren(__import__("PySide6.QtWidgets").QtWidgets.QLabel)]  # noqa: E731
    assert "None yet." in labels()
    auto.suggest("game")
    app.processEvents()
    assert "Loud game" in labels()
    window.ticks["chat"].setChecked(True)
    assert auto.settings["chat"] is True and ac.load_settings()["chat"] is True
    window.on.setChecked(False)
    assert auto.settings["on"] is False and not window.sens.isEnabled()
    window.close()
    app.processEvents()
    assert auto._listeners == []
    auto.changed()  # nothing left to redraw


def test_the_level_hook_feeds_it(setup):
    ac, auto, api, host, events = setup
    plugin_hooks.levels_heard(api, _levels(mic=-28))
    assert auto.detectors["mic"].fast is not None


def test_the_window_lists_it_in_tools_and_saves_suggestions_beside_clips(ac, tmp_path, monkeypatch, app):
    import keyring

    from sprigcast.app.control_server import WindowHost
    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    try:
        w._tools_menu.aboutToShow.emit()
        w._tools_menu.aboutToShow.emit()  # filled again, not twice
        labels = [a.text() for a in w._tools_menu.actions()]
        assert labels.count("Suggested clips…") == 1 and labels[-1] == "Suggested clips…"
        host = WindowHost(w)
        assert host.recordings_folder() == w._recordings_folder()
        with pytest.raises(RuntimeError):
            host.save_clip_into(tmp_path / "S")  # clips aren't ready
        host.say("hi")
        assert w.statusBar().currentMessage() == "hi"
    finally:
        w.close()


def test_a_loud_loop_suggests_once_not_every_time_it_comes_round(setup):
    """The PC heavy run: a looping video with a loud bit saved a suggestion every minute."""
    ac, auto, api, host, events = setup
    t = 0.0
    for _ in range(20):  # 20 minutes: 50 s of game, then 10 s of the loud bit, round and round
        t = _run(auto, 50, start=t, desk=-35)
        t = _run(auto, 10, start=t, desk=-12)
    assert host.saves == 1
    t = _run(auto, 2, start=t, desk=-35)
    _run(auto, 2, start=t, desk=0)  # something much louder than the loop still counts
    assert host.saves == 2


def test_a_steady_loud_game_is_never_a_spike(ac):
    det = ac.SpikeDetector("high", repeats=True)
    _, t = _talk(det, 30, -14)
    over, _ = _talk(det, 600, -14, start=t)
    assert over is None


def test_looping_media_is_left_out(setup):
    from sprigcast.core.scene import SourceConfig

    ac, auto, api, host, events = setup
    api.col.sources.append(SourceConfig(id="brb", kind="media", name="BRB", settings={"loop": True}))
    api.col.sources.append(SourceConfig(id="vid", kind="media", name="Clip", settings={"loop": False}))
    auto._groups_of = None
    groups = auto._source_groups()
    assert "brb" not in groups["game"] and "vid" in groups["game"]


def test_at_most_ten_an_hour_and_never_while_one_is_saving(setup):
    ac, auto, api, host, events = setup
    auto.set(gap=60)
    held = []
    auto._in_background = lambda work, then: held.append((work, then))
    assert auto.suggest("mark", now=0.0) is True
    assert auto.suggest("mark", now=100.0) is False  # the first is still being saved
    work, then = held.pop()
    then(work())
    for n in range(1, ac.MOST_AN_HOUR):
        assert auto.suggest("mark", now=n * 61.0) is True
        work, then = held.pop()
        then(work())
    assert not auto.can_suggest(ac.MOST_AN_HOUR * 61.0)
    assert auto.suggest("mark", now=ac.MOST_AN_HOUR * 61.0) is False
    assert auto.can_suggest(3600.0 + 1)  # the first has dropped out of the hour
