"""PNGtuber upgrade (user 2026-11-25): half-open mouth, naps, squash, sway and shake, reactions, outfits and
its own settings card."""

import pytest

from sprigcast import plugin_hooks
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core import drawn
from sprigcast.core.scene import SceneItem, SourceConfig
from sprigcast.core.source import create_source

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402
from test_pngtuber import _model, _picture, _ready, tuber  # noqa: E402,F401
from test_store_effects import Host  # noqa: E402


def _listed(source, clock, seconds, step=1 / 60):
    out = []
    drawn.ENGINE = True
    try:
        end = clock.now + seconds
        while clock.now < end:
            clock.now += step
            d = source.video_frame()
            if d is not None:
                out.append(d)
    finally:
        drawn.ENGINE = False
    return out


def _shown_state(source):
    return source._key_state


def test_a_soft_syllable_shows_the_half_open_mouth(tuber, monkeypatch, tmp_path):  # noqa: F811
    folder = _model(tmp_path / "m", half=((200, 150, 100, 255), (90, 20, 20, 255)))
    source, clock, level = _ready(tuber, monkeypatch, folder, shake=False)
    level["mic"] = 10 ** (-34 / 20)  # just over Talks above (-38)
    _listed(source, clock, 0.2)
    assert _shown_state(source) == "half"
    level["mic"] = 10 ** (-20 / 20)
    _listed(source, clock, 0.2)
    assert _shown_state(source) == "talk"


def test_it_naps_after_a_quiet_spell_and_wakes_when_you_talk(tuber, monkeypatch, tmp_path):  # noqa: F811
    folder = _model(tmp_path / "m", sleep=((90, 90, 200, 255), None))
    source, clock, level = _ready(tuber, monkeypatch, folder, sleep_after=30)
    _listed(source, clock, 5)
    assert _shown_state(source) == "idle"
    clock.now += 30
    _listed(source, clock, 0.1)
    assert _shown_state(source) == "sleep"
    level["mic"] = 0.05
    _listed(source, clock, 0.1)
    assert _shown_state(source) != "sleep"
    never, clock2, _ = _ready(tuber, monkeypatch, folder)  # Naps after: Never (the default)
    clock2.now += 3600
    _listed(never, clock2, 0.1)
    assert _shown_state(never) == "idle"


def test_bounce_styles_and_older_settings(tuber):  # noqa: F811
    assert tuber.bounce_style({}) == "hop" and tuber.bounce_style({"bounce": False}) == "off"
    assert tuber.bounce_style({"bounce": False, "bounce_style": "squash"}) == "squash"
    squashed = [tuber.squash_amount(t / 100, 1.0) for t in range(40)]
    assert max(squashed) == round(1000 * tuber.SQUASH) and min(squashed) < 0
    assert -min(squashed) <= 1000 * tuber.HOP / tuber.H  # never taller than the room above it
    assert tuber.squash_amount(1.0, 1.0) == 0


def test_squash_sway_and_shake_move_the_picture_without_new_art(tuber, monkeypatch, tmp_path):  # noqa: F811
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"), bounce_style="squash", sway=True)
    frames = _listed(source, clock, 2.0)
    assert len(frames) > 10  # the sway moves it
    xs = {round(p.transform[4]) for d in frames for p in d.places}
    assert len(xs) > 3
    level["mic"] = 0.3  # loud: a squash and a shake
    loud = _listed(source, clock, 0.3)
    scales = {round(p.transform[3], 3) for d in loud for p in d.places}
    assert min(scales) < 0.97
    arts = {p.key for d in frames + loud for p in d.places if p.key != "fade"}
    assert len(arts) <= 3  # moved, not drawn again


def test_reactions_hop_sparkle_and_shake(tuber, monkeypatch, tmp_path):  # noqa: F811
    source, clock, _level = _ready(tuber, monkeypatch, _model(tmp_path / "m"), bounce_style="off")
    assert _listed(source, clock, 0.1)
    assert _listed(source, clock, 0.3) == []  # still: nothing sent
    source.react("sparkle")
    sparkly = _listed(source, clock, 0.6)
    assert any(p.key == "sparkle" for d in sparkly for p in d.places)
    _listed(source, clock, 1.0)
    assert _listed(source, clock, 0.3) == []  # over: still again
    source.react("hop")  # a hop even with Bounce off
    hops = _listed(source, clock, 0.2)
    assert {round(p.y) for d in hops for p in d.places if p.key != "sparkle"} != {hops[0].places[0].y}
    source.react("Shake")
    assert _listed(source, clock, 0.3)
    with pytest.raises(ValueError):
        source.react("dance")


def _api(tuber, tmp_path, **settings):  # noqa: F811
    folder = _model(tmp_path / "m")
    cozy = _model(tmp_path / "cozy")
    col = _collection()
    settings = {"folder": str(folder), "outfits": [{"name": "Cozy", "folder": str(cozy)},
                                                   {"name": "Sloth", "folder": ""}], **settings}
    col.sources.append(SourceConfig(id="tuber", kind="pngtuber", name="Avatar", settings=settings))
    col.scene("chat-h").items.append(SceneItem(source="tuber"))
    host = Host(col)
    host.instances["tuber"] = create_source("pngtuber", "tuber", settings)
    return col, host, NativeApi(host), cozy


