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


def test_talking_starts_above_the_level_and_holds_between_words(tuber):
    voice = tuber.Voice()
    loud, quiet, edge = 10 ** (-20 / 20), 10 ** (-60 / 20), 10 ** (-40 / 20)  # -20, -60 and -40 dB
    feed = lambda level, t: voice.feed(level, t, talk_at=-38.0, yell_at=-6.0, hold=0.25)  # noqa: E731
    assert feed(quiet, 0.0) == "idle"
    assert feed(edge, 0.05) == "idle"  # under Talks above: still quiet
    assert feed(loud, 0.1) == "talk"
    assert feed(edge, 0.2) == "talk"  # a little under: keeps talking (no flapping)
    assert feed(quiet, 0.3) == "talk"  # a short gap between words
    assert feed(quiet, 0.5) == "idle"  # quiet for longer than Hold


def test_yelling_needs_a_moment_of_loud_not_a_clap(tuber):
    voice = tuber.Voice()
    shout = 0.8  # about -2 dB
    feed = lambda level, t: voice.feed(level, t, talk_at=-38.0, yell_at=-6.0, hold=0.25)  # noqa: E731
    assert feed(shout, 0.0) == "talk"  # a clap: not yet
    assert feed(0.1, 0.02) == "talk"
    assert feed(shout, 0.1) == "talk"
    assert feed(shout, 0.2) == "yell"
    assert feed(0.1, 0.25) == "yell"  # stays a moment
    assert feed(0.1, 0.6) == "talk"
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
    assert set(model.pictures["default"]) == {"idle", "talk", "blink", "talk_blink", "yell"}


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
    centre = (tuber.H - 30, tuber.W // 2)
    assert tuple(source.video_frame()[centre][:3]) == (100, 150, 200)
    sent = _run(source, clock, 8.0)
    assert any(tuple(f[centre][:3]) == (40, 40, 40) for f in sent)  # eyes shut at least once
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
    assert source._park is not None
    source._park.cancel()
    source._let_go()
    assert source._model is None


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
    source._load(folder)
    assert source.status == "Pick a folder with idle and talk pictures" and source._model is None
    _model(folder)
    source._get_model()
    assert source._loading is None  # looked a moment ago: not again yet
    clock.now += 11
    source._failed_at = clock.now - 11
    monkeypatch.setattr(tuber.threading, "Thread", lambda target, args, **kw: type(
        "T", (), {"start": lambda self: target(*args)})())
    source._get_model()
    assert source._model is not None and source.status == "ok"
