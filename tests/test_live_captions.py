"""The Live captions store plugin: finding speech, turning it into words (on the PC or with the streamer's
own Google key), the model download, and the caption card."""

import hashlib
import io
import json
import os
import sys
import threading
import urllib.error
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # same as the other Qt tests, so this file runs on its own

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
    module = sys.modules["sprigcast_plugin_live_captions"]
    module.PREFETCH = False  # no downloads from tests unless one asks
    yield module
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
        cpu = [f for f, _, _ in cap.model_files(name, "cpu")]
        gpu = [f for f, _, _ in cap.model_files(name, "gpu")]
        assert cpu == [f"{name}-encoder.int8.onnx", f"{name}-decoder.int8.onnx", f"{name}-tokens.txt"]
        assert gpu == [f"{name}-encoder.onnx", f"{name}-decoder.onnx", f"{name}-tokens.txt"]
        assert all(len(sha) == 64 and size > 0 for _, size, sha in cap.model_files(name, "gpu"))


def test_the_model_downloads_once_only_the_files_it_needs_and_old_ones_go(cap, monkeypatch):
    data = {"tiny-encoder.int8.onnx": b"enc8" * 50, "tiny-decoder.int8.onnx": b"dec8" * 80,
            "tiny-tokens.txt": b"IQ== 0\n", "tiny-encoder.onnx": b"enc" * 99, "tiny-decoder.onnx": b"dec" * 99}

    def entry(file):
        return (file, len(data[file]), hashlib.sha256(data[file]).hexdigest())

    monkeypatch.setitem(cap.MODELS, "tiny", {"rev": "abc", "tokens": entry("tiny-tokens.txt"),
                                             "cpu": (entry("tiny-encoder.int8.onnx"), entry("tiny-decoder.int8.onnx")),
                                             "gpu": (entry("tiny-encoder.onnx"), entry("tiny-decoder.onnx"))})
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
        return Reply(data[request.full_url.rsplit("/", 1)[1]])

    from sprigcast import update

    real = update.download
    monkeypatch.setattr(update, "download", lambda url, dest, p=None, c=lambda: False, **kw:
                        real(url, dest, p, c, opener=opener, **{k: v for k, v in kw.items() if k != "opener"}))
    old = models.path_for("whisper-base/model.bin")  # what the first version downloaded
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    seen = []
    assert not cap.model_ready("tiny", "cpu")
    folder = cap.fetch_model("tiny", "cpu", lambda d, t: seen.append((d, t)))
    assert cap.model_ready("tiny", "cpu") and not cap.model_ready("tiny", "gpu")
    assert sorted(p.name for p in folder.iterdir()) == ["tiny-decoder.int8.onnx", "tiny-encoder.int8.onnx",
                                                        "tiny-tokens.txt"]
    assert len(calls) == 3 and all("sherpa-onnx-whisper-tiny/resolve/abc/" in u for u in calls)
    assert seen[-1][0] == seen[-1][1] == sum(len(data[f]) for f, _, _ in cap.model_files("tiny", "cpu"))
    assert not old.exists() and not old.parent.exists()  # the old model's space is given back
    cap.fetch_model("tiny", "cpu")
    assert len(calls) == 3  # already here: nothing downloaded again
    cap.fetch_model("tiny", "gpu")
    assert len(calls) == 5  # the graphics card's files; the tokens are shared

    (folder / "tiny-decoder.onnx").unlink()
    data["tiny-decoder.onnx"] = b"bad" * 99  # same size, different bytes: not what was pinned
    with pytest.raises(models.ModelError):
        cap.fetch_model("tiny", "gpu")
    assert not (folder / "tiny-decoder.onnx").exists()

    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        cap.fetch_model("tiny", "gpu", cancel=stop)


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


