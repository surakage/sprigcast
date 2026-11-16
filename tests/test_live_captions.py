"""The Live captions store plugin: finding speech, turning it into words (on the PC or with the streamer's
own Google key), the model download, and the caption card."""

import hashlib
import io
import json
import sys
import threading
import urllib.error

import numpy as np
import pytest

from sprigcast import models, plugin_store
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds, source_class

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402

RATE = 16_000


@pytest.fixture
def cap(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "live_captions")
    assert plugin.name == "Live captions" and plugin.adds == "Source"
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    models.set_folder(tmp_path / "models")
    yield sys.modules["sprigcast_plugin_live_captions"]
    models.set_folder(None)
    plugin_store.remove(tmp_path / "plugins", "live_captions")
    assert "live_captions" not in known_kinds()


def _tone(seconds, level=0.2):
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * 220 * t) * level).astype(np.float32)


def _quiet(seconds, level=0.001):
    return (np.random.default_rng(1).standard_normal(int(RATE * seconds)) * level).astype(np.float32)


def test_it_isnt_running_until_added():
    assert "live_captions" not in known_kinds()


def test_speech_gives_guesses_while_talking_and_the_whole_thing_after_a_pause(cap):
    seg = cap.Segmenter(partial_every=1.0, longest=9.0)
    events = []
    audio = np.concatenate([_quiet(1.0), _tone(2.5), _quiet(1.2)])
    for i in range(0, len(audio), 800):  # 50 ms at a time, like the mic
        events += seg.feed(audio[i:i + 800])
    kinds = [k for k, _ in events]
    assert kinds.count("final") == 1 and kinds[-1] == "final"
    assert 1 <= kinds.count("partial") <= 3
    final = events[-1][1]
    assert 2.5 <= len(final) / RATE <= 3.5  # the speech, a little before it and a little quiet after
    assert not seg.talking


def test_quiet_and_clicks_say_nothing(cap):
    seg = cap.Segmenter()
    assert seg.feed(_quiet(3.0)) == []
    clicks = np.concatenate([_quiet(1.0), _tone(0.1), _quiet(1.0)])
    assert [k for k, _ in seg.feed(clicks)] == []


def test_long_talk_is_split_so_captions_keep_coming(cap):
    seg = cap.Segmenter(partial_every=0.0, longest=4.0)
    events = seg.feed(np.concatenate([_quiet(0.5), _tone(10.0)]))
    assert [k for k, _ in events] == ["final", "final"]  # no guesses when partials are off (Google)


def test_the_board_keeps_the_newest_words_and_clears_after_a_pause(cap):
    board = cap.Board()
    board.say("hello there", final=False, now=0.0)
    assert board.text(1.0, 4.0) == "hello there"
    board.say("Hello there, chat.", final=True, now=1.0)
    board.say("How", final=False, now=1.5)
    assert board.text(2.0, 4.0) == "Hello there, chat. How"
    board.say("Thank you.", final=True, now=2.0)  # what Whisper hears in near silence
    assert board.text(2.5, 4.0) == "Hello there, chat."
    assert board.text(7.0, 4.0) == ""  # a pause: the card goes away
    assert board.text(7.0, 0.0) == "Hello there, chat."  # Hide after 0 keeps it up


def test_wrap_keeps_the_last_lines(cap):
    lines = cap.wrap("one two three four five six", lambda s: len(s) <= 9, 2)
    assert lines == ["four five", "six"]


def test_model_follows_the_setting_and_the_pc(cap):
    assert cap.pick_model("auto", "en", cpu_count=16) == "base.en"
    assert cap.pick_model("auto", "es", cpu_count=4) == "tiny"
    assert cap.pick_model("small", "auto") == "small"
    for name in ("tiny", "tiny.en", "base", "base.en", "small", "small.en"):
        assert set(cap.MODELS[name]["files"]) == {"config.json", "tokenizer.json", "vocabulary.txt", "model.bin"}


