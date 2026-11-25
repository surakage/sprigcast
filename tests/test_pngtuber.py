"""PNGtuber store plugin: your avatar pictures, talking when you talk."""

import sys
import time

import numpy as np
import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import SceneItem, SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_store_effects import Host  # noqa: E402

from test_control_api import _collection  # noqa: E402


@pytest.fixture
def tuber(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "pngtuber")
    assert plugin.name == "PNGtuber" and plugin.adds == "Source"
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    module = sys.modules["sprigcast_plugin_pngtuber"]
    module._SCANS.clear()
    yield module
    plugin_store.remove(tmp_path / "plugins", "pngtuber")
    assert "pngtuber" not in known_kinds()


def _picture(path, colour, size=(100, 120), mouth=None, gif_frames=0):
    """A test picture: a coloured block (see-through around it), with a mouth patch when asked."""
    from PIL import Image

    def one(c):
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        block = Image.new("RGBA", (size[0] - 20, size[1] - 20), c)
        img.paste(block, (10, 20))
        if mouth:
            img.paste(Image.new("RGBA", (20, 10), mouth), (40, 90))
        return img

    path.parent.mkdir(parents=True, exist_ok=True)
    if gif_frames:
        frames = [one((colour[0], colour[1], (colour[2] + 40 * i) % 256, 255)) for i in range(gif_frames)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=50, loop=0, disposal=2)
    else:
        one(colour).save(path)
    return path


def _model(folder, **extra):
    _picture(folder / "idle.png", (200, 150, 100, 255))
    _picture(folder / "talk.png", (200, 150, 100, 255), mouth=(60, 20, 20, 255))
    for name, (colour, mouth) in extra.items():
        _picture(folder / f"{name}.png", colour, mouth=mouth)
    return folder


def test_it_isnt_running_until_added():
    assert "pngtuber" not in known_kinds()
    assert "pngtuber" in {p.id for p in plugin_store.catalog()}


def test_file_names_say_what_each_picture_is(tuber):
    read = tuber.read_name
    assert read("idle") == (None, "idle") and read("Mouth_Closed") == (None, "idle")
    assert read("talk-blink") == (None, "talk_blink") and read("SCREAM") == (None, "yell")
    assert read("Happy_Talk_Blink") == ("happy", "talk_blink")
    assert read("very angry idle") == ("very angry", "idle")
    assert read("background") is None


def test_a_model_folder_is_read_with_expressions_from_folders_and_names(tuber, tmp_path):
    folder = _model(tmp_path / "me", blink=((200, 150, 100, 255), None))
    _picture(folder / "happy_idle.png", (250, 200, 100, 255))
    _picture(folder / "happy_talk.webp", (250, 200, 100, 255), mouth=(0, 0, 0, 255))
    _picture(folder / "Sad" / "idle.png", (90, 90, 200, 255))
    _picture(folder / "only blink" / "blink.png", (90, 90, 200, 255))  # no idle or talk: not an expression
    (folder / "notes.txt").write_text("hi")
    found = tuber.scan(folder)
    assert tuber.expression_order(found) == ["default", "happy", "sad"]
    assert set(found["default"]) == {"idle", "talk", "blink"} and set(found["happy"]) == {"idle", "talk"}


def test_two_pictures_named_anything_are_quiet_then_talking(tuber, tmp_path):
    _picture(tmp_path / "m" / "a-frame.png", (1, 2, 3, 255))
    _picture(tmp_path / "m" / "b-frame.png", (1, 2, 3, 255))
    found = tuber.scan(tmp_path / "m")
    assert found["default"]["idle"].name == "a-frame.png" and found["default"]["talk"].name == "b-frame.png"


def test_missing_pictures_borrow_the_nearest_one(tuber):
    pick = tuber.pick_state
    have = {"idle": 1, "talk": 1}
    assert pick(have, "blink") == "idle" and pick(have, "talk_blink") == "talk"
    assert pick(have, "yell") == "talk" and pick(have, "yell_blink") == "talk"
    assert pick({"talk": 1, "blink": 1}, "idle") == "talk"
    assert pick({"idle": 1, "blink": 1, "talk": 1, "talk_blink": 1}, "yell_blink") == "talk_blink"


