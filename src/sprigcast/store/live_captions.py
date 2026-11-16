"""Live captions: what you say, written on stream as you say it.

Add Live captions (+ → Plugins → Live captions): it gets a sprig of its own along the bottom of your
bloom, in every shape. Move or resize it like anything else.

Two ways to turn speech into words (Settings → Captions by):
- On this PC (the default): a small Whisper speech model runs on your own PC. Free, private and
  offline. It runs on the graphics card when there is one (NVIDIA, AMD or Intel Arc, through
  DirectML: nothing more to install), else on a few processor threads (Run on picks). The model
  downloads the first time captions go on air and is kept for next time: on the processor 100 MB
  for Light, 160 MB for Balanced, 375 MB for Sharper; on a graphics card 150, 290 and 970 MB.
  Auto picks Light or Balanced for your PC.
- Google (your own key): Google Cloud Speech-to-Text, for PCs that are too slow for the on-PC model.
  Paste your own API key from a Google Cloud project with Speech-to-Text turned on. Google bills
  your project (about $0.96 an hour after the free 60 minutes a month). The key is kept encrypted
  for your Windows account, never in the garden file.

It only works while it's on air: hidden, or in a bloom that isn't showing, it stops listening, and a
minute later it lets the speech model go too, so it costs nothing when you aren't using it.

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import numpy as np

from sprigcast.core import painted
from sprigcast.core.source import Source, register

PLUGIN = {"id": "live_captions", "name": "Live captions", "version": 3, "adds": "Source",
          "about": "What you say, as captions on stream. Runs on your PC, or with your own Google key.",
          "new": "Runs on your graphics card (any NVIDIA, AMD or Intel Arc card), with nothing more to install."}

log = logging.getLogger(__name__)

KIND = "live_captions"
W, H = 1600, 260  # the card's box; scale it on the canvas like anything else
RATE = 16_000  # what speech models listen at
SECRET = "captions/google_key"  # where the Google key is kept (encrypted), see settings_dialog.secrets
PARK_SECONDS = 60.0  # after going off air, keep the model this long in case it comes straight back
GOOGLE_URL = "https://speech.googleapis.com/v1/speech:recognize"

# Languages: (Whisper's code, Google's code, name). "auto" lets Whisper work it out (Google uses en-US).
LANGUAGES = (
    ("auto", "en-US", "Any (on this PC)"),
    ("zh", "cmn-Hans-CN", "Chinese"),
    ("nl", "nl-NL", "Dutch"),
    ("en", "en-US", "English"),
    ("fr", "fr-FR", "French"),
    ("de", "de-DE", "German"),
    ("hi", "hi-IN", "Hindi"),
    ("it", "it-IT", "Italian"),
    ("ja", "ja-JP", "Japanese"),
    ("ko", "ko-KR", "Korean"),
    ("pl", "pl-PL", "Polish"),
    ("pt", "pt-BR", "Portuguese"),
    ("ru", "ru-RU", "Russian"),
    ("es", "es-US", "Spanish"),
    ("tr", "tr-TR", "Turkish"),
)

# Whisper models exported to ONNX by the sherpa-onnx project (k2-fsa, Apache 2.0), pinned to one commit
# so they never change under us, each file checked by its SHA-256. ONNX Runtime runs them on any graphics
# card through DirectML (no CUDA or cuDNN to download: those are about a gigabyte), or on the processor.
# The processor uses the smaller int8 files; the graphics card the full ones.
HF = "https://huggingface.co/csukuangfj/sherpa-onnx-whisper-{name}/resolve/{rev}/{file}"
_TOKENS = {False: (816730, "b34b360dbb493e781e479794586d661700670d65564001f23024971d1f2fa126"),
           True: (835554, "306cd27f03c1a714eca7108e03d66b7dc042abe8c258b44c199a7ed9838dd930")}


def _model(name: str, rev: str, enc: tuple[int, str], enc8: tuple[int, str], dec: tuple[int, str],
           dec8: tuple[int, str]) -> dict[str, Any]:
    return {"rev": rev, "tokens": (f"{name}-tokens.txt", *_TOKENS[name.endswith(".en")]),
            "gpu": ((f"{name}-encoder.onnx", *enc), (f"{name}-decoder.onnx", *dec)),
            "cpu": ((f"{name}-encoder.int8.onnx", *enc8), (f"{name}-decoder.int8.onnx", *dec8))}


MODELS: dict[str, dict[str, Any]] = {
    "tiny": _model("tiny", "65176e2deb88badc814a94058666cadccc29b61c",
                   (37647080, "42c1d4cbf889632ba21ab6f0d4064c80209755f265ce5cd630db4a6793e7089c"),
                   (12937772, "d24fb083ae3b1041fc24e97971d60e280c9342201fbb67b0ab428a8b4a51a434"),
                   (114505801, "e144c07dc6b55cece24392811f2d934b97013811f5e677d1315d341a0a74a25d"),
                   (89855401, "d2fece8dd42771f1df975c6c0445770d0c292bf7547c2cae04a6c0cc57540925")),
    "tiny.en": _model("tiny.en", "d026532c022fa99fd789d6b32446a1df7b6bfc43",
                      (37647080, "dc7de696432d97a3d64387800b230ac69d18e5e4efea0eec0613209dd8b7b0c9"),
                      (12937772, "0ce578b827c94a961aacb8fa14b02f096504b337e5c94be37c36238cbe3e8bc6"),
                      (114504265, "54d5a5fd2a757175aeb70a837a8404fc9c31b659610724694f5a0e2c51715f94"),
                      (89853865, "06c0e6ff6348d427e51839219d1c886c18cfdf411e629e33f5e1679bff9c1527")),
    "base": _model("base", "bb53ee204431c90d314c1cc08d28d23e5b7927cc",
                   (95087154, "5a6b87cb313993f6c9fefec9e7027556f6cb30becddf49655bee36c50ecc12d7"),
                   (29120534, "0b8fb1304b6109976038efff5ace81720e00386f3ff6b54ee8c75291ca0a1e11"),
                   (196548998, "8a12c3f6ad65bb5b86d7e6eccc302378f20f9fb2df6cb10747c62895da7ac194"),
                   (130672026, "9759d217388a01b3a4c7c15533201067b48ae819c4daafc8624e64b9409dc02d")),
    "base.en": _model("base.en", "59eea950fc76df2453efb57e6c0fd334548e8ffe",
                      (95087154, "f7eebd67fd79ede97c822439a3d88035ec26ac573db5e8a2a5dd45027e39c22c"),
                      (29120534, "ef6b936f4c9b1d90a3b68634b60c4ed8576b26172b33c2535ec0e933c9edb823"),
                      (196546950, "4f49d5d76f95e6f6da717e3d50e271f21fd2ecac10b7dc566ba6b14924c68792"),
                      (130669978, "f7162ad6db2dbef16cfaeaa7f945b9d7dd9c1b8d472f6aca82f2273d185e4d41")),
    "small": _model("small", "8f3c18b358db4d1f2fc1eae49d75cd20989e4309",
                    (409528992, "119bd1e8ba0524baee1687f6b22bf0abd2fe539549cd000734edbca81c66751e"),
                    (112442483, "4cbe7b22fa9026b843b60a68640c747de05bafb1a11b57edc0e66c232d9f33a9"),
                    (559127829, "a4165cca5c77e381938c0e111032a384901b1e434ae2ad948859035392d21d2c"),
                    (262226114, "acad50b5c782696e91b55914cc5ab4f756f1532f76e22aa6fc615f39fb69a8ee")),
    "small.en": _model("small.en", "d9533f69affd85061aee349af7fea5cb2996dbbe",
                       (409528992, "e6e3d58bc6600d54f18b982a800c3cb56c62b014da127ef4adb175e6d28f2702"),
                       (112442483, "8bdac288f369aa94ee2194059238c465ed82ea9d47ee8fa4a8c0a891873e462f"),
                       (559124757, "619aa554c30f6440d1c087e72695a65b2d9cfdfb415ef7152d652c2685ee4cc3"),
                       (262223042, "710ccf890e10f3faa15f51ec346081a2723c9f3adb6e4da81c6573a5a6f877fb")),
}
# Files the first version (faster-whisper) downloaded; removed once, when a model is fetched.
OLD_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")

# What Whisper "hears" in near-silence or noise. Dropped when that's all an utterance came out as.
HALLUCINATIONS = {"thank you.", "thank you", "thanks for watching!", "thanks for watching.", "you", "you.",
                  "bye.", "bye!", "so", "okay.", ".", "...", "[music]", "(music)", "[blank_audio]",
                  "subtitles by the amara.org community"}


def _mics() -> list[tuple[str, str]]:
    from sprigcast.sources.audio import device_label, list_audio_devices

    out = [("default", "Default mic")]
    for device in list_audio_devices(loopback=False, rescan=False):
        out.append((device.name, device_label(device.name)))
    return out


def google_code(language: str) -> str:
    return next((g for w, g, _ in LANGUAGES if w == language), "en-US")


def pick_model(choice: str, language: str, cpu_count: int | None = None) -> str:
    """The Whisper model for the Model setting: Auto is Balanced on a PC with 8 or more threads, else
    Light. English gets the English-only model, which is more accurate at the same size."""
    size = choice if choice in ("tiny", "base", "small") else ""
    if not size:
        size = "base" if (cpu_count if cpu_count is not None else os.cpu_count() or 2) >= 8 else "tiny"
    return f"{size}.en" if language == "en" else size


def model_files(name: str, device: str) -> list[tuple[str, int, str]]:
    """(file, size, sha256) of what this model needs on "gpu" or "cpu"."""
    spec = MODELS[name]
    return [*spec[device], spec["tokens"]]


def model_ready(name: str, device: str) -> bool:
    """Every file is here at its size (sprigcast.models checks hashes as they download)."""
    from sprigcast import models

    for file, size, _sha in model_files(name, device):
        path = models.path_for(f"whisper-onnx/{name}/{file}")
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


def forget_old_models() -> None:
    """Delete what the faster-whisper version downloaded (75-480 MB a model): nothing uses it now."""
    from sprigcast import models

    for name in MODELS:
        for file in OLD_FILES:
            try:
                models.remove(f"whisper-{name}/{file}")
            except (OSError, ValueError):
                pass


def fetch_model(name: str, device: str, progress: Callable[[int, int], None] | None = None,
                cancel: threading.Event | None = None) -> Path:
    """The model's folder, downloading what's missing first (in the shared models folder). Blocks.
    Raises models.ModelError on a failed or wrong download, InterruptedError when cancelled."""
    from sprigcast import models

    spec = MODELS[name]
    files = model_files(name, device)
    total = sum(size for _, size, _ in files)
    done_before = 0
    for file, size, sha in files:
        start = done_before
        step = (lambda d, _t, s=start: progress(s + d, total)) if progress else None
        models.ensure(f"whisper-onnx/{name}/{file}", HF.format(name=name, rev=spec["rev"], file=file), sha,
                      size=size, progress=step, cancel=cancel)
        done_before += size
        if progress:
            progress(done_before, total)
    forget_old_models()
    return models.path_for(f"whisper-onnx/{name}/{files[-1][0]}").parent


class Segmenter:
    """Finds speech in 16 kHz mono audio and hands it on as it goes.

    feed() returns events: ("partial", audio so far) about once a second while you talk, so captions
    keep up; ("final", the whole utterance) when you pause or it gets long. Quiet is told from speech
    by loudness against the room's own noise, which it keeps learning while you're quiet."""

    FRAME = 480  # 30 ms
    PREROLL = 10  # frames kept from before the speech started (300 ms), so first sounds aren't cut
    END_SILENCE = 0.7  # seconds of quiet that end an utterance
    MIN_SPEECH = 0.3  # seconds; shorter is a click or a cough

    def __init__(self, partial_every: float = 1.0, longest: float = 9.0):
        self.partial_every = partial_every
        self.longest = longest
        self.floor = 0.004
        self._left = np.zeros(0, np.float32)
        self._pre: list[np.ndarray] = []
        self._speech: list[np.ndarray] = []
        self._voiced = 0
        self._quiet = 0
        self._since_partial = 0

    @property
    def talking(self) -> bool:
        return bool(self._speech)

    def threshold(self) -> float:
        return max(0.01, self.floor * 3.0)

    def feed(self, samples: np.ndarray) -> list[tuple[str, np.ndarray]]:
        events: list[tuple[str, np.ndarray]] = []
        data = np.concatenate([self._left, samples.astype(np.float32, copy=False)])
        whole = len(data) // self.FRAME * self.FRAME
        self._left = data[whole:]
        fps = RATE / self.FRAME
        for start in range(0, whole, self.FRAME):
            frame = data[start:start + self.FRAME]
            rms = float(np.sqrt(np.mean(frame * frame)))
            loud = rms > self.threshold()
            if not self._speech:
                if loud:
                    self._speech = [*self._pre, frame]
                    self._pre = []
                    self._voiced, self._quiet, self._since_partial = 1, 0, 0
                else:
                    self.floor = self.floor * 0.98 + min(rms, 0.05) * 0.02
                    self._pre.append(frame)
                    del self._pre[:-self.PREROLL]
                continue
            self._speech.append(frame)
            self._since_partial += 1
            if loud:
                self._voiced += 1
                self._quiet = 0
            else:
                self._quiet += 1
            if self._quiet >= self.END_SILENCE * fps or len(self._speech) >= self.longest * fps:
                if self._voiced >= self.MIN_SPEECH * fps:
                    keep = len(self._speech) - max(0, self._quiet - 8)  # leave a little quiet at the end
                    events.append(("final", np.concatenate(self._speech[:keep])))
                self._speech, self._voiced, self._quiet = [], 0, 0
            elif self.partial_every and self._since_partial >= self.partial_every * fps \
                    and self._voiced >= self.MIN_SPEECH * fps:
                self._since_partial = 0
                events.append(("partial", np.concatenate(self._speech)))
        return events


def clean(text: str) -> str:
    text = " ".join(text.split())
    return "" if text.lower() in HALLUCINATIONS else text


class Board:
    """The words on the card: the last few things you said, then what you're saying now."""

    KEEP = 6

    def __init__(self):
        self.done: list[str] = []
        self.partial = ""
        self.changed = 0.0
        self.lock = threading.Lock()

    def say(self, text: str, final: bool, now: float) -> None:
        with self.lock:
            text = clean(text)
            if final:
                if text:
                    self.done = (self.done + [text])[-self.KEEP:]
                self.partial = ""
            else:
                self.partial = text
            self.changed = now

    def clear(self) -> None:
        with self.lock:
            self.done, self.partial = [], ""

    def text(self, now: float, hide_after: float) -> str:
        with self.lock:
            if hide_after > 0 and not self.partial and now - self.changed > hide_after:
                return ""
            return " ".join([*self.done, self.partial]).strip()