def test_apps_and_hotkeys_play_reactions_and_put_on_outfits(tuber, tmp_path):  # noqa: F811
    col, host, api, cozy = _api(tuber, tmp_path)
    assert {"pngtuber.react", "pngtuber.outfit", "pngtuber.outfits"} <= set(api.all_actions())
    assert api.run("pngtuber.react", {"reaction": "sparkle"}) == {"reaction": "sparkle"}
    assert host.instances["tuber"]._reaction[0] == "sparkle"
    with pytest.raises(ApiError) as err:
        api.run("pngtuber.react", {"reaction": "dance"})
    assert "hop, sparkle, shake" in err.value.message
    assert api.run("pngtuber.outfits", {}) == {"outfit": "", "outfits": ["Cozy", "Sloth"]}
    out = api.run("pngtuber.outfit", {"outfit": "cozy"})
    assert out["outfit"] == "Cozy" and col.source("tuber").settings["folder"] == str(cozy)
    assert col.source("tuber").settings["outfit"] == "Cozy"
    with pytest.raises(ApiError) as err:
        api.run("pngtuber.outfit", {"outfit": "party"})
    assert "Cozy, Sloth" in err.value.message

    keys = dict(plugin_hooks.hotkey_actions(col))
    assert keys["plugin:pngtuber:react:hop"] == "PNGtuber reaction: hop"
    assert keys["plugin:pngtuber:outfit:Sloth"] == "PNGtuber outfit: Sloth"
    notes = []
    assert plugin_hooks.run_hotkey(api, "plugin:pngtuber:outfit:Sloth", notes.append)
    assert col.source("tuber").settings["folder"] == "" and notes == []
    plugin_hooks.run_hotkey(api, "plugin:pngtuber:react:shake", notes.append)
    assert host.instances["tuber"]._reaction[0] == "shake"

    entries = []
    plugin_hooks.fill_item_menu(api, col.source("tuber"), lambda label, fn: entries.append((label, fn)))
    outfits = [(label, fn) for label, fn in entries if label.startswith("Outfit")]
    assert [label for label, _ in outfits] == ["Outfit: Cozy", "Outfit: Sloth"]
    outfits[0][1]()
    assert col.source("tuber").settings["outfit"] == "Cozy"


def test_picked_pictures_join_the_folder(tuber, tmp_path):  # noqa: F811
    folder = _model(tmp_path / "m")
    sleepy = _picture(tmp_path / "Snooze.png", (10, 10, 200, 255))
    settings = {"folder": str(folder), "sleep_picture": str(sleepy)}
    found = tuber.scan(tuber.model_folder(settings), tuber.picked_pictures(settings))
    assert found["default"]["sleep"] == sleepy and found["default"]["idle"].name == "idle.png"
    assert tuber.outfit_name_for({"folder": str(folder)}) == "m"
    assert tuber.outfit_name_for({"idle_picture": str(sleepy)}) == "Snooze"
    assert tuber.outfit_name_for({}) == "Sloth"


def test_the_settings_card_round_trips_and_saves_outfits(tuber, tmp_path):  # noqa: F811
    from sprigcast.app.pngtuber_dialog import NAPS, PngtuberDialog

    folder = _model(tmp_path / "m")
    reacted = []
    d = PngtuberDialog("PNGtuber", {"folder": str(folder), "bounce": False, "talk_at": -40, "sleep_after": 60},
                       listen_choices=[("auto", "Auto (your mic)"), ("sound:mic", "Mic")], level=lambda: -20.0,
                       react=reacted.append, ask_name=lambda suggested: suggested + " 2")
    try:
        v = d.values()
        assert v["bounce_style"] == "off" and v["talk_at"] == -40 and v["sleep_after"] == 60
        assert d.tiles["idle"].picture is not None and d.tiles["half"].borrowed  # no half picture: talk's, dimmed
        d._pick_style("squash")
        d.strength.slider.setValue(30)  # 150%
        d.nap.slider.setValue(0)
        v = d.values()
        assert v["bounce_style"] == "squash" and v["bounce"] and v["bounce_strength"] == 1.5 and v["sleep_after"] == 0
        assert NAPS[0] == 0
        d._save_outfit()
        assert d.outfit == "m 2" and d.values()["outfits"][0]["folder"] == str(folder)
        d.folder.setText("")
        d._wear(d.outfits[0])
        assert d.folder.text() == str(folder)
        d.meter._tick()
        assert d.meter.db == -20.0
    finally:
        d.close()


def test_the_window_opens_the_pngtuber_card(tuber, tmp_path, monkeypatch):  # noqa: F811
    from test_build_easy import _window

    from sprigcast.app.pngtuber_dialog import PngtuberDialog

    _col, w = _window(tmp_path, monkeypatch, "PngtuberCard")
    try:
        w._last_levels = (__import__("time").monotonic(), {"mic": (-18.0, -24.0)})
        d = w._pngtuber_dialog("PNGtuber", {"listen_to": "auto"}, None, primary="Add")
        assert isinstance(d, PngtuberDialog) and not hasattr(d, "tries")
        assert d.meter.level_now() == -18.0  # Auto follows the mic
        d.close()
    finally:
        w.close()