def test_the_model_downloads_once_and_wrong_files_are_thrown_away(cap, monkeypatch):
    small = {"config.json": b"{}", "tokenizer.json": b"tok", "vocabulary.txt": b"a\nb"}
    big = b"weights" * 100
    files = {}
    for name, data in small.items():
        files[name] = (len(data), "git", hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
    files["model.bin"] = (len(big), "sha256", hashlib.sha256(big).hexdigest())
    monkeypatch.setitem(cap.MODELS, "tiny", {"rev": "abc", "files": files})
    calls = []

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        @property
        def headers(self):
            return {"Content-Length": str(len(self.getvalue()))}

    def opener(request, timeout=0):
        calls.append(request.full_url)
        name = request.full_url.rsplit("/", 1)[1]
        return Reply(big if name == "model.bin" else small[name])

    from sprigcast import update

    real = update.download
    monkeypatch.setattr(update, "download", lambda url, dest, p=None, c=lambda: False, **kw:
                        real(url, dest, p, c, opener=opener, **{k: v for k, v in kw.items() if k != "opener"}))
    seen = []
    assert not cap.model_ready("tiny")
    folder = cap.fetch_model("tiny", lambda d, t: seen.append((d, t)))
    assert cap.model_ready("tiny") and (folder / "model.bin").read_bytes() == big
    assert len(calls) == 4 and all("/resolve/abc/" in u for u in calls)
    assert seen[-1][0] == seen[-1][1] == sum(f[0] for f in files.values())
    cap.fetch_model("tiny")
    assert len(calls) == 4  # already here: nothing downloaded again

    (folder / "vocabulary.txt").unlink()
    small["vocabulary.txt"] = b"a\nc"  # same size, different bytes: not what was pinned
    with pytest.raises(models.ModelError):
        cap.fetch_model("tiny")
    assert not (folder / "vocabulary.txt").exists()

    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        cap.fetch_model("tiny", cancel=stop)


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_google_gets_the_audio_and_the_key_in_a_header(cap):
    sent = []

    def opener(request, timeout=0):
        sent.append(request)
        return _Reply(json.dumps({"results": [{"alternatives": [{"transcript": "hello chat"}]},
                                              {"alternatives": [{"transcript": " how are you"}]}]}).encode())

    engine = cap.GoogleEngine("AIza-test", "es", opener=opener)
    assert engine.transcribe(_tone(1.0)) == "hello chat how are you"
    request = sent[0]
    assert request.full_url == cap.GOOGLE_URL and "key=" not in request.full_url  # never in the address
    assert request.get_header("X-goog-api-key") == "AIza-test"
    body = json.loads(request.data)
    assert body["config"]["languageCode"] == "es-US" and body["config"]["sampleRateHertz"] == RATE
    assert len(__import__("base64").b64decode(body["audio"]["content"])) == RATE * 2
    assert cap.GoogleEngine.partials is False  # each request is billed: only whole things said


def test_a_refused_google_key_is_told_apart(cap):
    def opener(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad", {},
                                     io.BytesIO(b'{"error": {"message": "API key not valid."}}'))

    with pytest.raises(cap.KeyRefused):
        cap.GoogleEngine("bad", "en", opener=opener).transcribe(_tone(0.5))


def test_whisper_runs_on_the_cpu_when_the_graphics_card_cant(cap, monkeypatch, tmp_path):
    made = []

    class Seg:
        def __init__(self, text, no_speech=0.0):
            self.text, self.no_speech_prob = text, no_speech
            self.avg_logprob = -1.5 if no_speech else -0.2  # unsure and probably not speech: dropped

    class FakeModel:
        def __init__(self, path, device, compute_type, **kw):
            made.append((device, compute_type, kw))
            self.device = device

        def transcribe(self, audio, **kw):
            if self.device == "cuda":
                raise RuntimeError("Library cublas64_12.dll is not found")
            assert kw["language"] == "en" and kw["vad_filter"] is False
            return iter([Seg(" Hi"), Seg(" there."), Seg(" ghost", no_speech=0.9)]), None

    fake_fw = type(sys)("faster_whisper")
    fake_fw.WhisperModel = FakeModel
    fake_ct = type(sys)("ctranslate2")
    fake_ct.get_cuda_device_count = lambda: 1
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
    monkeypatch.setitem(sys.modules, "ctranslate2", fake_ct)
    engine = cap.WhisperEngine(tmp_path, "en")
    assert engine.device == "cpu" and [m[0] for m in made] == ["cuda", "cpu"]
    assert made[1][1] == "int8" and 1 <= made[1][2]["cpu_threads"] <= 4
    assert engine.transcribe(_tone(1.0)) == "Hi there."


def test_the_card_is_drawn_only_when_the_words_change(cap):
    source = create_source("live_captions", "c", {})
    assert source.size == (1600, 260)
    empty = source.video_frame()
    assert empty.shape == (260, 1600, 4) and not empty[..., 3].any()  # nothing said: see-through
    assert source.video_frame() is None
    source.board.say("Welcome in, everyone!", final=True, now=__import__("time").monotonic())
    shown = source.video_frame()
    assert shown[..., 3].any() and not shown[:100, :, 3].any()  # the card sits at the bottom
    assert source.video_frame() is None
    source.update({**source.settings, "card_color": "#00000000"})
    assert source.video_frame() is not None


def test_it_listens_only_while_on_air(cap, monkeypatch):
    runs = []
    monkeypatch.setattr(cap, "PARK_SECONDS", 0.05)

    def run(self, stop):
        runs.append(stop)
        self._engine, self._engine_key = object(), ("pc",)
        stop.wait(5)

    monkeypatch.setattr(cap.LiveCaptionsSource, "_run", run)
    source = create_source("live_captions", "c", {})
    source.sleep()  # added off air: nothing starts
    assert runs == []
    source.wake()
    source.wake()
    assert len(runs) == 1  # one listener, however often it's woken
    source.set_hidden(True)
    assert runs[0].is_set()
    source.set_hidden(False)
    assert len(runs) == 2 and not runs[1].is_set()
    source.sleep()
    assert runs[1].is_set() and source._engine is not None  # the model stays a little while...
    import time

    deadline = time.monotonic() + 2
    while source._engine is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert source._engine is None  # ...then it's let go
    source.stop()


def test_without_a_google_key_it_says_so(cap, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings().remove(cap.SECRET)
    source = create_source("live_captions", "c", {"engine": "google"})
    assert source._make_engine(threading.Event()) is None
    assert "Google key" in source.status


def test_the_settings_form_has_drop_downs_and_keeps_the_key_encrypted_outside_the_garden(cap, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QComboBox, QLineEdit

    from sprigcast.app import settings_dialog
    from sprigcast.app.settings_dialog import SettingsDialog
    from sprigcast.control import secret_store

    monkeypatch.setattr(cap, "_mics", lambda: [("default", "Default mic"), ("Wave Mic", "Wave Mic")])
    cls = source_class("live_captions")
    cls.choices["listen_to"] = cap._mics
    saved = {}
    monkeypatch.setattr(secret_store, "protect", lambda v: "dpapi:" + v[::-1])
    monkeypatch.setattr(secret_store, "unprotect", lambda v: v[6:][::-1] if v.startswith("dpapi:") else v)
    QSettings().remove(cap.SECRET)
    dialog = SettingsDialog("Live captions", cls.defaults, {**cls.defaults, "listen_to": "Old USB mic"}, cls=cls)
    mic = dialog.fields["listen_to"]
    assert isinstance(mic, QComboBox) and mic.currentData() == "Old USB mic"  # unplugged now, still picked
    assert isinstance(dialog.fields["engine"], QComboBox)
    dialog.fields["engine"].setCurrentIndex(dialog.fields["engine"].findData("google"))
    key = dialog.secret_fields["google_key"]
    assert isinstance(key, QLineEdit) and key.echoMode() == QLineEdit.EchoMode.Password
    key.setText("  AIza-mine  ")
    dialog.accept()
    values = dialog.values()
    assert values["engine"] == "google" and values["lines"] == 2 and "google_key" not in values
    saved = QSettings().value(cap.SECRET)
    assert saved == "dpapi:" + "AIza-mine"[::-1]
    assert settings_dialog.read_secret(cap.SECRET) == "AIza-mine"
    from sprigcast.app.tester_report import SECRET_NAME

    assert SECRET_NAME.search(cap.SECRET)  # tester reports hide it
    again = SettingsDialog("Live captions", cls.defaults, values, cls=cls)
    assert again.secret_fields["google_key"].text() == "AIza-mine"
    again.secret_fields["google_key"].setText("")
    again.accept()
    assert QSettings().value(cap.SECRET) is None  # emptied: forgotten


def test_adding_it_puts_a_wide_band_along_the_bottom_of_both_shapes():
    col = _collection()
    col.sources.append(SourceConfig(id="c", kind="live_captions", name="Live captions"))
    part = col.own_sprig(col.scene("chat-h"), "c", (1600, 260), "Live captions", "bottom_center")
    for shape in col.shapes(part):
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        w, h = 1600 * item.scale, 260 * item.scale
        assert abs(item.x + w / 2 - canvas.width / 2) < 1e-6  # centred
        assert 0.7 * canvas.width <= w <= 0.9 * canvas.width
        assert item.y + h < canvas.height and item.y > canvas.height / 2  # low, but inside


def test_what_the_mic_hears_ends_up_on_the_card(cap, monkeypatch):
    """The listening thread end to end: a mic at 48 kHz stereo, a pause, words on the board."""
    from sprigcast.sources import audio

    speech = np.concatenate([_quiet(0.5), _tone(1.5), _quiet(1.0)])
    stereo48 = np.repeat(np.repeat(speech, 3)[:, None], 2, axis=1)
    opened = []

    class FakeMic:
        def __init__(self, source_id, settings):
            opened.append(settings["device"])
            self.ring = None

        def start(self):
            for i in range(0, len(stereo48), 4800):
                self.ring.write(stereo48[i:i + 4800].astype(np.float32))

        def stop(self):
            opened.append("stopped")

    heard = []

    class Engine:
        def transcribe(self, clip):
            heard.append(len(clip) / RATE)
            return "Good evening, chat."

    monkeypatch.setattr(audio, "MicSource", FakeMic)
    source = create_source("live_captions", "c", {"listen_to": "Wave Mic"})
    monkeypatch.setattr(source, "_make_engine", lambda stop: Engine())
    stop = threading.Event()
    worker = threading.Thread(target=source._run, args=(stop,))
    worker.start()
    import time

    deadline = time.monotonic() + 5
    while "Good evening" not in source.board.text(time.monotonic(), 4.0) and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    worker.join(2)
    assert source.board.done == ["Good evening, chat."] and source.status == "Listening"
    assert opened == ["Wave Mic", "stopped"] and heard and 1.4 <= heard[-1] <= 2.5
