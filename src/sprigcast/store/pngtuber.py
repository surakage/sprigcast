"""PNGtuber: your avatar pictures, talking when you talk.

Add PNGtuber (+ → Plugins → PNGtuber): it gets a sprig of its own in the bottom-right corner of your
bloom, in every shape. Move or resize it like anything else. With no folder picked it shows the
sample sloth.

Your model is a folder of pictures (PNG, GIF, WebP or APNG; GIFs play), named for what they show:
    idle.png        mouth closed (also: closed, quiet, rest, neutral)          needed
    talk.png        mouth open (also: talking, open, speaking)                  needed
    blink.png       mouth closed, eyes shut (also: idle_blink, eyes_closed)
    talk_blink.png  mouth open, eyes shut
    half.png        mouth half open, for soft syllables (also: half_open, mid)
    half_blink.png  half open, eyes shut
    yell.png        loud: shouting (also: scream, shout, loud)
    yell_blink.png  loud, eyes shut
    sleep.png       napping, after Naps after of quiet (also: asleep, nap, zzz)
An expression is a folder inside it with its own set (happy/idle.png, happy/talk.png...), or files
named with it in front (happy_idle.png, happy_talk.png). The pictures at the top are "default".
Anything missing borrows the nearest picture (no blink: it doesn't blink; no yell or half: it talks;
no sleep: it naps with its eyes shut).
Draw every picture the same size, so nothing jumps when it changes.
Pictures named anything else (Cozy.png, Pog.png...): pick them one by one in its settings (Idle, Talk,
Half open, Yell, Blink, Sleep; one picture for both Idle and Talk just bounces). Picked Idle and Talk
pictures replace the folder's own set; the others replace just that picture.
An outfit is a saved set of folder and pictures, put on with one click, a hotkey or an app.

It listens to a sound you already have in SprigCast (Auto: your mic), so it costs nothing extra and
goes quiet when you mute. It can also listen to a mic straight from Windows. Settings: Talks above
and Yells above (dB, with a live meter), Hold (seconds it stays talking after you stop, so it doesn't
flap between words), Naps after, Bounce (Hop, Squash or Off, and how strong), Idle sway, Shake on
yell and Blink.

Change expression with hotkeys (Tools → Hotkeys → Plugins), a right-click on it, the Stream Deck
PNGtuber key, or from an app:
    {"do": "pngtuber.expression", "expression": "happy"}                 until changed again
    {"do": "pngtuber.expression", "expression": "angry", "seconds": 10}  then back
    {"do": "pngtuber.next"}        the next expression (A-Z)
    {"do": "pngtuber.reset"}       back to default
    {"do": "pngtuber.expressions"} the list, and the one showing
    {"do": "pngtuber.react", "reaction": "sparkle"}   hop, sparkle or shake: for a follow, a sub, a raid
    {"do": "pngtuber.outfit", "outfit": "Cozy"}       put on a saved outfit
    {"do": "pngtuber.outfits"}     the saved outfits, and the one it's wearing
Streamer.bot (or StreamBridge) sends pngtuber.react when a follow, sub or raid comes in.

Hidden, or in a bloom that isn't showing, it stops listening; a minute later it lets the pictures go.

A SprigCast store plugin: add it from Tools → Plugin store.
"""

from __future__ import annotations

import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from sprigcast.control.native import ApiError, commit_settings, register_action, send_event
from sprigcast.core import drawn
from sprigcast.core.source import Source, register
from sprigcast.plugin_hooks import not_here, on_air, register_hotkeys, register_item_menu

PLUGIN = {"id": "pngtuber", "name": "PNGtuber", "version": 6, "adds": "Source",
          "about": "Your avatar pictures talk when you do: blinks, a little bounce and expressions.",
          "new": "A new settings card with a live mic meter, half-open, yell and sleep pictures, squash, sway "
                 "and shake, reactions for follows and subs, and outfits."}

log = logging.getLogger(__name__)

KIND = "pngtuber"
W, H = 640, 640  # the box the model is fitted into (feet on the bottom edge)
HOP = 36  # room above the model for its bounce, in box pixels
DEFAULT = "default"
PICTURES = {".png", ".gif", ".webp", ".apng"}
MOST_FRAMES = 90  # frames kept from one animated picture (longer ones skip frames evenly)
MOST_BYTES = 32 * 1024 * 1024  # all fitted frames together; past it animated pictures keep fewer frames

FADE = 0.04  # seconds to cross-fade between pictures (mouth open/closed): short, so it keeps up with syllables
EXPRESSION_FADE = 0.18
BLINK_FADE = 0.03
BLINK = 0.13  # seconds the eyes stay shut
HOP_TIME = 0.22  # one bounce, up and down
HYSTERESIS_DB = 4.0  # once talking, it keeps talking down to this much under Talks above
DROP_DB = 9.0  # the mouth shuts when the voice falls this far under the syllable's loudest (between syllables)
RISE_DB = 6.0  # and opens again once it rises this far above the dip (the next syllable)
PHRASE_GAP = 0.35  # quiet this long ends a sentence (the next one bounces again)
YELL_AFTER = 0.06  # seconds above Yells above before it yells (a clap isn't a yell)
HALF_DB = 8.0  # a syllable under this much over Talks above shows the half-open mouth (when there's a picture)
SIDE = 12  # room each side of the model for sway, shake and squash, in box pixels
SWAY_PX, SWAY_TIME = 5.0, 3.4  # idle sway: this far each way, once there and back in this many seconds
SHAKE_PX = 5.0  # shake on yell (and the Shake reaction): this far each way
SQUASH = 0.10  # squash bounce: this much shorter, then taller, at full strength
REACT_TIME = {"hop": HOP_TIME, "shake": 0.6, "sparkle": 1.1}
REACTIONS = ("hop", "sparkle", "shake")

ASSETS = Path(__file__).resolve().parent / "assets" / "pngtuber"