class _FakeOrt:
    """onnxruntime with a scripted "model": the decoder says `script` one token at a time."""

    SOT, EOT, NO_TS, NO_SPEECH, TRANSCRIBE, EN, ES = 50258, 50257, 50363, 50362, 50359, 50259, 50262
    VOCAB = 51865

    def __init__(self, script, gpu_fails=False, multilingual=True, quiet=False, heard="es"):
        self.script, self.gpu_fails, self.multilingual, self.quiet, self.heard = script, gpu_fails, multilingual, quiet, heard
        self.opened, self.prompts = [], []
        self.ExecutionMode = type("EM", (), {"ORT_SEQUENTIAL": 1})

    def set_default_logger_severity(self, _n):
        pass

    def get_available_providers(self):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]

    def SessionOptions(self):  # noqa: N802
        return type("Opts", (), {})()

    def InferenceSession(self, path, opts, providers):  # noqa: N802
        fake = self
        fake.opened.append((Path(path).name, providers[0], opts))
        if fake.gpu_fails and providers[0] == "DmlExecutionProvider":
            raise RuntimeError("DirectML: the driver is too old")
        meta = {"n_text_layer": "2", "n_text_state": "8", "n_text_ctx": "448", "eot": str(fake.EOT),
                "sot": str(fake.SOT), "no_timestamps": str(fake.NO_TS), "transcribe": str(fake.TRANSCRIBE),
                "no_speech": str(fake.NO_SPEECH), "is_multilingual": "1" if fake.multilingual else "0",
                "all_language_codes": "en,es", "all_language_tokens": f"{fake.EN},{fake.ES}"}

        class Session:
            def get_modelmeta(self):
                return type("M", (), {"custom_metadata_map": meta})()

            def run(self, _outputs, feed):
                if "mel" in feed:
                    assert feed["mel"].shape[1] == 80
                    t = feed["mel"].shape[2] // 2
                    return [np.zeros((2, 1, t, 8), np.float32)] * 2
                tokens = list(feed["tokens"][0])
                offset = int(feed["offset"][0])
                logits = np.zeros((1, len(tokens), fake.VOCAB), np.float32)
                if offset == 0:
                    fake.prompts.append(tokens)
                    logits[0, 0, fake.NO_SPEECH] = 30.0 if fake.quiet else 0.0
                    if tokens == [fake.SOT]:  # which language?
                        logits[0, -1, fake.ES if fake.heard == "es" else fake.EN] = 5.0
                    said = 0
                else:
                    said = offset - len(fake.prompts[-1]) + 1
                nxt = fake.script[said] if said < len(fake.script) else fake.EOT
                logits[0, -1, nxt] += 10.0
                cache = feed["in_n_layer_self_k_cache"]
                return [logits, cache, cache]

        return Session()


def _model_folder(tmp_path, folder_name="tiny", name="tiny"):
    folder = tmp_path / folder_name
    folder.mkdir()
    import base64

    lines = [f"{base64.b64encode(w.encode()).decode()} {i}" for i, w in enumerate([" Hi", " there", ".", " chat"])]
    (folder / f"{name}-tokens.txt").write_text("\n".join(lines), encoding="utf-8")
    return folder


def test_whisper_on_the_graphics_card_and_the_processor_when_it_cant(cap, monkeypatch, tmp_path):
    fake = _FakeOrt([0, 1, 2], gpu_fails=True)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    engine = cap.WhisperEngine(_model_folder(tmp_path), "tiny", "en", "gpu")
    assert engine.device == "cpu"
    assert [(f, p) for f, p, _ in fake.opened] == [("tiny-encoder.onnx", "DmlExecutionProvider"),
                                                   ("tiny-encoder.int8.onnx", "CPUExecutionProvider"),
                                                   ("tiny-decoder.int8.onnx", "CPUExecutionProvider")]
    opts = fake.opened[-1][2]
    assert opts.enable_cpu_mem_arena is False and 1 <= opts.intra_op_num_threads <= 4
    assert engine.transcribe(_tone(1.0)) == "Hi there."
    assert fake.prompts[-1] == [fake.SOT, fake.EN, fake.TRANSCRIBE, fake.NO_TS]

    fake = _FakeOrt([0, 3])
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    engine = cap.WhisperEngine(_model_folder(tmp_path, "tiny2"), "tiny", "auto", "gpu")
    assert engine.device == "gpu" and fake.opened[0][2].enable_mem_pattern is False
    assert engine.transcribe(_tone(1.0)) == "Hi chat"
    assert fake.prompts[-2] == [fake.SOT] and fake.prompts[-1][1] == fake.ES  # Any: it heard Spanish


