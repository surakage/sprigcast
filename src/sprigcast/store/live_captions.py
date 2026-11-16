"""Live captions: what you say, written on stream as you say it.

Add Live captions (+ → Plugins → Live captions): it gets a sprig of its own along the bottom of your
bloom, in every shape. Move or resize it like anything else.

Two ways to turn speech into words (Settings → Captions by):
- On this PC (the default): a small Whisper speech model runs on your own PC. Free, private and
  offline. The model downloads the first time captions go on air (75 MB for Light, 145 MB for
  Balanced, 480 MB for Sharper) and is kept for next time. Auto picks Light or Balanced for your PC.
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

PLUGIN = {"id": "live_captions", "name": "Live captions", "version": 2, "adds": "Source",
          "about": "What you say, as captions on stream. Runs on your PC, or with your own Google key.",
          "new": "Change how captions look: style, font, size, bold, all caps, alignment and outline colour."}

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

# Whisper models, converted for faster-whisper by Systran, pinned to one commit so they never change
# under us. Each file: (size, how it's checked). model.bin by SHA-256 (through sprigcast.models); the small
# files by the git blob id Hugging Face lists for them (SHA-1 of "blob <size>\0" + the file).
HF = "https://huggingface.co/Systran/faster-whisper-{name}/resolve/{rev}/{file}"
_TOK = {"": ("7818adb6de9fa3064d3ff81226fdd675be1f6344", 2203239, "c9074644d9d1205686f16d411564729461324b75", 459861),
        ".en": ("15d7bdf9ba25718ca2504eec6a8f02bc55af0a6a", 2128466, "ee695b8d3e3c10d488304e04468efec4ca27554a", 422309)}


def _model(rev: str, config: tuple[int, str], model: tuple[int, str], en: bool) -> dict[str, Any]:
    tok_oid, tok_size, vocab_oid, vocab_size = _TOK[".en" if en else ""]
    return {"rev": rev, "files": {
        "config.json": (config[0], "git", config[1]),
        "tokenizer.json": (tok_size, "git", tok_oid),
        "vocabulary.txt": (vocab_size, "git", vocab_oid),
        "model.bin": (model[0], "sha256", model[1]),
    }}


MODELS: dict[str, dict[str, Any]] = {
    "tiny": _model("d90ca5fe260221311c53c58e660288d3deb8d356", (2249, "3baa18e2b321a2f489614607852a729fcd516480"),
                   (75538270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"), False),
    "tiny.en": _model("0d3d19a32d3338f10357c0889762bd8d64bbdeba", (2317, "4065bb3bed375b176d5465be117d2d202e210434"),
                      (75537502, "1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a"), True),
    "base": _model("ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66", (2309, "867cf1a0fece1394e01d55e287ba2f09a577c046"),
                   (145217532, "d01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9"), False),
    "base.en": _model("3d3d5dee26484f91867d81cb899cfcf72b96be6c", (2227, "594369787efe617005d199b03739ee0ead7e3ab7"),
                      (145216508, "2a166925539a16005f14ff328359f9b9adb9dc4fb631bb3b227526862e93e2ef"), True),
    "small": _model("536b0662742c02347bc0e980a01041f333bce120", (2370, "e5047537059bd8f182d9ca64c470201585015187"),
                    (483546902, "3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671"), False),
    "small.en": _model("d1d751a5f8271d482d14ca55d9e2deeebbae577f", (2657, "ced3fd5bbb3e9f7664658065d2d36164712cf1d5"),
                       (483545366, "62b2a45b05ee59acb4a5341b33ee35e041395d378d418a18acfe4c9e768ee37a"), True),
}

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


def git_blob_id(path: Path) -> str:
    """The id git (and Hugging Face) gives a file: SHA-1 of "blob <size>\\0" and its bytes."""
    import hashlib

    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def model_ready(name: str) -> bool:
    """Every file of the model is here at its size (model.bin's hash is checked by sprigcast.models)."""
    from sprigcast import models

    for file, (size, _how, _check) in MODELS[name]["files"].items():
        path = models.path_for(f"whisper-{name}/{file}")
        if not path.is_file() or path.stat().st_size != size:
            return False
    return True


_small_lock = threading.Lock()


def fetch_model(name: str, progress: Callable[[int, int], None] | None = None,
                cancel: threading.Event | None = None, download=None) -> Path:
    """The model's folder, downloading what's missing first (in the shared models folder). Blocks.
    Raises models.ModelError on a failed or wrong download, InterruptedError when cancelled."""
    from sprigcast import models, update

    download = download or update.download
    spec = MODELS[name]
    files = spec["files"]
    total = sum(size for size, _, _ in files.values())
    done_before = 0
    for file, (size, how, check) in files.items():
        key = f"whisper-{name}/{file}"
        url = HF.format(name=name, rev=spec["rev"], file=file)
        start = done_before
        step = (lambda d, _t, s=start: progress(s + d, total)) if progress else None
        if how == "sha256":
            models.ensure(key, url, check, size=size, progress=step, cancel=cancel)
        else:
            path = models.path_for(key)
            with _small_lock:
                if not (path.is_file() and path.stat().st_size == size):
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("download cancelled")
                    try:
                        download(url, path, step, lambda: cancel is not None and cancel.is_set())
                    except InterruptedError:
                        raise
                    except OSError as exc:
                        raise models.ModelError("Couldn't download it. Check your internet and try again.") from exc
                    if path.stat().st_size != size or git_blob_id(path) != check:
                        path.unlink(missing_ok=True)
                        raise models.ModelError("The download didn't match. Try again later.")
        done_before += size
        if progress:
            progress(done_before, total)
    return models.path_for(f"whisper-{name}/config.json").parent


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


class WhisperEngine:
    """faster-whisper on this PC: the graphics card when CUDA works, otherwise a few CPU threads."""

    def __init__(self, folder: Path, language: str):
        from faster_whisper import WhisperModel

        self.language = None if language == "auto" else language
        self.device = "cpu"
        model = None
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                model = WhisperModel(str(folder), device="cuda", compute_type="float16")
                self._run(model, np.zeros(RATE, np.float32))  # CUDA's libraries load here; missing ones fail now
                self.device = "gpu"
        except Exception as exc:
            log.info("captions: graphics card not used (%s); using the CPU", exc)
            model = None
        if model is None:
            threads = max(1, min(4, (os.cpu_count() or 2) // 2))  # leave most of the PC to the stream
            model = WhisperModel(str(folder), device="cpu", compute_type="int8", cpu_threads=threads)
        self.model = model

    def _run(self, model, audio: np.ndarray) -> str:
        segments, _info = model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=False, condition_on_previous_text=False,
            without_timestamps=True, temperature=0.0)
        kept = [s.text for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)]
        return " ".join(t.strip() for t in kept)

    def transcribe(self, audio: np.ndarray) -> str:
        return self._run(self.model, audio)


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
    defaults = {"listen_to": "default", "engine": "pc", "model": "auto", "language": "en", "lines": 2,
                "hide_after": 4.0, "look": "card", "font": "", "text_size": "medium", "bold": True, "all_caps": False,
                "align": "center", "text_color": "#f2f7f3", "card_color": "#151c19d9", "outline_color": "#000000ff"}
    choices = {
        "listen_to": _mics,
        "engine": (("pc", "On this PC"), ("google", "Google (your own key)")),
        "model": (("auto", "Auto"), ("tiny", "Light"), ("base", "Balanced"), ("small", "Sharper")),
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
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._engine = None
        self._engine_key: tuple | None = None
        self._park: threading.Timer | None = None
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
        self._quiet()
        self._drop_engine()

    def update(self, settings: dict[str, Any]) -> None:
        listening = {k: settings.get(k) for k in ("listen_to", "engine", "model", "language")}
        before = {k: self.settings.get(k) for k in listening}
        super().update(settings)
        self._shown = None
        if listening != before and self._worker is not None:
            self._quiet()  # start again with the new mic, model or language
            self._listen()

    def _listen(self) -> None:
        with self._lock:
            if self._park is not None:
                self._park.cancel()
                self._park = None
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop = threading.Event()
            self._worker = threading.Thread(target=self._run, args=(self._stop,), name=f"captions-{self.id}",
                                            daemon=True)
            self._worker.start()

    def _quiet(self) -> None:
        with self._lock:
            worker, self._worker = self._worker, None
            self._stop.set()
            if worker is not None and self._park is None:
                self._park = threading.Timer(PARK_SECONDS, self._drop_engine)
                self._park.daemon = True
                self._park.start()
        self.board.clear()

    def _drop_engine(self) -> None:
        with self._lock:
            if self._worker is not None:
                return  # back on air: keep it
            self._engine, self._engine_key, self._park = None, None, None

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
        wanted = ("pc", name, language)
        if self._engine is not None and self._engine_key == wanted:
            return self._engine
        if not model_ready(name):
            def progress(done: int, total: int) -> None:
                self.status = f"Getting the speech model ({done * 100 // max(1, total)}%)"
            log.info("captions: downloading the %s speech model", name)
        else:
            progress = None
        folder = fetch_model(name, progress, cancel=stop)
        self.status = "Loading the speech model"
        engine = WhisperEngine(folder, language)
        self._engine, self._engine_key = engine, wanted
        log.info("captions: %s model on the %s", name, engine.device)
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