def wrap(words: str, fits: Callable[[str], bool], lines: int) -> list[str]:
    """Break the words into lines that fit and keep the last `lines` of them (the newest words)."""
    out: list[str] = []
    line = ""
    for word in words.split():
        attempt = f"{line} {word}" if line else word
        if line and not fits(attempt):
            out.append(line)
            line = word
        else:
            line = attempt
    if line:
        out.append(line)
    return out[-max(1, lines):]


N_FFT, HOP, N_MELS = 400, 160, 80
NO_SPEECH = 0.6  # above this, Whisper's "nobody is talking" wins and nothing is shown
TAIL = RATE * 2  # quiet added after what was said, so the model sees it end (less padding: it repeats itself)


def mel_filters(n_mels: int = N_MELS) -> np.ndarray:
    """Whisper's mel filter bank (Slaney scale, as librosa makes it), (n_mels, N_FFT // 2 + 1)."""
    fft_freqs = np.fft.rfftfreq(N_FFT, 1.0 / RATE)
    mels = np.linspace(0.0, 45.245640471924965, n_mels + 2)
    freqs = 200.0 / 3 * mels
    log_part = mels >= 15.0
    freqs[log_part] = 1000.0 * np.exp(np.log(6.4) / 27.0 * (mels[log_part] - 15.0))
    ramps = freqs[:, None] - fft_freqs[None, :]
    widths = np.diff(freqs)
    lower = -ramps[:-2] / widths[:-1, None]
    upper = ramps[2:] / widths[1:, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    weights *= (2.0 / (freqs[2:n_mels + 2] - freqs[:n_mels]))[:, None]
    return weights.astype(np.float32)


_FILTERS: np.ndarray | None = None


def log_mel(audio: np.ndarray) -> np.ndarray:
    """Whisper's input: the log-mel picture of the sound, (N_MELS, frames), scaled as it was trained."""
    global _FILTERS
    if _FILTERS is None:
        _FILTERS = mel_filters()
    audio = np.pad(audio.astype(np.float32), (0, TAIL))
    padded = np.pad(audio, N_FFT // 2, mode="reflect")
    frames = 1 + (len(padded) - N_FFT) // HOP
    windows = np.lib.stride_tricks.as_strided(padded, (frames, N_FFT), (padded.strides[0] * HOP, padded.strides[0]))
    spectrum = np.abs(np.fft.rfft(windows * np.hanning(N_FFT + 1)[:-1].astype(np.float32), axis=-1)) ** 2
    mel = _FILTERS @ spectrum[:-1].T.astype(np.float32)
    log = np.log10(np.maximum(mel, 1e-10))
    log = np.maximum(log, log.max() - 8.0)
    return ((log + 4.0) / 4.0).astype(np.float32)


def read_tokens(path: Path) -> dict[int, bytes]:
    """sherpa-onnx's tokens file: "<base64 of the token's bytes> <id>" per line."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text, _, number = line.rpartition(" ")
        if number.isdigit():
            out[int(number)] = base64.b64decode(text)
    return out


def repeat_start(tokens: list[int]) -> int | None:
    """Where the text started saying itself over again (Whisper's way of getting stuck), or None: a
    stretch of 8 or more tokens said twice in a row, or a short one three times. Cut it there."""
    n = len(tokens)
    for size in range(3, n // 2 + 1):
        tail = tokens[n - size:]
        if tokens[n - 2 * size:n - size] != tail:
            continue
        if size >= 8:
            return n - size
        if n >= 3 * size and tokens[n - 3 * size:n - 2 * size] == tail:
            return n - 2 * size
    return None


def bracketed(text: str) -> bool:
    """Whisper's notes about sounds rather than words: "[ Sound ]", "(music)", "*laughs*"."""
    t = text.strip()
    return len(t) > 1 and t[0] + t[-1] in ("[]", "()", "**", "♪♪")


def graphics_card() -> bool:
    """A graphics card worth handing the model to: an NVIDIA, AMD Radeon RX/Pro or Intel Arc card, and
    DirectML there to run on it. Built-in graphics stay free for the stream (the processor is fine)."""
    try:
        import onnxruntime as ort

        if "DmlExecutionProvider" not in ort.get_available_providers():
            return False
        from sprigcast.app.lowend import CARD_GPU, gpu_names

        return any(CARD_GPU.search(g) for g in gpu_names())
    except Exception:  # noqa: BLE001
        return False


class WhisperEngine:
    """Whisper through ONNX Runtime: the graphics card (DirectML) or a couple of processor threads."""

    def __init__(self, folder: Path, name: str, language: str, device: str):
        import onnxruntime as ort

        ort.set_default_logger_severity(3)
        self.device = "cpu"
        self.encoder = self.decoder = None
        if device == "gpu":
            try:
                self._open(ort, folder, name, "gpu")
                self.device = "gpu"
            except Exception as exc:  # noqa: BLE001 (an old driver: the processor still works)
                log.warning("captions: graphics card didn't start (%s); using the processor", exc)
                self.encoder = self.decoder = None
        if self.encoder is None:
            self._open(ort, folder, name, "cpu")
        meta = self.encoder.get_modelmeta().custom_metadata_map
        self.eot, self.sot = int(meta["eot"]), int(meta["sot"])
        self.no_speech = int(meta["no_speech"])
        self.no_timestamps, self.transcribe_id = int(meta["no_timestamps"]), int(meta["transcribe"])
        self.multilingual = meta.get("is_multilingual") == "1"
        codes = meta.get("all_language_codes", "").split(",")
        ids = [int(t) for t in meta.get("all_language_tokens", "").split(",") if t]
        self.languages = dict(zip(codes, ids))
        self.language = None if language == "auto" else language
        self.tokens = read_tokens(folder / MODELS[name]["tokens"][0])

    def _open(self, ort, folder: Path, name: str, device: str) -> None:
        encoder, decoder = (folder / file for file, _, _ in MODELS[name][device])
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.enable_cpu_mem_arena = False  # memory goes back when the model is let go
        if device == "gpu":
            opts.enable_mem_pattern = False  # DirectML needs these two
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            providers = ["DmlExecutionProvider"]
        else:
            opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 4))  # most of the PC is the stream's
            opts.inter_op_num_threads = 1
            providers = ["CPUExecutionProvider"]
        self.encoder = ort.InferenceSession(str(encoder), opts, providers=providers)
        self.decoder = ort.InferenceSession(str(decoder), opts, providers=providers)
        meta = self.encoder.get_modelmeta().custom_metadata_map
        self.layers, self.width = int(meta["n_text_layer"]), int(meta["n_text_state"])
        self.context = int(meta.get("n_text_ctx", 448))
        if device == "gpu":  # a quick run, so a card that can't do it fails here and not on stream
            k, v = self.encoder.run(None, {"mel": np.zeros((1, N_MELS, 200), np.float32)})
            self._step([int(meta["sot"])], self._cache(), k, v, 0)

    def _cache(self):
        empty = np.zeros((self.layers, 1, self.context, self.width), np.float32)
        return empty, empty.copy()

    def _step(self, tokens: list[int], cache, cross_k, cross_v, offset: int):
        """Logits for each token given (len(tokens) x vocabulary) and the cache after them."""
        logits, k, v = self.decoder.run(None, {
            "tokens": np.array([tokens], np.int64), "in_n_layer_self_k_cache": cache[0],
            "in_n_layer_self_v_cache": cache[1], "n_layer_cross_k": cross_k, "n_layer_cross_v": cross_v,
            "offset": np.array([offset], np.int64)})
        return logits[0], (k, v)

    def prompt(self, cross_k, cross_v) -> list[int]:
        if not self.multilingual:
            return [self.sot, self.no_timestamps]
        language = self.languages.get(self.language or "")
        if language is None:  # Any: ask the model which language it hears
            logits, _ = self._step([self.sot], self._cache(), cross_k, cross_v, 0)
            ids = list(self.languages.values())
            language = ids[int(np.argmax(logits[-1][ids]))]
        return [self.sot, language, self.transcribe_id, self.no_timestamps]

    def transcribe(self, audio: np.ndarray) -> str:
        mel = log_mel(audio)[None, :, :3000]
        cross_k, cross_v = self.encoder.run(None, {"mel": mel})
        tokens = self.prompt(cross_k, cross_v)
        cache, offset, said = self._cache(), 0, []
        most = min(self.context - len(tokens) - 1, 12 + int(len(audio) / RATE * 8))  # nobody says 8 tokens a second
        step = tokens
        while len(said) < most:
            logits, cache = self._step(step, cache, cross_k, cross_v, offset)
            if not offset:  # Whisper's own guess, at the start, that nobody is talking
                first = logits[0].astype(np.float64)
                quiet = np.exp(first[self.no_speech] - first.max()) / np.exp(first - first.max()).sum()
                if quiet > NO_SPEECH:
                    return ""
            offset += len(step)
            token = int(np.argmax(logits[-1]))
            if token == self.eot:
                break
            said.append(token)
            cut = repeat_start(said)
            if cut is not None:
                del said[cut:]
                break
            step = [token]
        text = b"".join(self.tokens.get(t, b"") for t in said if t < self.eot).decode("utf-8", "replace").strip()
        return "" if bracketed(text) else text


class KeyRefused(Exception):
    """Google turned the key down: say so and stop asking for a while."""


class GoogleEngine:
    """Google Cloud Speech-to-Text with the streamer's own API key, one request per thing said."""

    partials = False  # Google bills each request, so only whole utterances are sent

    def __init__(self, key: str, language: str, opener=urllib.request.urlopen):
        self.key = key
        self.language = google_code(language)
        self.opener = opener

    def body(self, audio: np.ndarray) -> bytes:
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        return json.dumps({
            "config": {"encoding": "LINEAR16", "sampleRateHertz": RATE, "languageCode": self.language,
                       "enableAutomaticPunctuation": True},
            "audio": {"content": base64.b64encode(pcm).decode("ascii")},
        }).encode("utf-8")

    def transcribe(self, audio: np.ndarray) -> str:
        request = urllib.request.Request(GOOGLE_URL, data=self.body(audio), method="POST",
                                         headers={"Content-Type": "application/json", "X-Goog-Api-Key": self.key})
        try:
            with self.opener(request, timeout=15) as reply:
                data = json.loads(reply.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403):
                try:
                    message = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
                except Exception:
                    message = ""
                if exc.code == 403 or "API key" in message or exc.code == 401:
                    raise KeyRefused(message or f"HTTP {exc.code}") from None
            raise
        results = data.get("results") or []
        return " ".join(str((r.get("alternatives") or [{}])[0].get("transcript", "")).strip() for r in results)


def _colour(text: str, fallback: str):
    from PySide6.QtGui import QColor

    text = str(text or "").strip()
    if text.startswith("#") and len(text) == 9:  # #rrggbbaa
        c = QColor(text[:7])
        c.setAlpha(int(text[7:], 16))
        return c if c.isValid() else QColor(fallback)
    c = QColor(text)
    return c if c.isValid() else QColor(fallback)


LOOKS = (("card", "Card"), ("outline", "Outline"), ("shadow", "Shadow"), ("plain", "Plain"))
SIZES = (("small", "Small"), ("medium", "Medium"), ("large", "Large"))
ALIGNS = (("left", "Left"), ("center", "Centre"), ("right", "Right"))
DEFAULT_FONTS = ["Nunito", "Segoe UI Variable Display", "Segoe UI", "Inter", "Arial"]


def _fonts() -> list[tuple[str, str]]:
    """The fonts on this PC, A-Z, after SprigCast's own."""
    from PySide6.QtGui import QFontDatabase

    names = sorted({f for f in QFontDatabase.families() if not f.startswith("@")}, key=str.lower)
    return [("", "SprigCast (Nunito)")] + [(f, f) for f in names]


def _font(settings: dict[str, Any], pixels: float):
    from PySide6.QtGui import QFont

    font = QFont()
    chosen = str(settings.get("font", "") or "").strip()
    font.setFamilies([chosen, *DEFAULT_FONTS] if chosen else DEFAULT_FONTS)
    font.setWeight(QFont.Weight.Bold if settings.get("bold", True) else QFont.Weight.Normal)
    font.setPixelSize(max(12, round(pixels)))
    return font


def paint(words: str, settings: dict[str, Any]):
    """The captions as a QImage (premultiplied), W x H: the newest lines at the bottom of the box, in the
    chosen style (on a rounded card that hugs them, outlined, with a shadow, or plain), font, size and
    alignment. Nothing said: an empty, see-through box."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter, QPainterPath, QPen

    image = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    if settings.get("all_caps", False):
        words = words.upper()
    if not words:
        return image
    look = str(settings.get("look", "card"))
    align = str(settings.get("align", "center"))
    lines_wanted = max(1, min(3, int(settings.get("lines", 2) or 2)))
    scale = {"small": 0.8, "medium": 1.0, "large": 1.2}.get(str(settings.get("text_size", "medium")), 1.0)
    pixels = {1: 92, 2: 76, 3: 60}[lines_wanted] * scale
    card_on = look == "card"
    pad_x, pad_y, margin = (44, 22, 12) if card_on else (16, 8, 12)
    while True:  # shrink until the lines fit the box's height
        font = _font(settings, pixels)
        metrics = QFontMetricsF(font)
        line_h = metrics.height()
        if line_h * lines_wanted + 2 * pad_y <= H - 2 * margin or pixels <= 20:
            break
        pixels *= 0.92
    room = W - 2 * margin - 2 * pad_x
    lines = wrap(words, lambda t: metrics.horizontalAdvance(t) <= room, lines_wanted)
    text_w = max(metrics.horizontalAdvance(line) for line in lines)
    card_w = min(W - 2 * margin, text_w + 2 * pad_x)
    card_h = min(H - 2 * margin, line_h * len(lines) + 2 * pad_y)
    x0 = margin if align == "left" else W - margin - card_w if align == "right" else (W - card_w) / 2
    y0 = H - margin - card_h
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    if card_on:
        card = _colour(settings.get("card_color"), "#151c19d9")
        if card.alpha():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(card)
            p.drawRoundedRect(QRectF(x0, y0, card_w, card_h), 32, 32)
    text = _colour(settings.get("text_color"), "#f2f7f3")
    edge = _colour(settings.get("outline_color"), "#000000ff")
    for i, line in enumerate(lines):
        width = metrics.horizontalAdvance(line)
        left = x0 + pad_x + {"left": 0.0, "right": text_w - width}.get(align, (text_w - width) / 2)
        base = QPointF(left, y0 + pad_y + i * line_h + metrics.ascent())
        if look in ("outline", "shadow"):
            path = QPainterPath()
            path.addText(base, font, line)
            if look == "outline":
                p.strokePath(path, QPen(edge, max(3.0, pixels * 0.14), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))
            else:
                p.setPen(Qt.PenStyle.NoPen)
                for step, alpha in ((0.07, 0.35), (0.045, 0.55)):  # a soft shadow below and right
                    shade = QColor(edge)
                    shade.setAlphaF(edge.alphaF() * alpha)
                    p.setBrush(shade)
                    p.drawPath(path.translated(pixels * step, pixels * step))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(text)
            p.drawPath(path)
        else:
            p.setFont(font)
            p.setPen(text)
            p.drawText(base, line)
    p.end()
    return image


LOOK_KEYS = ("lines", "look", "font", "text_size", "bold", "all_caps", "align", "text_color", "card_color",
             "outline_color")  # a change to any of these draws the card again


@register(KIND)
class LiveCaptionsSource(Source):
    label = "Live captions"
    defaults = {"listen_to": "default", "engine": "pc", "model": "auto", "run_on": "auto", "language": "en", "lines": 2,
                "hide_after": 4.0, "look": "card", "font": "", "text_size": "medium", "bold": True, "all_caps": False,
                "align": "center", "text_color": "#f2f7f3", "card_color": "#151c19d9", "outline_color": "#000000ff"}
    choices = {
        "listen_to": _mics,
        "engine": (("pc", "On this PC"), ("google", "Google (your own key)")),
        "model": (("auto", "Auto"), ("tiny", "Light"), ("base", "Balanced"), ("small", "Sharper")),
        "run_on": (("auto", "Auto"), ("gpu", "Graphics card"), ("cpu", "Processor")),
        "language": tuple((w, name) for w, _, name in LANGUAGES),
        "lines": ((1, "1"), (2, "2"), (3, "3")),
        "look": LOOKS,
        "font": _fonts,
        "text_size": SIZES,
        "align": ALIGNS,
    }
    secrets = {"google_key": SECRET}
    secrets_after = "engine"  # the key box sits under Captions by
    sleeps = True  # off air it stops listening; a minute later the model goes too
    own_sprig = (W, H)
    own_sprig_corner = "bottom_center"
    channel_order = painted.Frame.channel_order

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._frame = painted.Frame()
        self._shown: tuple | None = None
        self.board = Board()
        self._lock = threading.Lock()
        # One listening thread for the source's whole life on air: it listens while _want is set, waits
        # PARK_SECONDS once it's cleared, then lets the model go and ends. Waking within that minute
        # carries on with the same thread and model, so blooms going on and off air never pile threads up.
        self._worker: threading.Thread | None = None
        self._want = threading.Event()
        self._halt: threading.Event | None = None  # set to stop the current listening (off air, new settings)
        self._dead = False
        self._engine = None
        self._engine_key: tuple | None = None
        self._hidden = False
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    # Running only while on air ---------------------------------------------------------------

    def start(self) -> None:
        self._listen()

    def wake(self) -> None:
        self._shown = None
        if not self._hidden:
            self._listen()

    def sleep(self) -> None:
        self._quiet()
        self._frame.release()
        self._shown = None

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        if hidden:
            self._quiet()
        else:
            self._listen()

    def stop(self) -> None:
        """Removed: stop listening now and let the model go (the thread ends straight away)."""
        with self._lock:
            self._dead = True
        self._quiet()
        self._want.set()  # wakes a parked thread so it sees it's done
        with self._lock:
            worker = self._worker
        if worker is None:
            self._drop_engine()

    def update(self, settings: dict[str, Any]) -> None:
        listening = {k: settings.get(k) for k in ("listen_to", "engine", "model", "run_on", "language")}
        before = {k: self.settings.get(k) for k in listening}
        super().update(settings)
        self._shown = None
        if listening != before and self._want.is_set():
            self._quiet()  # start again with the new mic, model or language (same thread)
            self._listen()

    def _listen(self) -> None:
        with self._lock:
            if self._dead:
                return
            if self._halt is None or self._halt.is_set():
                self._halt = threading.Event()
            self._want.set()
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._life, name=f"captions-{self.id}", daemon=True)
                self._worker.start()

    def _quiet(self) -> None:
        with self._lock:
            self._want.clear()
            if self._halt is not None:
                self._halt.set()
            if not self._dead:
                self.status = "paused"  # off air: asleep, not a fault
        self.board.clear()

    def _drop_engine(self) -> None:
        self._engine, self._engine_key = None, None
        import gc

        gc.collect()  # the model's memory goes back to Windows now, not whenever Python next tidies up

    def _life(self) -> None:
        """The listening thread: listen while wanted; off air for PARK_SECONDS, let the model go and end."""
        try:
            while True:
                with self._lock:
                    if self._dead:
                        return
                    halt = self._halt if self._want.is_set() else None
                if halt is None or halt.is_set():
                    if not self._want.wait(PARK_SECONDS):
                        with self._lock:
                            if not self._want.is_set():
                                self._worker = None
                                return
                    continue
                self._run(halt)
                halt.wait()  # it gave up (no key, no model) while still on air: wait to be stopped or changed
        finally:
            with self._lock:
                if self._worker is threading.current_thread():
                    self._worker = None
            self._drop_engine()

    # The listening thread --------------------------------------------------------------------

    def _make_engine(self, stop: threading.Event):
        engine_kind = self.settings.get("engine", "pc")
        language = str(self.settings.get("language", "en"))
        if engine_kind == "google":
            from sprigcast.app.settings_dialog import read_secret

            key = read_secret(SECRET)
            if not key:
                self.status = "Add your Google key in Settings"
                return None
            return GoogleEngine(key, language)
        name = pick_model(str(self.settings.get("model", "auto")), language)
        run_on = str(self.settings.get("run_on", "auto"))
        device = "gpu" if run_on == "gpu" or (run_on == "auto" and graphics_card()) else "cpu"
        wanted = ("pc", name, language, device)
        if self._engine is not None and self._engine_key == wanted:
            return self._engine
        self._engine = None  # let the old one go before loading another
        if not model_ready(name, device):
            def progress(done: int, total: int) -> None:
                self.status = f"Getting the speech model ({done * 100 // max(1, total)}%)"
            log.info("captions: downloading the %s speech model for the %s", name, device)
        else:
            progress = None
        folder = fetch_model(name, device, progress, cancel=stop)
        self.status = "Loading the speech model"
        engine = WhisperEngine(folder, name, language, device)
        self._engine, self._engine_key = engine, wanted
        log.info("captions: %s model on the %s", name, "graphics card" if engine.device == "gpu" else "processor")
        return engine

    def _run(self, stop: threading.Event) -> None:
        from sprigcast.sources.audio import SAMPLE_RATE, AudioRing, MicSource

        try:
            engine = self._make_engine(stop)
        except InterruptedError:
            return
        except Exception as exc:
            log.warning("captions: couldn't get ready: %s", exc)
            self.status = f"Couldn't get the speech model. {exc}" if exc.__class__.__name__ == "ModelError" \
                else "Couldn't start captions"
            return
        if engine is None or stop.is_set():
            return
        partials = getattr(engine, "partials", True)
        segmenter = Segmenter(partial_every=1.0 if partials else 0.0, longest=9.0)
        mic = MicSource(f"{self.id}-mic", {"device": self.settings.get("listen_to", "default")})
        mic.ring = AudioRing(capacity=SAMPLE_RATE * 12)  # room for audio that arrives while words are worked out
        import soxr

        down = soxr.ResampleStream(SAMPLE_RATE, RATE, 1, dtype="float32")
        mic.start()
        refused_until = 0.0
        self.status = "Listening"
        try:
            while not stop.wait(0.05):
                n = len(mic.ring)
                if not n:
                    continue
                block = mic.ring.read(n)
                mono = down.resample_chunk(np.ascontiguousarray(block.mean(axis=1, dtype=np.float32)))
                events = segmenter.feed(mono)
                finals = [audio for kind, audio in events if kind == "final"]
                latest = next((audio for kind, audio in reversed(events) if kind == "partial"), None)
                jobs = [(a, True) for a in finals]
                if latest is not None and not finals:
                    jobs.append((latest, False))  # only the newest guess; older ones are already out of date
                for audio, final in jobs:
                    if stop.is_set() or time.monotonic() < refused_until:
                        break
                    try:
                        text = engine.transcribe(audio)
                    except KeyRefused as exc:
                        log.warning("captions: Google refused the key: %s", exc)
                        self.status = "Google didn't accept the key. Check it and Speech-to-Text in your project"
                        refused_until = time.monotonic() + 30
                        continue
                    except Exception as exc:
                        log.warning("captions: couldn't turn speech into words: %s", exc)
                        self.status = "Captions hit a problem; trying again"
                        continue
                    if not stop.is_set():
                        self.board.say(text, final, time.monotonic())
                        self.status = "Listening"
        finally:
            mic.stop()

    # Drawing -----------------------------------------------------------------------------------

    def video_frame(self) -> np.ndarray | None:
        hide_after = float(self.settings.get("hide_after", 4.0) or 0.0)
        words = self.board.text(time.monotonic(), hide_after)
        key = (words, *(self.settings.get(k) for k in LOOK_KEYS))
        if key == self._shown:
            return None  # nothing new said: nothing drawn, nothing uploaded
        self._shown = key
        return self._frame(paint(words, self.settings))