def test_whisper_stops_when_it_repeats_itself_and_says_nothing_to_quiet(cap, monkeypatch, tmp_path):
    loop = [0, 1, 3, 2, 0, 1, 3, 2, 0, 1] * 3 + [0, 1, 3, 2, 0, 1, 3, 2]
    fake = _FakeOrt(loop, multilingual=False)
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    engine = cap.WhisperEngine(_model_folder(tmp_path), "tiny", "en", "cpu")
    text = engine.transcribe(_tone(3.0))
    assert text.count("Hi there chat.") <= 3 and len(text) < 60
    assert fake.prompts[-1] == [fake.SOT, fake.NO_TS]
    fake.quiet = True
    assert engine.transcribe(_quiet(1.0)) == ""
    fake.quiet = False
    fake.script = [0]
    assert cap.repeat_start([5, 6, 7, 5, 6, 7, 5, 6, 7]) == 3
    assert cap.repeat_start(list(range(8)) * 2) == 8
    assert cap.repeat_start([1, 2, 3, 4, 1, 2, 3, 4]) is None  # a short phrase said twice can be real
    assert cap.bracketed("[ Sound ]") and cap.bracketed("(music)") and not cap.bracketed("Hi (there)!")


def test_log_mel_is_whispers_picture_of_the_sound(cap):
    mel = cap.log_mel(_tone(1.0))
    assert mel.dtype == np.float32 and mel.shape == (80, (RATE + cap.TAIL) // 160)
    assert mel.max() <= 2.5 and mel.min() >= mel.max() - 2.0 - 1e-6  # 8 decades, scaled by 4
    peak = int(np.argmax(mel[:, 20]))
    assert 5 <= peak <= 15  # 220 Hz sits low on the mel scale
    filters = cap.mel_filters()
    assert filters.shape == (80, 201) and (filters >= 0).all() and (filters.sum(axis=1) > 0).all()


def test_auto_uses_a_graphics_card_only_when_there_is_one(cap, monkeypatch):
    from sprigcast.app import lowend

    monkeypatch.setattr(cap, "directml", lambda: True)
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("NVIDIA GeForce RTX 4070",))
    assert cap.graphics_card()
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("Intel(R) UHD Graphics 770",))
    assert not cap.graphics_card()
    monkeypatch.setattr(cap, "directml", lambda: False)
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("NVIDIA GeForce RTX 4070",))
    assert not cap.graphics_card()


def test_directml_is_found_without_importing_onnxruntime(cap, monkeypatch, tmp_path):
    import importlib.util

    pkg = tmp_path / "onnxruntime"
    (pkg / "capi").mkdir(parents=True)
    (pkg / "__init__.py").write_text("raise ImportError('must not be imported')")
    spec = type("Spec", (), {"origin": str(pkg / "__init__.py")})()
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: spec)
    assert not cap.directml()
    (pkg / "capi" / "DirectML.dll").write_bytes(b"")
    assert cap.directml()


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


def _captions_threads(source_id="c"):
    return [t for t in threading.enumerate() if t.name.startswith(f"captions-{source_id}") and t.is_alive()]


def _until(check, seconds=3.0):
    import time

    deadline = time.monotonic() + seconds
    while not check() and time.monotonic() < deadline:
        time.sleep(0.01)
    return check()


def test_it_listens_only_while_on_air(cap, monkeypatch):
    runs = []
    monkeypatch.setattr(cap, "PARK_SECONDS", 0.2)

    def run(self, stop):
        runs.append(stop)
        self._engine, self._engine_key = object(), ("pc",)
        stop.wait(5)

    monkeypatch.setattr(cap.LiveCaptionsSource, "_run", run)
    source = create_source("live_captions", "c", {})
    source.sleep()  # added off air: nothing starts
    assert runs == [] and _captions_threads() == []
    source.wake()
    source.wake()
    assert _until(lambda: len(runs) == 1) and len(_captions_threads()) == 1  # one listener, however often woken
    source.set_hidden(True)
    assert runs[0].is_set() and source.status == "paused"
    source.set_hidden(False)
    assert _until(lambda: len(runs) == 2) and not runs[1].is_set() and len(_captions_threads()) == 1
    source.sleep()
    assert runs[1].is_set() and source._engine is not None  # the model stays a little while...
    assert _until(lambda: source._engine is None and not _captions_threads())  # ...then it and the thread go
    source.stop()