def test_the_mouth_opens_and_shuts_with_each_syllable(tuber):
    voice = tuber.Voice()
    dB = lambda d: 10 ** (d / 20)  # noqa: E731
    feed = lambda d, t: voice.feed(dB(d), t, talk_at=-38.0, yell_at=-6.0, hold=0.08)  # noqa: E731
    assert feed(-60, 0.0) == "idle"
    assert feed(-40, 0.05) == "idle"  # under Talks above: still quiet
    assert feed(-20, 0.1) == "talk" and voice.speaking
    assert feed(-24, 0.15) == "talk"  # a little softer: same syllable
    assert feed(-32, 0.2) == "idle"  # the dip between syllables, though still above Talks above
    assert feed(-31, 0.22) == "idle"  # not the next syllable yet
    assert feed(-22, 0.25) == "talk"  # the next syllable
    assert feed(-60, 0.4) == "idle" and voice.speaking  # a short gap: same sentence (no new bounce)
    assert feed(-60, 0.7) == "idle" and not voice.speaking  # the sentence is over


def test_hold_keeps_the_mouth_open_at_least_that_long(tuber):
    voice = tuber.Voice()
    feed = lambda level, t: voice.feed(level, t, talk_at=-38.0, yell_at=-6.0, hold=0.25)  # noqa: E731
    assert feed(0.1, 0.0) == "talk"
    assert feed(0.0, 0.1) == "talk"
    assert feed(0.0, 0.3) == "idle"


def test_yelling_needs_a_moment_of_loud_not_a_clap(tuber):
    voice = tuber.Voice()
    shout = 0.8  # about -2 dB
    feed = lambda level, t: voice.feed(level, t, talk_at=-38.0, yell_at=-6.0, hold=0.08)  # noqa: E731
    assert feed(shout, 0.0) == "talk"  # a clap: not yet
    assert feed(0.1, 0.02) == "talk"
    assert feed(shout, 0.1) == "talk"
    assert feed(shout, 0.2) == "yell"
    assert feed(shout, 0.3) == "yell"
    assert feed(0.3, 0.35) == "yell"  # stays a moment
    assert feed(0.3, 0.45) == "talk"
    assert feed(0.0, 1.0) == "idle"


def test_blinks_come_every_few_seconds_and_are_short(tuber):
    import random

    blinker = tuber.Blinker(random.Random(4))
    shut = [t for t in np.arange(0, 30, 0.01) if blinker.closed(float(t))]
    starts = [shut[0]] + [b for a, b in zip(shut, shut[1:]) if b - a > 0.05]
    assert 4 <= len(starts) <= 16
    assert len(shut) * 0.01 <= len(starts) * (tuber.BLINK + 0.02)