# File names -> what the picture shows. Compared without case, spaces, dashes or underscores.
STATES = ("idle", "talk", "half", "blink", "talk_blink", "half_blink", "yell", "yell_blink", "sleep")
PICKED = ("idle", "talk", "half", "yell", "blink", "sleep")  # the pictures you can pick one by one (<state>_picture)
_NAMES = {
    "idle": ("idle", "closed", "mouthclosed", "quiet", "rest", "neutral", "silent", "still", "notalk", "default"),
    "talk": ("talk", "talking", "open", "mouthopen", "speak", "speaking", "talk1"),
    "blink": ("blink", "idleblink", "blinking", "eyesclosed", "closedblink", "blinkclosed", "quietblink"),
    "talk_blink": ("talkblink", "talkingblink", "openblink", "speakblink", "speakingblink", "blinktalk"),
    "yell": ("yell", "yelling", "scream", "screaming", "shout", "shouting", "loud"),
    "yell_blink": ("yellblink", "screamblink", "shoutblink", "loudblink"),
    "half": ("half", "halfopen", "halftalk", "talkhalf", "mouthhalf", "mid", "midtalk", "little"),
    "half_blink": ("halfblink", "halfopenblink", "halftalkblink", "midblink"),
    "sleep": ("sleep", "sleeping", "asleep", "nap", "napping", "zzz", "snooze"),
}
_ALIAS = {name: state for state, names in _NAMES.items() for name in names}
# A missing picture borrows from these, in order (any of them may be missing too).
_BORROW = {
    "idle": ("talk",),
    "talk": ("idle",),
    "blink": ("idle",),
    "talk_blink": ("talk", "blink"),
    "yell": ("talk",),
    "yell_blink": ("yell", "talk_blink"),
    "half": ("talk",),
    "half_blink": ("half", "talk_blink"),
    "sleep": ("blink", "idle"),  # no sleep picture: it naps with its eyes shut
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def read_name(stem: str) -> tuple[str | None, str] | None:
    """(expression or None, state) from a file name without its extension, or None if it isn't one.
    "talk" -> (None, "talk"); "Happy_Talk_Blink" -> ("happy", "talk_blink")."""
    state = _ALIAS.get(_norm(stem))
    if state:
        return None, state
    tokens = [t for t in re.split(r"[\s_.\-]+", stem.strip().lower()) if t]
    for k in range(1, len(tokens)):
        state = _ALIAS.get(_norm("".join(tokens[k:])))
        if state:
            return " ".join(tokens[:k]), state
    return None


def _entries(folder: Path, want_dirs: bool) -> list[Path]:
    """Subfolders or files of a folder, A-Z. os.scandir knows each entry's type from the folder listing itself
    on Windows; Path.iterdir() then is_file() asked the disk once more per file (436 ms on the PC's window
    thread for a big emotes folder, 2026-11-18)."""
    found = []
    try:
        with os.scandir(folder) as listing:
            for entry in listing:
                try:
                    if entry.is_dir() if want_dirs else entry.is_file():
                        found.append(entry.path)
                except OSError:
                    continue
    except OSError:
        return []
    return sorted((Path(p) for p in found), key=lambda p: p.name.lower())


def _subfolders(folder: Path) -> list[Path]:
    return _entries(folder, True)


def _pictures(folder: Path) -> list[Path]:
    return [p for p in _entries(folder, False) if p.suffix.lower() in PICTURES]


def scan(folder: Path | None, picked: dict[str, Path] | None = None) -> dict[str, dict[str, Path]]:
    """Expression -> state -> picture, from a model folder. Only expressions with idle or talk are kept.
    picked: idle and talk pictures chosen one by one; they're the default expression (the folder's own
    default is left out, its blinks wouldn't match them). Doesn't open any picture, so it's quick enough
    to ask whenever the hotkey list is made."""
    found = _scan_folder(folder) if folder is not None else {}
    picked = {k: v for k, v in (picked or {}).items() if v.is_file()}
    if "idle" in picked or "talk" in picked:  # your own idle and talk: the folder's default is left out
        found[DEFAULT] = {**picked, "idle": picked.get("idle") or picked["talk"],
                          "talk": picked.get("talk") or picked["idle"]}
    elif picked and DEFAULT in found:  # a blink or a sleep picture of your own over the folder's
        found[DEFAULT] = {**found[DEFAULT], **picked}
    return found


def _scan_folder(folder: Path) -> dict[str, dict[str, Path]]:
    found: dict[str, dict[str, Path]] = {}
    loose = []
    for path in _pictures(folder):
        name = read_name(path.stem)
        if name is None:
            loose.append(path)
            continue
        expression, state = name
        found.setdefault(expression or DEFAULT, {}).setdefault(state, path)
    if not found and len(loose) == 2:
        found[DEFAULT] = {"idle": loose[0], "talk": loose[1]}  # two pictures named anything: quiet, then talking
    elif not found and len(loose) == 1:
        found[DEFAULT] = {"idle": loose[0]}  # just one: it bounces when you talk
    for sub in _subfolders(folder):
        states: dict[str, Path] = {}
        for path in _pictures(sub):
            name = read_name(path.stem)
            if name is not None:
                states.setdefault(name[1], path)
        if states:
            found.setdefault(sub.name.strip().lower(), {}).update(
                {k: v for k, v in states.items() if k not in found.get(sub.name.strip().lower(), {})})
    return {e: s for e, s in found.items() if "idle" in s or "talk" in s}


def pick_state(states, state: str) -> str | None:
    """The state to show for `state`, borrowing when its picture is missing."""
    seen: set[str] = set()
    todo = [state]
    while todo:
        s = todo.pop(0)
        if s in seen:
            continue
        seen.add(s)
        if s in states:
            return s
        todo.extend(_BORROW.get(s, ()))
    return None


def expression_order(names) -> list[str]:
    """Default first, then A-Z."""
    return sorted(names, key=lambda n: (n != DEFAULT, n.lower()))


def _path(settings: dict[str, Any], key: str) -> Path | None:
    text = str(settings.get(key, "") or "").strip().strip('"')
    return Path(text) if text else None


def picked_pictures(settings: dict[str, Any]) -> dict[str, Path]:
    """The pictures chosen one by one (state -> file), as set (found or not)."""
    out = {}
    for state in PICKED:
        path = _path(settings, f"{state}_picture")
        if path is not None:
            out[state] = path
    return out


def model_folder(settings: dict[str, Any]) -> Path | None:
    """The model's folder: the sample sloth when nothing is picked; None for picked pictures alone."""
    folder = _path(settings, "folder")
    if folder is not None:
        return folder
    picked = picked_pictures(settings)
    return None if "idle" in picked or "talk" in picked else _sample()


def model_key(settings: dict[str, Any]) -> tuple:
    """What the model is made from: a new one is loaded when this changes."""
    picked = picked_pictures(settings)
    return (model_folder(settings),) + tuple(picked.get(state) for state in PICKED)


def _name(path: Path) -> str:
    return path.name or str(path)


def what_is_missing(folder: Path | None, picked: dict[str, Path]) -> str:
    """Why there's no model, exactly: the file or folder that can't be found, or what the folder lacks."""
    for path in picked.values():
        if not path.is_file():
            return f"Can't find {_name(path)}"
    if folder is None:
        return "Pick an Idle picture"
    if not folder.is_dir():
        return f"Can't find the folder {_name(folder)}"
    if not _pictures(folder) and not any(_pictures(sub) for sub in _subfolders(folder)):
        return f"No pictures in {_name(folder)}"
    return f"No Idle or Talk picture in {_name(folder)}"


def _sample() -> Path:
    if (ASSETS / "sloth" / "idle.webp").exists():
        return ASSETS / "sloth"
    from sprigcast import plugin_store  # installed in the plugins folder: the art stays with SprigCast

    return plugin_store.STORE_DIR / "assets" / "pngtuber" / "sloth"


_SCANS: dict[tuple, tuple[float, dict]] = {}
_RESCANNING: set[tuple] = set()
SCAN_FRESH_S = 5.0


def expressions_in(settings: dict[str, Any]) -> list[str]:
    """The expressions of a source's model. Asked on the window's thread (hotkey lists, menus, the API): only
    the very first time is the folder read here. After that it answers from what it found last, and if that's
    over SCAN_FRESH_S old reads the folder again on a worker for next time."""
    key = model_key(settings)
    now = time.monotonic()
    cached = _SCANS.get(key)
    if cached is None:
        cached = _SCANS[key] = (now, scan(key[0], picked_pictures(settings)))
    elif now - cached[0] > SCAN_FRESH_S and key not in _RESCANNING:
        _RESCANNING.add(key)
        picked = picked_pictures(settings)

        def rescan():
            try:
                _SCANS[key] = (time.monotonic(), scan(key[0], picked))
            finally:
                _RESCANNING.discard(key)

        from sprigcast.core import workers

        try:
            workers.run(rescan, name="pngtuber folder")
        except RuntimeError:  # the pool is shutting down (SprigCast closing)
            _RESCANNING.discard(key)
    return expression_order(cached[1])


# Pictures -----------------------------------------------------------------------------------------


@dataclass
class Picture:
    """One state's picture, fitted, as premultiplied BGRA cut to what isn't see-through.
    frames: [(pixels, x, y)] in box pixels; times: when each frame ends (seconds, looping)."""

    frames: list[tuple[np.ndarray, int, int]]
    times: list[float] = field(default_factory=list)

    def frame_at(self, t: float) -> int:
        if len(self.frames) < 2 or not self.times:
            return 0
        t %= self.times[-1]
        for i, end in enumerate(self.times):
            if t < end:
                return i
        return len(self.frames) - 1


def diff_box(a: np.ndarray, b: np.ndarray) -> tuple[int, int, int, int] | None:
    """(top, left, bottom, right) around where two same-size pictures differ, or None if they don't.
    Differences too small to see (a WebP's or JPEG-ish noise) don't count."""
    changed = (np.abs(a.astype(np.int16) - b.astype(np.int16)) > 10).any(axis=2)
    rows, cols = np.nonzero(changed.any(axis=1))[0], np.nonzero(changed.any(axis=0))[0]
    if not len(rows):
        return None
    return int(rows[0]), int(cols[0]), int(rows[-1]) + 1, int(cols[-1]) + 1


@dataclass
class Model:
    folder: Path | None
    pictures: dict[str, dict[str, Picture]]  # expression -> state -> picture
    # Where pairs of still pictures differ, worked out while loading so a fade only blends that part.
    diffs: dict[tuple, tuple[int, int, int, int] | None] = field(default_factory=dict)
    # For the engine (frame_listed): each frame as a QImage, made the first time it shows, uploaded to the card once.
    arts: dict[tuple[int, int], object] = field(default_factory=dict)

    @property
    def expressions(self) -> list[str]:
        return expression_order(self.pictures)

    def picture(self, expression: str, state: str) -> Picture | None:
        states = self.pictures.get(expression) or self.pictures.get(DEFAULT) or next(iter(self.pictures.values()))
        picked = pick_state(states, state)
        return states[picked] if picked else None


class ModelError(Exception):
    pass


def _measure(path: Path) -> tuple[int, int, int]:
    """(width, height, frames) without decoding the picture."""
    from PIL import Image

    with Image.open(path) as img:
        return img.width, img.height, max(1, getattr(img, "n_frames", 1))


def _open_frames(path: Path, most: int, fit) -> tuple[list, list[float]]:
    """At most `most` of the picture's frames, each fitted by fit(Pillow RGBA frame) as soon as it's decoded
    (so a big picture is never held whole), and how long each shows (seconds)."""
    from PIL import Image, ImageSequence

    frames, durations = [], []
    with Image.open(path) as img:
        count = getattr(img, "n_frames", 1)
        step = max(1, math.ceil(count / max(1, most)))
        for i, frame in enumerate(ImageSequence.Iterator(img)):
            if i % step:
                if durations:
                    durations[-1] += max(0.02, (frame.info.get("duration") or 100) / 1000)
                continue
            frames.append(fit(frame.convert("RGBA")))
            durations.append(max(0.02, (frame.info.get("duration") or img.info.get("duration") or 100) / 1000))
    return frames, durations


def _fit(frame, scale: float, box: tuple[int, int, int]) -> tuple[np.ndarray, int, int]:
    """Scale a Pillow RGBA frame, stand it on the bottom edge, centred, and cut it to its visible part."""
    from PIL import Image

    w, h, room = box
    size = max(1, round(frame.width * scale)), max(1, round(frame.height * scale))
    img = frame.convert("RGBa")  # premultiplied: no dark fringes when scaled
    if size != img.size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    rgba = np.asarray(img, dtype=np.uint8)
    x0, y0 = (w - size[0]) // 2, h - size[1]
    alpha = rgba[..., 3]
    rows, cols = np.nonzero(alpha.any(axis=1))[0], np.nonzero(alpha.any(axis=0))[0]
    if not len(rows):
        return np.zeros((1, 1, 4), np.uint8), 0, h - 1
    top, bottom, left, right = rows[0], rows[-1] + 1, cols[0], cols[-1] + 1
    cut = rgba[top:bottom, left:right]
    bgra = np.ascontiguousarray(cut[..., [2, 1, 0, 3]])
    return bgra, x0 + int(left), y0 + int(top)


def load_model(folder: Path | None, box: tuple[int, int] = (W, H), picked: dict[str, Path] | None = None,
               warn=None) -> Model:
    """Open and fit every picture of the model (on a worker thread: Pillow lets other threads run).
    picked: idle and talk pictures chosen one by one (see scan). warn(text): where a picture that
    won't open is told (the log)."""
    warn = warn or log.warning
    found = scan(folder, picked)
    _SCANS[(folder,) + tuple((picked or {}).get(state) for state in PICKED)] = (time.monotonic(), found)
    if not found or any(not p.is_file() for p in (picked or {}).values()):
        raise ModelError(what_is_missing(folder, picked or {}))
    sizes: dict[tuple[str, str], tuple[int, int, int]] = {}
    for expression, states in found.items():
        for state, path in states.items():
            try:
                sizes[expression, state] = _measure(path)
            except Exception as exc:
                warn(f"pngtuber: couldn't open {path.name}: {exc}")
    if not any(state in ("idle", "talk") for _e, state in sizes):
        raise ModelError("Couldn't open the pictures")
    w, h = box
    widest = max(1, max(sw for sw, _sh, _n in sizes.values()))
    tallest = max(1, max(sh for _sw, sh, _n in sizes.values()))
    scale = min((w - 2 * SIDE) / widest, (h - HOP) / tallest)  # one scale for all, so every picture lines up
    # Frames are kept at the size they're shown, never the file's own; too many of them and animated
    # pictures keep fewer (evenly spaced, same length), so a folder of big animated emotes stays small.
    frame_bytes = {k: (sw * scale) * (sh * scale) * 4 for k, (sw, sh, _n) in sizes.items()}
    total = sum(frame_bytes[k] * min(n, MOST_FRAMES) for k, (_sw, _sh, n) in sizes.items())
    still = sum(frame_bytes[k] for k, (_sw, _sh, n) in sizes.items() if n == 1)
    thin = 1.0 if total <= MOST_BYTES else max(0.0, (MOST_BYTES - still) / max(1.0, total - still))
    pictures: dict[str, dict[str, Picture]] = {}
    for (expression, state), (_sw, _sh, n) in sizes.items():
        most = min(n, MOST_FRAMES) if n == 1 or thin >= 1.0 else max(2, int(min(n, MOST_FRAMES) * thin))
        try:
            fitted, durations = _open_frames(found[expression][state], most,
                                             lambda f: _fit(f, scale, (w, h, HOP)))
        except Exception as exc:
            warn(f"pngtuber: couldn't open {found[expression][state].name}: {exc}")
            continue
        if fitted:
            pictures.setdefault(expression, {})[state] = Picture(fitted, list(np.cumsum(durations)))
    pictures = {e: st for e, st in pictures.items() if "idle" in st or "talk" in st}
    if not pictures:
        raise ModelError("Couldn't open the pictures")
    model = Model(folder, pictures)
    stills = [p for states in pictures.values() for p in states.values() if len(p.frames) == 1]
    for i, a in enumerate(stills):
        for b in stills[i + 1:]:
            (pa, xa, ya), (pb, xb, yb) = a.frames[0], b.frames[0]
            if (xa, ya) == (xb, yb) and pa.shape == pb.shape:
                box = diff_box(pa, pb)
                model.diffs[(id(a), 0, id(b), 0)] = model.diffs[(id(b), 0, id(a), 0)] = box
    return model


# Hearing you ------------------------------------------------------------------------------------


def to_db(level: float) -> float:
    return -100.0 if level <= 1e-5 else 20 * math.log10(level)


class Voice:
    """The mouth follows the syllables: it opens when the voice rises above Talks above and shuts when the voice
    dips between syllables (well under that syllable's loudest) or goes quiet, staying open at least Hold seconds.
    Yelling is a moment above Yells above. `speaking` is the whole sentence, gaps between syllables included."""

    def __init__(self):
        self.speaking = False
        self.yelling = False
        self.open = False
        self._heard = -1e9  # last time it was loud enough to count as speaking
        self._opened = -1e9
        self._peak = -100.0  # loudest of the syllable the mouth is open for
        self._dip = 100.0  # quietest since the mouth shut
        self._loud_since: float | None = None
        self._yelled = -1e9

    def feed(self, level: float, now: float, talk_at: float, yell_at: float, hold: float) -> str:
        db = to_db(level)
        if db >= talk_at - HYSTERESIS_DB:
            self._heard = now
        if self.open:
            self._peak = max(self._peak, db)
            if now - self._opened >= hold and (db < talk_at - HYSTERESIS_DB or db < self._peak - DROP_DB):
                self.open, self._dip = False, db
        else:
            self._dip = min(self._dip, db)
            if db >= talk_at and (not self.speaking or db >= self._dip + RISE_DB):
                self.open, self._opened, self._peak = True, now, db
        if self.open:
            self.speaking = True
        elif self.speaking and now - self._heard > PHRASE_GAP:
            self.speaking = False
        if db >= yell_at:
            if self._loud_since is None:
                self._loud_since = now
            if now - self._loud_since >= YELL_AFTER:
                self.yelling = True
            if self.yelling:
                self._yelled = now
        else:
            self._loud_since = None
            if self.yelling and (db < yell_at - HYSTERESIS_DB or now - self._yelled > 0.25) \
                    and now - self._yelled > min(hold, 0.25):
                self.yelling = False
        if not self.speaking:
            self.yelling = False
        if not self.open:
            return "idle"
        return "yell" if self.yelling else "talk"

    @property
    def peak_db(self) -> float:
        """The loudest of the syllable the mouth is open for (a soft one shows the half-open mouth)."""
        return self._peak


class Blinker:
    """Shuts the eyes every few seconds, now and then twice in a row."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng or random.Random()
        self._start = -1.0
        self._next = -1.0

    def closed(self, now: float) -> bool:
        if self._next < 0:
            self._next = now + self.rng.uniform(1.0, 4.0)
        if now >= self._next:
            self._start = now
            double = self.rng.random() < 0.12
            self._next = now + (BLINK + 0.12 if double else self.rng.uniform(2.5, 6.0))
        return 0 <= now - self._start < BLINK


def bounce_style(settings: dict[str, Any]) -> str:
    """hop, squash or off (older settings only have Bounce on or off)."""
    style = str(settings.get("bounce_style") or "").lower()
    if style in ("hop", "squash", "off"):
        return style
    return "hop" if settings.get("bounce", True) else "off"


def squash_amount(t: float, strength: float) -> int:
    """How squashed it is t seconds into a squash bounce, in thousandths of its height: shorter first, then a
    little taller (never past the room above it), then back."""
    if t < 0 or t >= HOP_TIME * 1.4:
        return 0
    phase = math.sin(2 * math.pi * t / (HOP_TIME * 1.4))
    if phase >= 0:
        return int(round(1000 * SQUASH * strength * phase))
    return int(round(1000 * max(-HOP / H, -SQUASH * strength * 0.5) * -phase))


_SPARKLE_SPOTS = ((0.12, 0.18, 70), (0.86, 0.10, 84), (0.50, 0.02, 60), (0.04, 0.45, 56), (0.94, 0.40, 64),
                  (0.30, 0.04, 50), (0.70, 0.24, 46))  # where stars pop (share of the picture), and how big
_STAR = []


def _star():
    """A soft four-pointed star (premultiplied), made once."""
    if not _STAR:
        n = 64
        yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
        u, v = np.abs(xx - (n - 1) / 2) / (n / 2), np.abs(yy - (n - 1) / 2) / (n / 2)
        arms = np.clip(1 - (np.sqrt(u) + np.sqrt(v)) / 1.15, 0, 1) ** 0.6
        glow = np.clip(1 - np.hypot(u, v) * 1.4, 0, 1) ** 2 * 0.8
        a = np.clip(arms * 1.6 + glow, 0, 1)
        colour = np.array([200, 245, 255], np.float32)  # BGR: a warm white gold
        bgra = np.zeros((n, n, 4), np.uint8)
        bgra[..., :3] = (colour * a[..., None]).astype(np.uint8)
        bgra[..., 3] = (255 * a).astype(np.uint8)
        _STAR.append(_qimage(bgra, True))
    return _STAR[0]


def hop_height(t: float, height: float) -> int:
    """How high the bounce is t seconds after it started (0 when over)."""
    if t < 0 or t >= HOP_TIME:
        return 0
    return int(round(height * math.sin(math.pi * t / HOP_TIME)))


# What it listens to ------------------------------------------------------------------------------

SOUND = "sound:"
DEVICE = "device:"


def _listen_choices(collection=None) -> list[tuple[str, str]]:
    out = [("auto", "Auto (your mic)")]
    if collection is not None:
        sounds = [s for s in collection.sources if s.kind in ("audio_input", "audio_output", "app_audio")]
        for s in sorted(sounds, key=lambda s: s.name.lower()):
            out.append((SOUND + s.id, s.name))
    out.append((DEVICE + "default", "Default mic (from Windows)"))
    try:
        from sprigcast.sources.audio import device_label, list_audio_devices

        for device in list_audio_devices(loopback=False, rescan=False):
            out.append((DEVICE + device.name, f"{device_label(device.name)} (from Windows)"))
    except Exception:
        log.debug("pngtuber: couldn't list mics", exc_info=True)
    return out


def check_listen(col, value) -> str:
    """An app's Mic setting as one this understands: auto, a SprigCast sound (sound:<id>, or its id or name alone),
    or a Windows mic (device:<name>). Anything else is refused: before, it was kept and quietly heard as Auto."""
    text = str(value or "").strip()
    sounds = [s for s in getattr(col, "sources", []) or [] if s.kind in ("audio_input", "audio_output", "app_audio")]
    if text.lower() in ("", "auto", "mic", "auto (your mic)"):
        return "auto"
    if text.startswith(DEVICE) and len(text) > len(DEVICE):
        return text
    bare = text[len(SOUND):] if text.startswith(SOUND) else text
    for s in sounds:
        if bare == s.id or bare.lower() == s.id.lower() or bare.lower() == s.name.lower():
            return SOUND + s.id
    names = ", ".join(sorted(s.name for s in sounds)) or "none"
    raise ValueError(f"listen_to {text!r} isn't a sound here: use auto, a sound's name or id ({names}), "
                     f"or device:<Windows mic name>.")


def sound_for(setting: str, collection) -> str | None:
    """The id of the SprigCast sound to follow for this Mic setting, or None to open a mic itself."""
    setting = str(setting or "auto")
    if setting.startswith(DEVICE):
        return None
    sources = list(getattr(collection, "sources", []) or [])
    if setting.startswith(SOUND):
        wanted = setting[len(SOUND):]
        return wanted if any(s.id == wanted for s in sources) else None
    mics = [s for s in sources if s.kind == "audio_input"]
    voices = [s for s in mics if (s.settings or {}).get("duck_voice", True)]
    pick = sorted(voices or mics, key=lambda s: s.name.lower())
    return pick[0].id if pick else None


def _sound_level(sound_id: str) -> float:
    from sprigcast.core.filters import SOUND_LEVELS

    return SOUND_LEVELS.get(sound_id, 0.0)


def _quit_later(mic) -> None:
    """Close a mic without holding up the render thread (closing waits for its thread)."""
    threading.Thread(target=mic.stop, name="pngtuber-mic-close", daemon=True).start()


# The source --------------------------------------------------------------------------------------


@register(KIND)
class PngtuberSource(Source):
    label = "PNGtuber"
    defaults = {"folder": "", "idle_picture": "", "talk_picture": "", "half_picture": "", "yell_picture": "",
                "blink_picture": "", "sleep_picture": "", "listen_to": "auto", "talk_at": -38.0, "yell_at": -12.0,
                "hold": 0.08, "sleep_after": 0, "bounce": True, "bounce_style": "", "bounce_strength": 1.0,
                "sway": False, "shake": True, "blink": True, "outfits": [], "outfit": ""}
    choices = {"listen_to": _listen_choices}
    placeholders = {"folder": "Sloth (built in)"}  # an empty Folder is the sample sloth
    sleeps = True  # off air it stops listening and lets the pictures go
    slow_wake = True  # a switch to a bloom showing it waits (briefly) for its pictures to load again
    own_sprig = (W, H)
    own_sprig_corner = "bottom_right"  # bottom left is Now playing's
    channel_order = "pBGRA"

    @staticmethod
    def check_settings(col, settings: dict[str, Any]) -> dict[str, Any]:
        """For apps (source.set / source.new): Mic (listen_to) takes a sound's name or id and is kept as the
        choice it matches; anything unknown is refused, saying why."""
        if "listen_to" in settings:
            settings["listen_to"] = check_listen(col, settings["listen_to"])
        return settings

    def __init__(self, source_id: str, settings: dict[str, Any]):
        super().__init__(source_id, settings)
        self._lock = threading.Lock()
        self._model: Model | None = None
        self._model_key: tuple | None = None
        self._said: set[str] = set()  # problems already in the log (until a model loads)
        self._loading: tuple | None = None  # model_key being loaded
        self._failed: tuple | None = None
        self._failed_at = 0.0
        self._buf = np.zeros((H + HOP, W, 4), np.uint8)
        self._shown: tuple | None = None  # (picture key, fade step, hop) last sent
        self._key: tuple | None = None  # picture showing now
        self._key_state = "idle"
        self._from: tuple | None = None  # picture fading out
        self._fade_start = 0.0
        self._fade_len = FADE
        self._hop_start = -1.0
        self._hop_size = 0.0
        self._hop_style = "hop"
        self._quiet_since = time.monotonic()  # for Naps after
        self._reaction: tuple[str, float] | None = None  # (name, started)
        self._was = (False, False)  # (speaking, yelling) last frame
        self.voice = Voice()
        self.blinker = Blinker()
        self.expression = str(self.settings.get("expression") or DEFAULT).lower()
        self._revert: tuple[float, str] | None = None  # (when, expression) after a timed expression
        self._follow: str | None = None
        self._resolved = False  # the engine has said what to follow
        self._mic = None
        self._hidden = False
        self._diffs: dict[tuple, tuple[int, int, int, int] | None] = {}
        self.status = "ok"

    @property
    def size(self) -> tuple[int, int]:
        return W, H

    # Waking and resting -------------------------------------------------------------------------

    def start(self) -> None:
        self._get_model()

    def wake(self) -> None:
        self._shown = None
        self._quiet_since = time.monotonic()  # back on screen awake, not mid-nap
        self._get_model()

    def sleep(self) -> None:
        """Off air: stop listening and let the pictures go (they load again, quickly, on the way back)."""
        self._close_mic()
        self._shown = None
        self._let_go()

    def set_hidden(self, hidden: bool) -> None:
        self._hidden = hidden
        self._shown = None
        if hidden:
            self._close_mic()

    def stop(self) -> None:
        self._close_mic()
        self._model = None

    def update(self, settings: dict[str, Any]) -> None:
        old_key, old_listen = model_key(self.settings), self.settings.get("listen_to")
        super().update(settings)
        wanted = str(self.settings.get("expression") or DEFAULT).lower()
        if wanted != self.expression and self._revert is None:
            self.set_expression(wanted)
        if model_key(self.settings) != old_key:
            self._failed = None
            self._model = None
            self._get_model()
        if self.settings.get("listen_to") != old_listen:
            self._close_mic()  # opened again for the new mic, if it still needs one
        self._shown = None

    def _let_go(self) -> None:
        with self._lock:
            if self.asleep:
                self._model = None
                self._diffs.clear()
                self._key = self._from = None

    def _get_model(self) -> None:
        key = model_key(self.settings)
        model = self._model
        if model is not None and self._model_key == key:
            return
        with self._lock:
            if self._loading == key:
                return
            if self._failed == key and time.monotonic() - self._failed_at < 10:
                return  # looked a moment ago; pictures added to the folder are found on a later try
            self._loading = key
        threading.Thread(target=self._load, args=(key,), name=f"pngtuber-{self.id}", daemon=True).start()

    def _say(self, text: str, level: int = logging.WARNING) -> None:
        """Log it, unless it was said already: a folder looked at again every 10 s says it once."""
        if text not in self._said:
            self._said.add(text)
            log.log(level, text)

    def _load(self, key: tuple) -> None:
        folder = key[0]
        picked = {state: path for state, path in zip(PICKED, key[1:]) if path is not None}
        idle, talk = picked.get("idle"), picked.get("talk")
        try:
            model = load_model(folder, picked=picked, warn=self._say)
        except ModelError as exc:
            self.status = str(exc)
            self._failed, self._failed_at = key, time.monotonic()
            self._say(f"pngtuber: no model: {exc}", logging.INFO)
            model = None
        except Exception as exc:
            self._say(f"pngtuber: couldn't load {folder}: {exc}")
            self.status = "Couldn't open the pictures"
            self._failed, self._failed_at = key, time.monotonic()
            model = None
        with self._lock:
            self._loading = None
            if model is not None and model_key(self.settings) == key and not self.asleep:
                self._model_key = key
                self._model = model
                self._diffs.clear()
                self._key = self._from = None
                self._shown = None
                self.status = "ok"
                self._said.clear()  # a problem coming back later is said again
                log.info("pngtuber: model %s, expressions %s", _name(folder or idle or talk),
                         ", ".join(model.expressions))

    # Listening ----------------------------------------------------------------------------------

    def sound_to_follow(self, collection) -> str | None:
        """Asked by the engine whenever blooms change: the SprigCast sound whose level this follows."""
        self._follow = sound_for(self.settings.get("listen_to", "auto"), collection)
        self._resolved = True
        if self._follow is not None:
            self._close_mic()
        return self._follow

    def _device(self) -> str:
        setting = str(self.settings.get("listen_to") or "auto")
        return setting[len(DEVICE):] if setting.startswith(DEVICE) else "default"

    def _level(self) -> float:
        if self._follow is not None:
            return _sound_level(self._follow)
        if not self._resolved or self._hidden:
            return 0.0
        mic = self._mic
        if mic is None:
            from sprigcast.sources.audio import SAMPLE_RATE, AudioRing, MicSource

            mic = MicSource(f"{self.id}-mic", {"device": self._device()})
            mic.ring = AudioRing(capacity=SAMPLE_RATE // 4)  # a quarter second is plenty to measure
            mic.start()
            self._mic = mic
            return 0.0
        n = len(mic.ring)
        if not n:
            return 0.0
        block = mic.ring.read(n)
        return float(np.max(np.abs(block))) if block.size else 0.0

    def _close_mic(self) -> None:
        mic, self._mic = self._mic, None
        if mic is not None:
            _quit_later(mic)

    # Expressions --------------------------------------------------------------------------------

    def expressions(self) -> list[str]:
        model = self._model
        return model.expressions if model is not None else expressions_in(self.settings)

    def set_expression(self, name: str, seconds: float = 0.0) -> str:
        """Show this expression (for seconds, then the one before). Returns the expression now."""
        name = str(name or DEFAULT).lower()
        if seconds > 0:
            back = self._revert[1] if self._revert is not None else self.expression
            self._revert = (time.monotonic() + seconds, back)
        else:
            self._revert = None
        self.expression = name
        return name

    # Drawing ------------------------------------------------------------------------------------

    def video_frame(self) -> np.ndarray | None:
        if self._hidden and self._shown is not None:
            return None  # its eye is off in Build: nothing to show, nothing to work out
        model = self._model
        if model is None:
            self._get_model()
            if self._loading is not None:
                return None  # on its way (back from sleep): a bloom switch waits for it, see slow_wake
            if self._shown != ("empty",):
                self._shown = ("empty",)
                self._buf.fill(0)
                return self._buf[:H]
            return None
        now = time.monotonic()
        if self._revert is not None and now >= self._revert[0]:
            self.expression, self._revert = self._revert[1], None
        s = self.settings
        talk_at = _num(s.get("talk_at"), -38.0)
        state = self.voice.feed(self._level(), now, talk_at, _num(s.get("yell_at"), -12.0),
                                max(0.0, _num(s.get("hold"), 0.08)))
        voice = self.voice
        if state == "talk" and voice.peak_db < talk_at + HALF_DB:
            state = "half"  # a soft syllable: the half-open mouth (the talk picture when there's none)
        # one hop as a sentence starts (not every syllable), a bigger one as a yell starts
        was_speaking, was_yelling = self._was
        style = bounce_style(s)
        strength = min(2.0, max(0.25, _num(s.get("bounce_strength"), 1.0)))
        if ((voice.speaking and not was_speaking) or (voice.yelling and not was_yelling)) \
                and style != "off" and now - self._hop_start > HOP_TIME * 0.6:
            self._start_hop(now, style, (1.0 if voice.yelling else 0.55) * strength)
        self._was = (voice.speaking, voice.yelling)
        if voice.speaking:
            self._quiet_since = now
        nap = _num(s.get("sleep_after"), 0.0)
        if nap > 0 and now - self._quiet_since >= nap:
            state = "sleep"  # a long quiet: it naps (its eyes shut when there's no sleep picture)
        elif s.get("blink", True) and self.blinker.closed(now):
            state = "blink" if state == "idle" else f"{state}_blink"
        reaction = self._reaction
        if reaction is not None and now - reaction[1] >= REACT_TIME[reaction[0]]:
            self._reaction = reaction = None
        expression = self.expression if self.expression in model.pictures else DEFAULT
        picture = model.picture(expression, state)
        if picture is None:
            return None
        key = (expression, id(picture), picture.frame_at(now))
        if key != self._key:
            if self._key is not None:
                self._from = self._key
                self._fade_start = now
                same_face = self._key[0] == expression
                blink_only = ("blink" in state) != ("blink" in self._key_state) and \
                    state.replace("_blink", "").replace("blink", "idle") == \
                    self._key_state.replace("_blink", "").replace("blink", "idle")
                slow = "sleep" in (state, self._key_state)
                self._fade_len = EXPRESSION_FADE if not same_face or slow else BLINK_FADE if blink_only else FADE
                if self._key[1] == id(picture):
                    self._from = None  # an animated picture's next frame: no fade
            self._key, self._key_state = key, state
        f = 1.0
        if self._from is not None:
            f = (now - self._fade_start) / self._fade_len
            if f >= 1.0:
                self._from, f = None, 1.0
        step = 256 if self._from is None else max(0, min(255, int(f * 256)))
        hop, squash = 0, 0
        if self._hop_start >= 0:
            if self._hop_style == "squash":
                squash = squash_amount(now - self._hop_start, self._hop_size)
            else:
                hop = hop_height(now - self._hop_start, min(HOP, HOP * self._hop_size))
        if not drawn.wanted():  # the old engine's buffer: only the hop (see _compose)
            shown = (key, self._from, step, hop)
            if shown == self._shown:
                return None
            if self._shown is None or self._shown[:3] != shown[:3]:
                self._compose(model, key, self._from, step)
            self._shown = shown
            return self._buf[hop:hop + H]
        dx = 0
        if s.get("sway", False) and state not in ("sleep",):
            dx += round(SWAY_PX * math.sin(2 * math.pi * now / SWAY_TIME))
        if (s.get("shake", True) and voice.yelling) or (reaction is not None and reaction[0] == "shake"):
            dx += round(SHAKE_PX * math.sin(2 * math.pi * now * 13))
        sparkle = -1
        if reaction is not None and reaction[0] == "sparkle":
            sparkle = int((now - reaction[1]) * 60)  # a new look each frame while it sparkles
        shown = (key, self._from, step, hop, dx, squash, sparkle)
        if shown == self._shown:
            return None  # nothing moved: nothing drawn, nothing uploaded
        self._shown = shown
        d = self._listed(model, key, self._from, step, hop, dx, squash)
        if sparkle >= 0:
            self._sparkles(d, model, key, now - reaction[1])
        return d

    def _start_hop(self, now: float, style: str, size: float) -> None:
        self._hop_start, self._hop_style, self._hop_size = now, style, size

    def react(self, name: str) -> str:
        """Play a reaction now: hop, sparkle or shake (a follow, a sub, a raid...)."""
        name = str(name or "").strip().lower()
        if name not in REACTIONS:
            raise ValueError(f"No reaction called {name!r}. There are: {', '.join(REACTIONS)}.")
        now = time.monotonic()
        if name == "hop":
            self._start_hop(now, "hop", 1.0)
        else:
            self._reaction = (name, now)
        self._quiet_since = now  # a reaction wakes it from a nap
        return name

    def _sparkles(self, d: drawn.Drawn, model: Model, key: tuple, t: float) -> None:
        """Little stars that pop around the model's head, each a moment after the last."""
        frame = self._frame_of(model, key)
        if frame is None:
            return
        pixels, x, y = frame
        h, w = pixels.shape[:2]
        star = _star()
        for i, (fx, fy, size) in enumerate(_SPARKLE_SPOTS):
            local = (t - i * 0.08) / 0.55
            if not 0 <= local < 1:
                continue
            grow = math.sin(math.pi * local)
            side = max(4.0, size * (0.5 + 0.5 * grow))
            cx = min(W - side / 2, max(side / 2, x + fx * w))
            cy = min(H - side / 2, max(side / 2, y + fy * h - local * 14))
            d.save()
            d.opacity(min(1.0, grow * 1.4))
            d.image("sparkle", star, cx - side / 2, cy - side / 2, side, side)
            d.restore()

    def _listed(self, model: Model, key: tuple, old: tuple | None, step: int, hop: int, dx: int = 0,
                squash: int = 0) -> drawn.Drawn:
        """What _compose puts in the buffer, as a list for the engine (core/drawn.py): the picture showing is art
        sent to the card once, so a bounce or a held mouth sends nothing; a fade sends only the part that blends
        (the mouth, the eyes), and the rest of the picture around it as crops of the art."""
        d = drawn.Drawn(W, H)
        new = self._frame_of(model, key)
        if new is None:
            return d
        if dx or squash:  # sway, shake and squash move the whole picture, about its feet
            d.translate(W / 2 + dx, H)
            sy = 1 - squash / 1000
            d.scale(1 + min(0.04, max(-0.04, (1 - sy) * 0.5)), sy)
            d.translate(-W / 2, -H)
        pixels, x, y = new
        h, w = pixels.shape[:2]
        y -= hop
        art_key, art = f"pic:{key[1]}:{key[2]}", self._art(model, key, pixels)
        before = self._frame_of(model, old) if old is not None and step < 256 else None
        if before is None:
            d.image(art_key, art, x, y, w, h)
            return d
        b_pixels, bx, by = before
        by -= hop
        if (bx, by) == (x, y) and b_pixels.shape == pixels.shape:
            box = self._diff(model, old, key, b_pixels, pixels)
            if box is None:
                d.image(art_key, art, x, y, w, h)
                return d
            t, l, b, r = box
            for px, py, pw, ph in ((0, 0, w, t), (0, b, w, h - b), (0, t, l, b - t), (r, t, w - r, b - t)):
                if pw > 0 and ph > 0:  # the picture around the part that fades
                    d.image(art_key, art, x + px, y + py, pw, ph, (px, py, pw, ph))
            a16 = pixels[t:b, l:r].astype(np.uint16)
            b16 = b_pixels[t:b, l:r].astype(np.uint16)
            d.image("fade", _qimage(((a16 * step + b16 * (256 - step)) >> 8).astype(np.uint8), True),
                    x + l, y + t, r - l, b - t)
            return d
        # Different shapes: both faded, over the box they cover together (nothing else is drawn).
        top, left = min(y, by), min(x, bx)
        bottom, right = max(y + h, by + b_pixels.shape[0]), max(x + w, bx + b_pixels.shape[1])
        area = np.zeros((bottom - top, right - left, 4), np.uint16)
        area[y - top:y - top + h, x - left:x - left + w] = pixels.astype(np.uint16) * step
        area[by - top:by - top + b_pixels.shape[0], bx - left:bx - left + b_pixels.shape[1]] += \
            b_pixels.astype(np.uint16) * (256 - step)
        d.image("fade", _qimage((area >> 8).astype(np.uint8), True), left, top, right - left, bottom - top)
        return d

    @staticmethod
    def _art(model: Model, key: tuple, pixels: np.ndarray):
        found = model.arts.get(key[1:])
        if found is None:
            if len(model.arts) > 2 * MOST_FRAMES:  # a long animation: its frames are sent again, not all kept
                model.arts.clear()
            found = model.arts[key[1:]] = _qimage(pixels, False)
        return found

    def _frame_of(self, model: Model, key: tuple):
        for states in model.pictures.values():
            for picture in states.values():
                if id(picture) == key[1]:
                    return picture.frames[min(key[2], len(picture.frames) - 1)]
        return None

    def _compose(self, model: Model, key: tuple, old: tuple | None, step: int) -> None:
        """Put the picture (cross-faded from the old one by step/256) into the buffer the frames come from."""
        buf = self._buf
        buf.fill(0)
        new = self._frame_of(model, key)
        if new is None:
            return
        pixels, x, y = new
        h, w = pixels.shape[:2]
        buf[y:y + h, x:x + w] = pixels
        if old is None or step >= 256:
            return
        before = self._frame_of(model, old)
        if before is None:
            return
        b_pixels, bx, by = before
        if (bx, by) == (x, y) and b_pixels.shape == pixels.shape:
            box = self._diff(model, old, key, b_pixels, pixels)  # only where they differ: the mouth, the eyes
            if box is None:
                return
            t, l, b, r = box
            a16 = pixels[t:b, l:r].astype(np.uint16)
            b16 = b_pixels[t:b, l:r].astype(np.uint16)
            buf[y + t:y + b, x + l:x + r] = ((a16 * step + b16 * (256 - step)) >> 8).astype(np.uint8)
            return
        # Different shapes (another expression drawn differently): fade the whole of both.
        top, left = min(y, by), min(x, bx)
        bottom = max(y + h, by + b_pixels.shape[0])
        right = max(x + w, bx + b_pixels.shape[1])
        area = buf[top:bottom, left:right].astype(np.uint16) * step
        under = np.zeros((bottom - top, right - left, 4), np.uint16)
        under[by - top:by - top + b_pixels.shape[0], bx - left:bx - left + b_pixels.shape[1]] = b_pixels
        area += under * (256 - step)
        buf[top:bottom, left:right] = (area >> 8).astype(np.uint8)

    def _diff(self, model: Model, a_key: tuple, b_key: tuple, a: np.ndarray, b: np.ndarray):
        pair = (a_key[1], a_key[2], b_key[1], b_key[2])
        if pair in model.diffs:
            return model.diffs[pair]
        if pair not in self._diffs:  # frames of animated pictures: worked out the first time they meet
            self._diffs[pair] = diff_box(a, b)
            if len(self._diffs) > 256:
                self._diffs.pop(next(iter(self._diffs)))
        return self._diffs[pair]


def _qimage(pixels: np.ndarray, copy: bool):
    """A premultiplied BGRA frame as a QImage (copy: its own pixels; else it keeps the frame alive and shares them)."""
    from PySide6.QtGui import QImage

    pixels = np.ascontiguousarray(pixels)
    h, w = pixels.shape[:2]
    image = QImage(pixels.data, w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied)
    if copy:
        return image.copy()
    image._pixels = pixels  # the QImage reads them: they live as long as it does
    return image


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# Expressions from apps, hotkeys and the right-click menu ------------------------------------------


NO_PICTURES = "Add pictures to the PNGtuber first (idle and talk)."


def _tubers(api, data: dict | None = None) -> list:
    if data and data.get("source") is not None:
        source = api._source(data, "source")
        if source.kind != KIND:
            raise ApiError("bad_request", f"{source.name} isn't a PNGtuber.")
        return [source]
    found = [s for s in api.col.sources if s.kind == KIND]
    if not found:
        raise ApiError("not_found", "Add a PNGtuber first (+ → Plugins → PNGtuber).")
    return found


def _choices_of(source) -> list[str]:
    return expressions_in(source.settings)


def change(api, sources, how, seconds: float = 0.0) -> dict:
    """Set each PNGtuber's expression to how(list of its expressions, the one now). Saved with your blooms
    unless it's only for some seconds. PNGtubers with no pictures yet are left alone."""
    named = [(source, _choices_of(source)) for source in sources]
    named = [(source, names) for source, names in named if names]
    if not named:
        raise ApiError("not_found", NO_PICTURES)
    shown = DEFAULT
    for source, names in named:
        now = str(source.settings.get("expression") or DEFAULT).lower()
        instance = api.host.source_instance(source.id)
        if instance is not None and hasattr(instance, "expression"):
            now = instance.expression
        shown = how(names, now)
        if seconds <= 0:
            source.settings = {**source.settings, "expression": shown}
        if instance is not None and hasattr(instance, "set_expression"):
            instance.set_expression(shown, seconds)
    if seconds <= 0:
        api.host.commit("sources")
    try:
        send_event(api, "pngtuber.expression", {"expression": shown})
    except Exception:
        log.debug("couldn't tell apps about the expression", exc_info=True)
    out = {"expression": shown, "expressions": named[0][1]}
    if seconds > 0:
        out["seconds"] = seconds
    return out


def _named(wanted: str):
    def how(names: list[str], _now: str) -> str:
        match = next((n for n in names if n == wanted.strip().lower()), None)
        if match is None:
            raise ApiError("not_found", f"No expression called {wanted!r}. This model has: {', '.join(names)}.")
        return match
    return how


def _next(names: list[str], now: str) -> str:
    return names[(names.index(now) + 1) % len(names)] if now in names else names[0]


def _seconds(data) -> float:
    value = data.get("seconds", 0) or 0
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ApiError("bad_request", "seconds must be a number.") from None
    if value < 0 or value > 3600:
        raise ApiError("bad_request", "seconds must be between 0 and 3600.")
    return value


def _expression(api, data):
    wanted = data.get("expression")
    if not isinstance(wanted, str) or not wanted.strip():
        raise ApiError("bad_request", "expression is needed (pngtuber.expressions lists them).")
    return change(api, _tubers(api, data), _named(wanted), _seconds(data))


def _next_action(api, data):
    return change(api, _tubers(api, data), _next, _seconds(data))


def _reset(api, data):
    return change(api, _tubers(api, data), lambda names, now: DEFAULT if DEFAULT in names else names[0])


def _list(api, data):
    source = _tubers(api, data)[0]
    instance = api.host.source_instance(source.id)
    now = getattr(instance, "expression", None) or str(source.settings.get("expression") or DEFAULT).lower()
    return {"expression": now, "expressions": _choices_of(source)}


_WHICH = {"source": "?which PNGtuber (default: all of them)"}
_FOR = {"seconds": "?only for this many seconds, then back (default: until changed)"}
register_action("pngtuber.expression", "Show one of the PNGtuber's expressions.",
                {"expression": "the expression's name", **_FOR, **_WHICH}, _expression)
register_action("pngtuber.next", "The PNGtuber's next expression (A-Z, after default).", {**_FOR, **_WHICH},
                _next_action)
register_action("pngtuber.reset", "Back to the PNGtuber's default expression.", dict(_WHICH), _reset)
register_action("pngtuber.expressions", "The PNGtuber's expressions and the one showing.", dict(_WHICH), _list)


def react(api, sources, name: str) -> dict:
    """Play a reaction (hop, sparkle, shake) on each PNGtuber that's running."""
    name = str(name or "").strip().lower()
    if name not in REACTIONS:
        raise ApiError("bad_request", f"No reaction called {name!r}. There are: {', '.join(REACTIONS)}.")
    for source in sources:
        instance = api.host.source_instance(source.id)
        if instance is not None and hasattr(instance, "react"):
            instance.react(name)
    return {"reaction": name}


def _react(api, data):
    return react(api, _tubers(api, data), data.get("reaction"))


def outfits_of(settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in (settings.get("outfits") or []) if isinstance(o, dict) and str(o.get("name", "")).strip()]


OUTFIT_KEYS = ("folder",) + tuple(f"{state}_picture" for state in PICKED)


def outfit_name_for(pictures: dict[str, Any]) -> str:
    """A name to offer for a new outfit: its folder's name, else its idle picture's."""
    for key in ("folder", "idle_picture", "talk_picture"):
        text = str(pictures.get(key, "") or "").strip().strip('"')
        if text:
            path = Path(text)
            return (path.name if key == "folder" else path.stem) or "Outfit"
    return "Sloth"


def wear(api, sources, wanted: str) -> dict:
    """Put on the outfit called `wanted` (its folder and pictures), saved with your blooms."""
    wanted = str(wanted or "").strip().lower()
    changed, names = [], []
    for source in sources:
        outfits = outfits_of(source.settings)
        names += [o["name"] for o in outfits if o["name"] not in names]
        outfit = next((o for o in outfits if str(o["name"]).strip().lower() == wanted), None)
        if outfit is None:
            continue
        source.settings = {**source.settings, **{k: str(outfit.get(k, "") or "") for k in OUTFIT_KEYS},
                           "outfit": outfit["name"]}
        changed.append(source)
    if not changed:
        if not names:
            raise ApiError("not_found", "Save an outfit first (the PNGtuber's settings → + Save as outfit).")
        raise ApiError("not_found", f"No outfit called {wanted!r}. There are: {', '.join(names)}.")
    commit_settings(api.host, changed, "sources")
    return {"outfit": changed[0].settings["outfit"], "outfits": names}


def _outfit(api, data):
    wanted = data.get("outfit")
    if not isinstance(wanted, str) or not wanted.strip():
        raise ApiError("bad_request", "outfit is needed (pngtuber.outfits lists them).")
    return wear(api, _tubers(api, data), wanted)


def _outfit_list(api, data):
    source = _tubers(api, data)[0]
    return {"outfit": str(source.settings.get("outfit") or ""), "outfits": [o["name"] for o in outfits_of(source.settings)]}


register_action("pngtuber.react", "A quick PNGtuber reaction, for a follow, a sub or a raid: hop, sparkle or shake.",
                {"reaction": "hop, sparkle or shake", **_WHICH}, _react)
register_action("pngtuber.outfit", "Put on one of the PNGtuber's saved outfits.", {"outfit": "the outfit's name", **_WHICH},
                _outfit)
register_action("pngtuber.outfits", "The PNGtuber's saved outfits and the one it's wearing.", dict(_WHICH), _outfit_list)


def _hotkeys(collection):
    tubers = [s for s in collection.sources if s.kind == KIND]
    if not tubers:
        return []
    names: list[str] = []
    for source in tubers:
        names += [n for n in expressions_in(source.settings) if n not in names]
    keys = [(f"pngtuber:expression:{n}", f"PNGtuber: {n}") for n in expression_order(names)]
    keys += [("pngtuber:next", "PNGtuber: next expression")]
    keys += [(f"pngtuber:react:{r}", f"PNGtuber reaction: {r}") for r in REACTIONS]
    outfits: list[str] = []
    for source in tubers:
        outfits += [o["name"] for o in outfits_of(source.settings) if o["name"] not in outfits]
    return keys + [(f"pngtuber:outfit:{n}", f"PNGtuber outfit: {n}") for n in sorted(outfits, key=str.lower)]


def _run_hotkey(api, hid):
    try:
        tubers = _tubers(api)
        if hid == "pngtuber:next":
            change(api, tubers, _next)
        elif hid.startswith("pngtuber:react:"):
            react(api, tubers, hid.split(":", 2)[2])
        elif hid.startswith("pngtuber:outfit:"):
            wanted = hid.split(":", 2)[2]
            have = [s for s in tubers if any(o["name"] == wanted for o in outfits_of(s.settings))]
            if not have:
                return f"No {wanted} outfit in this PNGtuber"
            wear(api, have, wanted)
            tubers = have
        elif hid.startswith("pngtuber:expression:"):
            wanted = hid.split(":", 2)[2]
            have = [s for s in tubers if wanted in expressions_in(s.settings)]
            if not have:
                return f"No {wanted} expression in this PNGtuber"
            change(api, have, _named(wanted))
            tubers = have
        else:
            return None
    except ApiError as exc:
        log.info("pngtuber hotkey: %s", exc)
        return exc.message
    if not any(on_air(api.col, s.id) for s in tubers):
        return not_here(tubers[0].name)
    return None


register_hotkeys(_hotkeys, _run_hotkey)


def _menu(api, source, add):
    if source is None or source.kind != KIND:
        return
    names = expressions_in(source.settings)
    if len(names) >= 2:
        for name in names:
            add(f"Expression: {name}", lambda name=name: change(api, [source], _named(name)))
    for outfit in outfits_of(source.settings):
        add(f"Outfit: {outfit['name']}", lambda name=outfit["name"]: wear(api, [source], name))


register_item_menu(_menu)