def test_blooms_going_on_and_off_air_never_pile_up_threads(cap, monkeypatch):
    """The PC run: threads went from 3 to 5 across bloom wake and sleep cycles."""
    from sprigcast.sources import audio

    monkeypatch.setattr(cap, "PARK_SECONDS", 30.0)
    mics = []

    class FakeMic:
        def __init__(self, source_id, settings):
            self.ring = None
            self.alive = threading.Event()
            mics.append(self)

        def start(self):
            self.alive.set()

        def stop(self):
            self.alive.clear()

    class Engine:
        def transcribe(self, clip):
            return ""

    loads = []
    monkeypatch.setattr(audio, "MicSource", FakeMic)
    source = create_source("live_captions", "c", {})

    def make(stop):
        if source._engine is None:
            loads.append(1)
            source._engine = Engine()
        return source._engine

    monkeypatch.setattr(source, "_make_engine", make)
    before = threading.active_count()
    for _ in range(25):
        source.wake()
        source.set_hidden(False)  # the renderer says this after each wake
        assert _until(lambda: any(m.alive.is_set() for m in mics))
        source.sleep()
        assert _until(lambda: not any(m.alive.is_set() for m in mics))  # the mic is let go each time
    assert len(_captions_threads()) == 1 and threading.active_count() <= before + 1
    assert len(loads) == 1  # the model was loaded once and reused within the minute
    source.update({**source.settings, "language": "es"})  # off air: nothing starts
    assert not any(m.alive.is_set() for m in mics)
    source.stop()
    assert _until(lambda: not _captions_threads()) and source._engine is None


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


def _ink(image):
    """Columns and rows that have anything drawn, and the darkest drawn pixel."""
    from PySide6.QtGui import QImage

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    a = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()
    drawn = a[..., 3] > 0
    cols, rows = np.where(drawn.any(axis=0))[0], np.where(drawn.any(axis=1))[0]
    return cols, rows, a


def test_each_style_draws_its_own_look(cap):
    words = "Welcome in everyone, we're finally beating the last boss"
    base = cap.LiveCaptionsSource.defaults
    for look, _ in cap.LOOKS:
        cols, rows, _a = _ink(cap.paint(words, {**base, "look": look}))
        assert len(cols) and rows.max() > 200, look  # something drawn, near the bottom of the box
    _c, _r, card = _ink(cap.paint(words, {**base, "look": "card"}))
    _c, _r, outline = _ink(cap.paint(words, {**base, "look": "outline"}))
    _c, _r, plain = _ink(cap.paint(words, {**base, "look": "plain"}))
    assert (outline[..., :3].max(axis=2)[outline[..., 3] == 255] < 40).any()  # black edges round the letters
    assert not (plain[..., :3].max(axis=2)[plain[..., 3] == 255] < 40).any()  # plain: no edges
    assert (card[..., 3] > 0).sum() > (plain[..., 3] > 0).sum() * 2  # the card fills behind the words


def test_alignment_size_and_caps(cap):
    base = {**cap.LiveCaptionsSource.defaults, "look": "plain", "lines": 1}
    left, _, _ = _ink(cap.paint("hi chat", {**base, "align": "left"}))
    right, _, _ = _ink(cap.paint("hi chat", {**base, "align": "right"}))
    centre, _, _ = _ink(cap.paint("hi chat", base))
    assert left.min() < 60 and right.max() > 1540 and abs((centre.min() + centre.max()) / 2 - 800) < 20
    small, srows, _ = _ink(cap.paint("hi chat", {**base, "text_size": "small"}))
    large, lrows, _ = _ink(cap.paint("hi chat", {**base, "text_size": "large"}))
    assert len(large) > len(small) and len(lrows) > len(srows)
    lower, _, _ = _ink(cap.paint("hi chat", base))
    upper, _, _ = _ink(cap.paint("hi chat", {**base, "all_caps": True}))
    assert len(upper) > len(lower)
    for lines in (1, 2, 3):  # big text never spills out of the box
        _c, rows, _a = _ink(cap.paint("word " * 60, {**base, "look": "card", "text_size": "large", "lines": lines}))
        assert rows.min() > 0 and rows.max() < 260 - 1