def test_pictures_are_fitted_together_standing_on_the_bottom_and_premultiplied(tuber, tmp_path):
    folder = _model(tmp_path / "m")
    _picture(folder / "yell.png", (200, 150, 100, 128), size=(50, 60))  # half see-through and smaller
    model = tuber.load_model(folder)
    idle = model.pictures["default"]["idle"].frames[0]
    pixels, x, y = idle
    scale = min(tuber.W / 100, (tuber.H - tuber.HOP) / 120)
    assert abs(pixels.shape[0] - 100 * scale) < 2 * scale + 2  # cut to what shows: 100 of 120 rows (soft edges)
    assert y + pixels.shape[0] == tuber.H  # feet on the bottom edge
    assert tuple(pixels[50, 50]) == (100, 150, 200, 255)  # B, G, R, A
    yell, _, yy = model.pictures["default"]["yell"].frames[0]
    assert abs(yell.shape[0] - 40 * scale) < 2 * scale + 2  # same scale as the others, not blown up to fill
    b, g, r, a = (int(v) for v in yell[yell.shape[0] // 2, yell.shape[1] // 2])
    assert abs(a - 128) <= 1 and abs(r - 100) <= 2  # colour already multiplied by alpha
    assert model.diffs  # where pictures differ is worked out while loading


def test_animated_pictures_play_and_long_ones_are_thinned(tuber, tmp_path, monkeypatch):
    folder = tmp_path / "m"
    _picture(folder / "idle.gif", (200, 150, 100, 255), gif_frames=4)
    _picture(folder / "talk.png", (200, 150, 100, 255), mouth=(0, 0, 0, 255))
    picture = tuber.load_model(folder).pictures["default"]["idle"]
    assert len(picture.frames) == 4 and picture.times[-1] == pytest.approx(0.2)
    assert [picture.frame_at(t) for t in (0.0, 0.06, 0.11, 0.17, 0.21)] == [0, 1, 2, 3, 0]
    monkeypatch.setattr(tuber, "MOST_FRAMES", 2)
    picture = tuber.load_model(folder).pictures["default"]["idle"]
    assert len(picture.frames) == 2 and picture.times[-1] == pytest.approx(0.2)  # same length, fewer frames


def test_a_folder_without_a_model_says_so(tuber, tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(tuber.ModelError):
        tuber.load_model(tmp_path / "empty")


def test_the_sample_sloth_ships_with_an_expression(tuber):
    model = tuber.load_model(tuber.model_folder({}))
    assert model.expressions == ["default", "blush"]
    assert set(model.pictures["default"]) == {"idle", "talk", "blink", "talk_blink", "yell", "yell_blink"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _ready(tuber, monkeypatch, folder, **settings):
    clock = Clock()
    monkeypatch.setattr(tuber.time, "monotonic", clock)
    level = {"mic": 0.0}
    monkeypatch.setattr(tuber, "_sound_level", lambda sid: level[sid])
    source = create_source("pngtuber", "tuber", {"folder": str(folder), "blink": False, **settings})
    source._model = tuber.load_model(folder)
    source._follow, source._resolved = "mic", True
    return source, clock, level


def _run(source, clock, seconds, step=1 / 60):
    sent = []
    end = clock.now + seconds
    while clock.now < end:
        clock.now += step
        frame = source.video_frame()
        if frame is not None:
            sent.append(frame.copy())
    return sent


def test_it_talks_with_your_voice_and_sends_nothing_while_still(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    first = source.video_frame().copy()
    assert first.shape == (tuber.H, tuber.W, 4) and first.flags["C_CONTIGUOUS"]
    assert _run(source, clock, 1.0) == []  # quiet and not blinking: nothing drawn, nothing uploaded
    level["mic"] = 0.3
    talking = _run(source, clock, 0.6)
    mouth = talking[-1]
    assert len(talking) > 3  # the fade and the bounce
    assert not np.array_equal(mouth, first)
    assert _run(source, clock, 0.5) == []  # still talking, bounce over: still
    level["mic"] = 0.0
    quiet = _run(source, clock, 1.0)
    assert np.array_equal(quiet[-1], first)  # mouth closed again, back on the ground


def lowest(frame):
    return int(np.nonzero(frame[..., 3].any(axis=1))[0][-1])


def test_the_mouth_fades_and_the_model_bounces(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    source.video_frame()
    level["mic"] = 0.3
    frames = _run(source, clock, 0.3)
    heights = [tuber.H - 1 - lowest(f) for f in frames]
    assert max(heights) > 5 and heights[-1] == 0  # went up and came back down
    model = source._model
    talk, x, y = model.pictures["default"]["talk"].frames[0]
    idle = model.pictures["default"]["idle"].frames[0][0]
    rows, cols = np.nonzero((talk != idle).any(axis=2))
    r, c = int(rows[len(rows) // 2]), int(cols[len(cols) // 2])
    seen = [int(f[y + r - h, x + c, 0]) for f, h in zip(frames, heights)]
    closed, opened = int(idle[r, c, 0]), int(talk[r, c, 0])
    assert seen[-1] == opened
    assert any(min(closed, opened) < v < max(closed, opened) for v in seen)  # a frame mid-fade


def lowest_ok(frame):
    return lowest(frame) == frame.shape[0] - 1


def test_no_bounce_when_bounce_is_off(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"), bounce=False)
    source.video_frame()
    level["mic"] = 0.3
    for frame in _run(source, clock, 0.3):
        assert lowest_ok(frame)


def test_it_blinks(tuber, monkeypatch, tmp_path):
    folder = _model(tmp_path / "m", blink=((40, 40, 40, 255), None))
    source, clock, level = _ready(tuber, monkeypatch, folder, blink=True)
    import random

    source.blinker = tuber.Blinker(random.Random(4))  # the same blinks every run (the clock is fake too)
    centre = (tuber.H - 30, tuber.W // 2)
    assert tuple(source.video_frame()[centre][:3]) == (100, 150, 200)
    sent = _run(source, clock, 8.0)
    assert any(tuple(f[centre][:3]) == (40, 40, 40) for f in sent)  # eyes shut at least once
    source.blinker._next = clock.now + 60  # no blink starts now: whatever the last one was, it ends
    sent += _run(source, clock, 0.5)
    assert tuple(sent[-1][centre][:3]) == (100, 150, 200)  # and open again


def test_expressions_switch_and_a_timed_one_goes_back(tuber, monkeypatch, tmp_path):
    folder = _model(tmp_path / "m")
    _picture(folder / "happy" / "idle.png", (250, 230, 50, 255))
    _picture(folder / "happy" / "talk.png", (250, 230, 50, 255), mouth=(0, 0, 0, 255))
    source, clock, level = _ready(tuber, monkeypatch, folder)
    source.video_frame()
    source.set_expression("happy")
    frames = _run(source, clock, 0.5)
    assert tuple(frames[-1][tuber.H - 30, tuber.W // 2][:3]) == (50, 230, 250)
    source.set_expression("default")
    source.set_expression("happy", seconds=2)
    _run(source, clock, 1.0)
    assert source.expression == "happy"
    _run(source, clock, 1.5)
    assert source.expression == "default"
    source.set_expression("happy")
    _run(source, clock, 0.5)
    source.set_expression("nope")  # not in the model: shows default rather than nothing
    assert tuple(_run(source, clock, 0.5)[-1][tuber.H - 30, tuber.W // 2][:3]) == (100, 150, 200)


def test_it_listens_to_your_sprigcast_mic_or_straight_to_windows(tuber):
    col = _collection()
    assert tuber.sound_for("auto", col) == "mic"
    assert tuber.sound_for("sound:desk", col) == "desk"
    assert tuber.sound_for("sound:gone", col) is None  # a removed sound: the default mic itself
    assert tuber.sound_for("device:Headset", col) is None
    col.sources = [s for s in col.sources if s.kind != "audio_input"]
    assert tuber.sound_for("auto", col) is None
    source = create_source("pngtuber", "t", {"listen_to": "device:Headset"})
    assert source.sound_to_follow(_collection()) is None and source._device() == "Headset"


def test_apps_set_the_mic_by_name_or_id_and_unknown_ones_are_refused(tuber):
    col = _collection()
    col.sources.append(SourceConfig(id="game-audio", kind="app_audio", name="Game Sound"))
    col.sources.append(SourceConfig(id="tuber", kind="pngtuber", name="Tuber", settings={}))
    api = NativeApi(Host(col, None))
    for given in ("game-audio", "sound:game-audio", "game sound"):
        got = api.run("source.set", {"source": "Tuber", "settings": {"listen_to": given}})
        assert got["settings"]["listen_to"] == "sound:game-audio"
    assert api.run("source.set", {"source": "Tuber", "settings": {"listen_to": "Auto"}})["settings"]["listen_to"] == "auto"
    for given in ("device:Wave Link Mic", "device:default"):
        assert api.run("source.set", {"source": "Tuber", "settings": {"listen_to": given}})["settings"]["listen_to"] == given
    with pytest.raises(ApiError, match="Game Sound"):
        api.run("source.set", {"source": "Tuber", "settings": {"listen_to": "speakers"}})


def test_the_engine_measures_the_followed_sound(tuber):
    from test_sleeping_sources import FakeCompositor

    from sprigcast.core.filters import SOUND_LEVELS
    from sprigcast.render.engine import Engine

    col = _collection()
    col.sources.append(SourceConfig(id="tuber", kind="pngtuber", name="PNGtuber"))
    col.scene("brb-h").items.append(SceneItem(source="tuber"))
    engine = Engine(col)
    SOUND_LEVELS["old"] = 0.9
    try:
        engine._sync_sources(FakeCompositor())
        assert "mic" in engine.mixer.followed and "old" not in SOUND_LEVELS  # stale levels don't linger
    finally:
        for source in engine._sources.values():
            source.stop()


def test_off_air_it_stops_listening_and_lets_the_pictures_go(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    closed = []

    class Mic:
        def stop(self):
            closed.append(True)

    monkeypatch.setattr(tuber, "_quit_later", lambda mic: mic.stop())
    source._mic = Mic()
    source.asleep = True
    source.sleep()
    assert closed and source._mic is None
    assert source._model is None  # let go at once, not a minute later


def test_back_on_air_it_sends_nothing_until_the_pictures_are_in(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    source.asleep = True
    source.sleep()
    started = []
    monkeypatch.setattr(tuber.threading, "Thread", lambda target, args, **k: type(
        "T", (), {"start": lambda self: started.append((target, args))})())
    source.asleep = False
    source.wake()
    assert started and source.video_frame() is None  # loading: the engine keeps the old bloom up (slow_wake)
    target, args = started[0]
    target(*args)
    frame = source.video_frame()
    assert frame is not None and frame[..., 3].any()


def test_a_load_that_ends_after_going_back_to_sleep_is_not_kept(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    source.asleep = True
    source.sleep()
    source._load(tuber.model_key(source.settings))
    assert source._model is None


def test_big_animated_emotes_are_kept_small(tuber, tmp_path, monkeypatch):
    """Frames are kept at the size they're shown and, past the budget, animated pictures keep fewer frames;
    no picture is ever held at its own (big) size."""
    folder = tmp_path / "emotes"
    _picture(folder / "idle.gif", (200, 150, 100, 255), size=(1600, 1600), gif_frames=40)
    _picture(folder / "talk.gif", (200, 150, 100, 255), size=(1600, 1600), gif_frames=40, mouth=(0, 0, 0, 255))
    seen = []
    real_fit = tuber._fit
    monkeypatch.setattr(tuber, "_fit", lambda f, *a: (seen.append(f.size), real_fit(f, *a))[1])
    monkeypatch.setattr(tuber, "MOST_BYTES", 8 * 1024 * 1024)
    model = tuber.load_model(folder)
    kept = [p for states in model.pictures.values() for p in states.values()]
    total = sum(px.nbytes for p in kept for px, _x, _y in p.frames)
    assert total <= 8 * 1024 * 1024
    assert all(2 <= len(p.frames) < 40 for p in kept)
    assert all(p.times[-1] == pytest.approx(2.0) for p in kept)  # same length, fewer frames
    assert len(seen) == sum(len(p.frames) for p in kept)  # only kept frames were ever fitted
    assert all(px.shape[0] <= tuber.H and px.shape[1] <= tuber.W for p in kept for px, _x, _y in p.frames)


def test_own_mic_opens_only_when_no_sprigcast_sound(tuber, monkeypatch, tmp_path):
    from sprigcast.sources import audio

    opened = []

    class FakeMic:
        def __init__(self, sid, settings):
            self.ring = None
            opened.append(settings["device"])

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(audio, "MicSource", FakeMic)
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"), listen_to="device:Headset")
    source._follow = None
    source.video_frame()
    assert opened == ["Headset"]
    source.set_hidden(True)
    assert source._mic is None and source._level() == 0.0


def test_apps_hotkeys_and_right_click_change_the_expression(tuber, tmp_path):
    folder = _model(tmp_path / "m")
    _picture(folder / "happy" / "idle.png", (250, 230, 50, 255))
    col = _collection()
    col.sources.append(SourceConfig(id="tuber", kind="pngtuber", name="Avatar", settings={"folder": str(folder)}))
    col.scene("chat-h").items.append(SceneItem(source="tuber"))
    host = Host(col)
    host.instances["tuber"] = create_source("pngtuber", "tuber", {"folder": str(folder)})
    api = NativeApi(host)
    assert {"pngtuber.expression", "pngtuber.next", "pngtuber.reset", "pngtuber.expressions"} <= set(api.all_actions())

    assert api.run("pngtuber.expressions", {}) == {"expression": "default", "expressions": ["default", "happy"]}
    out = api.run("pngtuber.expression", {"expression": "Happy"})
    assert out["expression"] == "happy" and col.source("tuber").settings["expression"] == "happy"
    assert host.instances["tuber"].expression == "happy" and host.commits[-1] == "sources"
    commits = len(host.commits)
    out = api.run("pngtuber.expression", {"expression": "default", "seconds": "5"})
    assert out["seconds"] == 5.0 and len(host.commits) == commits  # a timed one isn't saved
    assert col.source("tuber").settings["expression"] == "happy"
    assert api.run("pngtuber.next", {})["expression"] == "happy"
    assert api.run("pngtuber.reset", {})["expression"] == "default"
    with pytest.raises(ApiError) as err:
        api.run("pngtuber.expression", {"expression": "angry"})
    assert "default, happy" in err.value.message
    with pytest.raises(ApiError):
        api.run("pngtuber.expression", {"expression": "happy", "source": "Cam"})

    keys = dict(plugin_hooks.hotkey_actions(col))
    assert keys["plugin:pngtuber:expression:happy"] == "PNGtuber: happy"
    assert "plugin:pngtuber:next" in keys
    notes = []
    assert plugin_hooks.run_hotkey(api, "plugin:pngtuber:expression:happy", notes.append)
    assert host.instances["tuber"].expression == "happy" and notes == []
    col.active_scenes["horizontal"] = "brb-h"
    plugin_hooks.run_hotkey(api, "plugin:pngtuber:next", notes.append)
    assert notes == ["Not in this bloom: Avatar"]

    entries = []
    plugin_hooks.fill_item_menu(api, col.source("tuber"), lambda label, fn: entries.append((label, fn)))
    assert [label for label, _ in entries] == ["Expression: default", "Expression: happy"]
    entries[0][1]()
    assert host.instances["tuber"].expression == "default"


def test_without_a_pngtuber_apps_hear_which_plugin_to_add(tuber):
    api = NativeApi(Host(_collection()))
    with pytest.raises(ApiError) as err:
        api.run("pngtuber.next", {})
    assert "Add a PNGtuber" in err.value.message


def test_a_pngtuber_with_no_pictures_asks_for_them(tuber, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    col = _collection()
    col.sources.append(SourceConfig(id="tuber", kind="pngtuber", name="Avatar", settings={"folder": str(empty)}))
    col.scene("chat-h").items.append(SceneItem(source="tuber"))
    host = Host(col)
    host.instances["tuber"] = create_source("pngtuber", "tuber", {"folder": str(empty)})
    api = NativeApi(host)
    for action, data in (("pngtuber.next", {}), ("pngtuber.reset", {}), ("pngtuber.expression", {"expression": "x"}),
                         ("pngtuber.next", {"seconds": 5})):
        with pytest.raises(ApiError) as err:
            api.run(action, data)
        assert err.value.message == tuber.NO_PICTURES
    assert api.run("pngtuber.expressions", {}) == {"expression": "default", "expressions": []}
    notes = []
    plugin_hooks.run_hotkey(api, "plugin:pngtuber:next", notes.append)
    assert notes == [tuber.NO_PICTURES]
    entries = []
    plugin_hooks.fill_item_menu(api, col.source("tuber"), lambda label, fn: entries.append((label, fn)))
    assert entries == []


def test_the_settings_form_lists_sprigcast_sounds_and_mics(tuber, monkeypatch):
    from PySide6.QtWidgets import QComboBox

    from sprigcast.app.settings_dialog import SettingsDialog
    from sprigcast.sources import audio

    monkeypatch.setattr(audio, "list_audio_devices", lambda loopback=False, rescan=False: [])
    cls = tuber.PngtuberSource
    dialog = SettingsDialog("PNGtuber", cls.defaults, cls.defaults, cls=cls, collection=_collection())
    box = dialog.fields["listen_to"]
    assert isinstance(box, QComboBox)
    items = [(box.itemData(i), box.itemText(i)) for i in range(box.count())]
    assert items[0] == ("auto", "Auto (your mic)")
    assert ("sound:mic", "Mic") in items and ("sound:desk", "Desktop audio") in items
    assert ("device:default", "Default mic (from Windows)") in items
    assert dialog.values()["listen_to"] == "auto"
    dialog.deleteLater()


def test_frames_are_quick(tuber, monkeypatch):
    source, clock, level = _ready(tuber, monkeypatch, tuber.model_folder({}), blink=True)
    source.video_frame()
    spent = []
    for i in range(600):
        level["mic"] = 0.3 if (i // 40) % 2 else 0.0
        clock.now += 1 / 60
        began = time.perf_counter()
        source.video_frame()
        spent.append(time.perf_counter() - began)
    assert sorted(spent)[int(len(spent) * 0.99)] < 0.01


def test_hidden_it_does_nothing_and_shows_again_when_its_eye_is_back_on(tuber, monkeypatch, tmp_path):
    source, clock, level = _ready(tuber, monkeypatch, _model(tmp_path / "m"))
    source.video_frame()
    source.set_hidden(True)
    level["mic"] = 0.3
    assert source.video_frame() is not None  # one last frame when it's hidden...
    assert _run(source, clock, 0.5) == []  # ...then nothing while its eye is off
    source.set_hidden(False)
    assert _run(source, clock, 0.1)


def test_a_model_folder_filled_later_is_found_without_a_restart(tuber, monkeypatch, tmp_path):
    folder = tmp_path / "later"
    folder.mkdir()
    clock = Clock()
    monkeypatch.setattr(tuber.time, "monotonic", clock)
    source = create_source("pngtuber", "t", {"folder": str(folder)})
    source._load(tuber.model_key(source.settings))
    assert source.status == "No pictures in later" and source._model is None
    _model(folder)
    source._get_model()
    assert source._loading is None  # looked a moment ago: not again yet
    clock.now += 11
    source._failed_at = clock.now - 11
    monkeypatch.setattr(tuber.threading, "Thread", lambda target, args, **kw: type(
        "T", (), {"start": lambda self: target(*args)})())
    source._get_model()
    assert source._model is not None and source.status == "ok"


def test_a_folder_of_other_pictures_says_exactly_what_is_missing(tuber, tmp_path):
    emotes = tmp_path / "resized-images"
    for name in ("Angry", "Cozy", "Pog"):
        _picture(emotes / f"{name}.png", (200, 150, 100, 255))
    for folder, said in ((emotes, "No Idle or Talk picture in resized-images"),
                         (tmp_path / "gone", "Can't find the folder gone")):
        with pytest.raises(tuber.ModelError, match=said):
            tuber.load_model(folder)
    (tmp_path / "empty").mkdir()
    with pytest.raises(tuber.ModelError, match="No pictures in empty"):
        tuber.load_model(tmp_path / "empty")
    with pytest.raises(tuber.ModelError, match="Can't find Lost.png"):
        tuber.load_model(emotes, picked={"idle": emotes / "Cozy.png", "talk": emotes / "Lost.png"})


def test_idle_and_talk_pictures_can_be_picked_from_anywhere(tuber, monkeypatch, tmp_path):
    emotes = tmp_path / "emotes"
    quiet = _picture(emotes / "Cozy.png", (200, 150, 100, 255))
    loud = _picture(emotes / "Pog.png", (200, 150, 100, 255), mouth=(60, 20, 20, 255))
    _picture(emotes / "Angry.png", (90, 10, 10, 255))
    settings = {"idle_picture": str(quiet), "talk_picture": str(loud)}
    assert tuber.model_folder(settings) is None  # not the sample sloth's blinks under your pictures
    assert tuber.expressions_in(settings) == ["default"]
    model = tuber.load_model(None, picked=tuber.picked_pictures(settings))
    assert set(model.pictures["default"]) == {"idle", "talk"}
    # with a model folder too: they're its default, its expressions stay
    happy = _model(tmp_path / "m" / "happy")
    _picture(happy.parent / "blink.png", (1, 2, 3, 255))
    found = tuber.scan(happy.parent, {"idle": quiet, "talk": loud})
    assert found["default"] == {"idle": quiet, "talk": loud} and "happy" in found
    # just one picked: it's both (it bounces when you talk)
    assert tuber.scan(None, {"idle": quiet})["default"] == {"idle": quiet, "talk": quiet}
    # the source loads them, and loads again when one is changed
    clock = Clock()
    monkeypatch.setattr(tuber.time, "monotonic", clock)
    monkeypatch.setattr(tuber.threading, "Thread", lambda target, args, **kw: type(
        "T", (), {"start": lambda self: target(*args)})())
    source = create_source("pngtuber", "t", settings)
    source._get_model()
    assert source._model is not None and source.status == "ok"
    source.update({**settings, "talk_picture": str(emotes / "Lost.png")})
    assert source._model is None and source.status == "Can't find Lost.png"


def test_one_picture_named_anything_just_bounces(tuber, tmp_path):
    only = _picture(tmp_path / "solo" / "Me.png", (200, 150, 100, 255))
    assert tuber.scan(tmp_path / "solo") == {"default": {"idle": only}}
    model = tuber.load_model(tmp_path / "solo")
    assert model.picture("default", "talk") is model.picture("default", "idle")


def test_the_settings_form_has_pickers_for_the_two_pictures(tuber):
    from sprigcast.app.settings_dialog import SettingsDialog

    defaults = tuber.PngtuberSource.defaults
    dialog = SettingsDialog("PNGtuber", defaults, dict(defaults), cls=tuber.PngtuberSource)
    assert {"idle_picture", "talk_picture"} <= set(dialog.fields)
    assert dialog.fields["folder"].text() == "" and dialog.fields["folder"].placeholderText() == "Sloth (built in)"
    assert list(defaults)[:3] == ["folder", "idle_picture", "talk_picture"]


def test_a_folder_without_a_model_is_logged_once_not_every_look(tuber, monkeypatch, tmp_path, caplog):
    import logging

    folder = tmp_path / "resized-images"
    _picture(folder / "Cozy.png", (200, 150, 100, 255))
    _picture(folder / "Pog.png", (200, 150, 100, 255))
    _picture(folder / "Rage.png", (200, 150, 100, 255))
    source = create_source("pngtuber", "t", {"folder": str(folder)})
    key = (folder,) + (None,) * len(tuber.PICKED)
    with caplog.at_level(logging.INFO, logger=tuber.log.name):
        for _ in range(5):
            source._load(key)
        said = [r.getMessage() for r in caplog.records if "no model" in r.getMessage()]
        assert said == ["pngtuber: no model: No Idle or Talk picture in resized-images"]
        (folder / "Rage.png").unlink()  # two pictures named anything: a model
        source._load(key)
        assert source.status == "ok"
        (folder / "Pog.png").unlink()
        (folder / "Cozy.png").unlink()
        source._load(key)  # broken again later: said again
        said = [r.getMessage() for r in caplog.records if "no model" in r.getMessage()]
        assert said[-1] == "pngtuber: no model: No pictures in resized-images" and len(said) == 2