def test_a_new_look_is_drawn_straight_away_and_old_captions_keep_theirs(cap):
    import time

    source = create_source("live_captions", "c", {"text_color": "#ffffff", "card_color": "#000000cc"})  # version 1
    source.board.say("Hello there.", final=True, now=time.monotonic())
    assert source.video_frame() is not None and source.video_frame() is None
    for key, value in (("look", "outline"), ("font", "DejaVu Serif"), ("bold", False), ("align", "left"),
                       ("outline_color", "#ff0000ff"), ("all_caps", True), ("text_size", "large")):
        source.update({**source.settings, key: value})
        assert source.video_frame() is not None, key
    fonts = cap._fonts()
    assert fonts[0] == ("", "SprigCast (Nunito)")
    names = [n for n, _ in fonts[1:]]
    assert names == sorted(names, key=str.lower)


def test_the_key_box_sits_under_captions_by_and_no_chat_tip(cap):
    from PySide6.QtWidgets import QFormLayout, QLabel

    from sprigcast.app.settings_dialog import SettingsDialog

    cls = source_class("live_captions")
    dialog = SettingsDialog("Live captions", cls.defaults, cls.defaults, cls=cls)
    form = dialog.layout()
    labels = [form.itemAt(i, QFormLayout.ItemRole.LabelRole).widget().text()
              for i in range(form.rowCount()) if form.itemAt(i, QFormLayout.ItemRole.LabelRole)
              and form.itemAt(i, QFormLayout.ItemRole.LabelRole).widget()]
    assert labels[labels.index("Captions by") + 1] == "Google key"
    assert not any("Chat panel" in w.text() for w in dialog.findChildren(QLabel))


def test_wake_and_sleep_many_times_keeps_memory_objects_and_threads_flat(cap, monkeypatch):
    """The per-plugin memory test before #403: about 10 MB more on every wake, never freed, and more
    threads each time. Each cycle here listens, hears speech, draws captions and goes off air."""
    import gc
    import time
    import tracemalloc
    import weakref

    from sprigcast.sources import audio

    speech = np.concatenate([_quiet(0.3), _tone(1.0), _quiet(0.9)])
    stereo48 = np.repeat(np.repeat(speech, 3)[:, None], 2, axis=1).astype(np.float32)
    mics = []

    class FakeMic:
        def __init__(self, source_id, settings):
            self.ring = None
            mics.append(weakref.ref(self))

        def start(self):
            for i in range(0, len(stereo48), 4800):
                self.ring.write(stereo48[i:i + 4800])

        def stop(self):
            pass

    class Engine:
        def transcribe(self, clip):
            return "Hello chat."

    loads = []
    monkeypatch.setattr(audio, "MicSource", FakeMic)
    monkeypatch.setattr(cap, "PARK_SECONDS", 30.0)
    source = create_source("live_captions", "c", {})

    def make(stop):
        if source._engine is None:
            loads.append(1)
            source._engine = Engine()
        return source._engine

    monkeypatch.setattr(source, "_make_engine", make)

    def cycle():
        source.wake()
        source.set_hidden(False)
        assert _until(lambda: "Hello chat." in source.board.text(time.monotonic(), 4.0))
        source.video_frame()
        source.sleep()
        assert _until(lambda: source.status == "paused")

    for _ in range(3):  # warm up: first frames, caches
        cycle()
    gc.collect()
    threads = threading.active_count()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for _ in range(15):
        cycle()
    gc.collect()
    grown = sum(s.size_diff for s in tracemalloc.take_snapshot().compare_to(before, "filename"))
    tracemalloc.stop()
    assert grown < 300_000, f"{grown} bytes more after 15 wakes"  # one mic ring is about 4.6 MB
    assert threading.active_count() == threads and len(_captions_threads()) == 1
    assert sum(1 for m in mics if m() is not None) == 0  # every mic (and its 12 s ring) was let go
    assert len(loads) == 1
    source.stop()
    assert _until(lambda: not _captions_threads()) and source._engine is None


def _render_ticks(source, seconds, until=None):
    """Call the source like the render loop does, 60 times a second, and return the longest tick gap."""
    import time

    worst, last = 0.0, time.perf_counter()
    end = last + seconds
    while time.perf_counter() < end and not (until and until()):
        source.video_frame()
        time.sleep(1 / 60)
        now = time.perf_counter()
        worst = max(worst, now - last - 1 / 60)
        last = now
    return worst


def test_loading_the_model_never_holds_up_the_render_tick(cap):
    """The PC test: making the graphics-card session held Python's lock for 1.3 s and froze the stream.
    Here the helper's load holds its own lock for 1.5 s in one C call; SprigCast's ticks stay smooth."""
    import time

    source = create_source("live_captions", "c", {})
    done = {}

    def load():
        t = time.perf_counter()
        done["engine"] = cap.ProcessEngine(None, "tiny", "en", "cpu", hold=1.5)
        done["took"] = time.perf_counter() - t

    loader = threading.Thread(target=load)
    loader.start()
    worst = _render_ticks(source, 10.0, until=lambda: "engine" in done)
    loader.join()
    engine = done["engine"]
    assert done["took"] >= 1.4 and engine.alive()
    assert worst < 0.05, f"a render tick waited {worst * 1000:.0f} ms"
    engine.close()
    assert engine.proc.returncode is not None  # the helper (and the model's memory) is gone



def test_captions_get_ready_in_the_helper_and_show_nothing_until_then(cap, monkeypatch, tmp_path):
    import time

    from sprigcast.sources import audio

    speech = np.concatenate([_quiet(0.3), _tone(1.0), _quiet(1.0)])
    stereo48 = np.repeat(np.repeat(speech, 3)[:, None], 2, axis=1).astype(np.float32)

    class FakeMic:
        def __init__(self, source_id, settings):
            self.ring = None

        def start(self):
            for i in range(0, len(stereo48), 4800):
                self.ring.write(stereo48[i:i + 4800])

        def stop(self):
            pass

    made = []
    real = cap.ProcessEngine

    def slow_helper(folder, name, language, device):
        engine = real(None, name, language, device, hold=1.0)  # loads for a second, holding its own lock
        engine.transcribe = lambda clip: "Ready now."
        made.append(engine)
        return engine

    monkeypatch.setattr(audio, "MicSource", FakeMic)
    monkeypatch.setattr(cap, "fetch_model", lambda *a, **k: tmp_path)
    monkeypatch.setattr(cap, "model_ready", lambda *a: True)
    monkeypatch.setattr(cap, "ProcessEngine", slow_helper)
    monkeypatch.setattr(cap, "PARK_SECONDS", 0.2)
    source = create_source("live_captions", "c", {})
    source.wake()
    assert _until(lambda: source.status == "Loading the speech model")
    seen_loading = []
    worst = _render_ticks(source, 10.0, until=lambda: bool(made))
    seen_loading.append(source.board.text(time.monotonic(), 4.0))
    assert worst < 0.05 and seen_loading == [""]  # smooth, and nothing on stream while it loads
    assert _until(lambda: "Ready now." in source.board.text(time.monotonic(), 4.0)) and source.status == "Listening"
    source.sleep()
    assert _until(lambda: made[0].proc.poll() is not None)  # off air a while: the helper ends
    source.stop()


def test_the_model_downloads_when_captions_join_a_garden_not_when_they_go_on_air(cap, monkeypatch):
    fetched = []
    gate = threading.Event()

    def fetch(name, device, progress=None, cancel=None):
        fetched.append((name, device))
        gate.wait(2)

    monkeypatch.setattr(cap, "PREFETCH", True)
    monkeypatch.setattr(cap, "fetch_model", fetch)
    monkeypatch.setattr(cap, "graphics_card", lambda: False)
    source = create_source("live_captions", "c", {"model": "tiny", "language": "es"})
    source.sleep()  # how the renderer adds a source that isn't on screen
    source.sleep()
    assert _until(lambda: fetched == [("tiny", "cpu")])  # once, in the background
    gate.set()
    google = create_source("live_captions", "g", {"engine": "google"})
    google.sleep()
    assert fetched == [("tiny", "cpu")]  # Google mode needs no model
    source.stop()
    google.stop()


def test_a_helper_that_cant_load_says_why(cap, tmp_path):
    # The reason comes back from the helper: the missing model file, or onnxruntime itself where a checkout
    # was set up before it was a dependency.
    reasons = "NoSuchFile|No such file|FileNotFound|doesn't exist|INVALID|Load model|No module named 'onnxruntime"
    with pytest.raises(RuntimeError, match=reasons):
        cap.ProcessEngine(tmp_path, "tiny", "en", "cpu")
