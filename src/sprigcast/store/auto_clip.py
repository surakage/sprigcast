"""Auto clip: SprigCast listens for big moments and saves them as suggested clips for you to keep or delete.

While clips are ready (the Clip button), Auto clip follows how loud your mic and your game are against
their own usual level, and how fast chat is moving against its usual pace. A shout, a big explosion or
chat going wild saves the last seconds (a few seconds after the moment, so the reaction is in) as a
suggested clip. Nothing is posted or shared: suggestions wait in Tools → Suggested clips…, where you
play, keep or delete them. Kept clips move to your Clips folder; ones you leave are deleted after a
few days (3 unless you change it).

Settings (in the same window): what to listen for (Mic, Game, Chat), Sensitivity, the Gap between
two suggestions, and how many days unkept ones stay.

It costs next to nothing: it reads the sound levels the meters already have, about 15 times a second,
and does nothing at all while clips aren't ready. Chat counts only when Chat is ticked.

For apps (Streamer.bot and others): the event autoclip.suggested, and the actions autoclip.status,
autoclip.set, autoclip.list, autoclip.keep, autoclip.delete and autoclip.mark (suggest a clip now,
say on a raid).

A SprigCast store plugin: add it from Settings → Plugins.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.plugin_hooks import register_level_watcher, register_tool

PLUGIN = {"id": "auto_clip", "name": "Auto clip", "version": 1, "adds": "Tool",
          "about": "Saves loud moments and chat bursts as suggested clips for you to keep or delete."}

log = logging.getLogger(__name__)

FOLDER = "Suggested clips"  # in the recordings folder, beside Clips (Clips is tidied by Settings → Space)
INDEX = "suggested.json"
MOST = 50  # unkept suggestions held at most; the oldest go first
SENSITIVITY = ("low", "medium", "high")
REASONS = {"mic": "Loud mic", "game": "Loud game", "chat": "Chat burst", "mark": "Marked"}
DEFAULTS: dict[str, Any] = {"on": True, "mic": True, "game": True, "chat": False, "sensitivity": "medium",
                            "gap": 45, "days": 3}


# Spotting the moments -------------------------------------------------------------------------------

class SpikeDetector:
    """A sound that gets much louder than its own usual level, for a moment that isn't just a click.

    feed(RMS dBFS, now) about 15 times a second; it returns how many dB over the usual level when a
    spike starts, else None. The usual level is learned only from moments with sound in them (talking
    level, not the silence between), slowly, and never from a spike itself."""

    MARGIN = {"low": 15.0, "medium": 10.0, "high": 6.0}  # dB over the usual level
    LOUD_DB = -32.0  # and at least this loud, so a quiet room's small noises never count
    ACTIVE_DB = -50.0  # above the hiss: something is making sound
    WARMUP_S = 12.0  # seconds of sound heard before it trusts the usual level
    HOLD_S = 0.35  # over the line this long (a plosive or a desk knock is shorter)
    FAST_S, SLOW_S, LEARN_S = 0.25, 40.0, 5.0
    NEW_NORMAL_S = 8.0  # still over the line after this long: louder is the usual now (the game got louder)

    def __init__(self, sensitivity: str = "medium"):
        self.margin = self.MARGIN.get(sensitivity, self.MARGIN["medium"])
        self.fast: float | None = None
        self.usual: float | None = None
        self.heard_s = 0.0
        self.over_s = 0.0
        self.armed = True
        self.disarmed_s = 0.0
        self._last: float | None = None

    def feed(self, db: float, now: float) -> float | None:
        dt = 0.0 if self._last is None else max(0.0, min(now - self._last, 0.5))
        self._last = now
        db = max(float(db), -90.0)
        self.fast = db if self.fast is None else self.fast + (db - self.fast) * (1 - math.exp(-dt / self.FAST_S))
        if self.fast > self.ACTIVE_DB:
            self.heard_s += dt
            if self.usual is None:
                self.usual = self.fast
            elif self.armed and self.over_s == 0.0:
                tau = self.LEARN_S if self.heard_s < self.WARMUP_S else self.SLOW_S
                self.usual += (self.fast - self.usual) * (1 - math.exp(-dt / tau))
            elif not self.armed and self.disarmed_s > self.NEW_NORMAL_S:
                self.usual += (self.fast - self.usual) * (1 - math.exp(-dt / self.LEARN_S))
        if not self.armed:
            self.disarmed_s += dt
        if self.usual is None or self.heard_s < self.WARMUP_S:
            return None
        line = max(self.usual + self.margin, self.LOUD_DB)
        if self.fast >= line:
            self.over_s += dt
            if self.armed and self.over_s >= self.HOLD_S:
                self.armed = False
                return self.fast - self.usual
        else:
            self.over_s = 0.0
            if self.fast < max(self.usual + self.margin / 2, self.LOUD_DB - 6):
                self.armed, self.disarmed_s = True, 0.0
        return None


class ChatBurst:
    """Chat moving much faster than its usual pace: messages in the last WINDOW seconds against the
    usual count for that long, learned slowly while chat is calm."""

    WINDOW = 20.0
    RULES = {"low": (4.0, 12), "medium": (3.0, 8), "high": (2.0, 5)}  # (times the usual, and at least)
    WARMUP_S = 60.0
    LEARN_S = 180.0
    NEW_NORMAL_S = 60.0  # busy this long: this pace is the usual now (a raid that stays)

    def __init__(self, sensitivity: str = "medium"):
        self.times, self.least = self.RULES.get(sensitivity, self.RULES["medium"])
        self._at: deque[float] = deque()
        self.usual = 0.0
        self.armed = True
        self.disarmed_s = 0.0
        self._since: float | None = None
        self._last: float | None = None

    def message(self, now: float) -> None:
        self._at.append(now)
        while self._at and self._at[0] < now - self.WINDOW:
            self._at.popleft()

    def check(self, now: float) -> int | None:
        """The message count when a burst starts, else None."""
        if self._since is None:
            self._since = now
        while self._at and self._at[0] < now - self.WINDOW:
            self._at.popleft()
        count = len(self._at)
        dt = 0.0 if self._last is None else max(0.0, min(now - self._last, 1.0))
        self._last = now
        line = max(self.least, self.usual * self.times)
        if self.armed or self.disarmed_s > self.NEW_NORMAL_S:
            self.usual += (count - self.usual) * (1 - math.exp(-dt / self.LEARN_S))
        if not self.armed:
            self.disarmed_s += dt
        if now - self._since < self.WARMUP_S:
            return None
        if self.armed and count >= line:
            self.armed = False
            return count
        if not self.armed and count < line / 2:
            self.armed, self.disarmed_s = True, 0.0
        return None


# The suggestions -----------------------------------------------------------------------------------

class Suggestions:
    """The suggested clips in <recordings>/Suggested clips, listed in suggested.json (newest last)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.items: list[dict[str, Any]] = []
        self.load()

    @property
    def index(self) -> Path:
        return self.folder / INDEX

    def load(self) -> None:
        try:
            data = json.loads(self.index.read_text(encoding="utf-8"))
            items = data.get("clips", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            items = []
        self.items = [i for i in items if isinstance(i, dict) and i.get("id") and isinstance(i.get("files"), list)]

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clips": self.items}, indent=1), encoding="utf-8")
        tmp.replace(self.index)

    def add(self, files: list[Path], reason: str, over: float | None = None,
            when: datetime | None = None) -> dict[str, Any]:
        when = when or datetime.now()
        base = when.strftime("%Y%m%d-%H%M%S")
        taken = {i["id"] for i in self.items}
        sid, n = base, 1
        while sid in taken:
            n += 1
            sid = f"{base}-{n}"
        item = {"id": sid, "at": when.isoformat(timespec="seconds"), "reason": reason,
                "files": [str(f) for f in files]}
        if over is not None:
            item["over"] = round(float(over), 1)
        self.items.append(item)
        while len(self.items) > MOST:
            self._delete_files(self.items.pop(0))
        self.save()
        return item

    def find(self, sid: str) -> dict[str, Any] | None:
        return next((i for i in self.items if i["id"] == sid), None)

    def delete(self, sid: str) -> bool:
        item = self.find(sid)
        if item is None:
            return False
        self._delete_files(item)
        self.items.remove(item)
        self.save()
        return True

    def keep(self, sid: str, clips_folder: Path) -> list[Path]:
        """Move a suggestion's files into Clips/<its day>, as a clip you saved. Returns the new files."""
        item = self.find(sid)
        if item is None:
            return []
        from sprigcast.output import storage

        day = storage.day_folder(Path(clips_folder), _when(item))
        moved = []
        for name in item["files"]:
            path = Path(name)
            if not path.is_file():
                continue
            target = day / path.name
            n = 1
            while target.exists():
                n += 1
                target = day / f"{path.stem} ({n}){path.suffix}"
            shutil.move(str(path), str(target))
            moved.append(target)
        self.items.remove(item)
        self.save()
        self._tidy_days()
        return moved

    def expire(self, days: int, now: datetime | None = None) -> int:
        """Delete suggestions older than days (0 keeps them), and forget ones whose files are gone."""
        now = now or datetime.now()
        before = len(self.items)
        keep = []
        for item in self.items:
            old = days > 0 and now - _when(item) > timedelta(days=days)
            if old:
                self._delete_files(item)
            elif any(Path(f).is_file() for f in item["files"]):
                keep.append(item)
        if len(keep) != before:
            self.items = keep
            self.save()
            self._tidy_days()
        return before - len(keep)

    def _delete_files(self, item: dict[str, Any]) -> None:
        for name in item.get("files", []):
            try:
                Path(name).unlink(missing_ok=True)
            except OSError as exc:  # open in a player: it goes next time
                log.info("auto clip: couldn't delete %s: %s", name, exc)
        self._tidy_days()

    def _tidy_days(self) -> None:
        if not self.folder.is_dir():
            return
        for sub in self.folder.iterdir():
            if sub.is_dir():
                try:
                    sub.rmdir()  # only when empty
                except OSError:
                    pass


def _when(item: dict[str, Any]) -> datetime:
    try:
        return datetime.fromisoformat(str(item.get("at", "")))
    except ValueError:
        return datetime.now()


def describe(item: dict[str, Any]) -> dict[str, Any]:
    """A suggestion as apps get it."""
    return {"id": item["id"], "at": item["at"], "reason": item["reason"],
            "label": REASONS.get(item["reason"], item["reason"]), "files": list(item["files"])}


# Settings ------------------------------------------------------------------------------------------

def settings_path() -> Path:
    from sprigcast import models

    return models.folder().parent / "auto_clip.json"


def load_settings() -> dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    return clean_settings({**DEFAULTS, **(data if isinstance(data, dict) else {})})


def clean_settings(s: dict[str, Any]) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for key in ("on", "mic", "game", "chat"):
        out[key] = bool(s.get(key, DEFAULTS[key]))
    out["sensitivity"] = s.get("sensitivity") if s.get("sensitivity") in SENSITIVITY else DEFAULTS["sensitivity"]
    for key, low, high in (("gap", 10, 600), ("days", 0, 30)):
        try:
            out[key] = max(low, min(high, int(s.get(key, DEFAULTS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULTS[key]
    return out


def save_settings(s: dict[str, Any]) -> None:
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(s, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("auto clip: couldn't keep the settings: %s", exc)


# The watcher ---------------------------------------------------------------------------------------

def _groups(col) -> dict[str, set[str]]:
    """Source ids whose sound counts as "mic" (your voice) or "game" (everything else but music)."""
    from sprigcast.audio.mixer import is_music, is_voice

    mic, game = set(), set()
    for source in col.sources:
        try:
            if is_voice(source):
                mic.add(source.id)
            elif not is_music(source):
                game.add(source.id)
        except Exception:  # noqa: BLE001 - an odd source never stops the rest
            continue
    return {"mic": mic, "game": game}


class AutoClip:
    """Follows the levels and chat, and saves suggestions. Lives on the UI thread."""

    EXPIRE_EVERY = 3600.0

    def __init__(self, api):
        self.api = api
        self.settings = load_settings()
        self.detectors: dict[str, Any] = {}
        self.last_at = -1e9  # monotonic time of the last suggestion
        self.pending = False
        self._groups: dict[str, set[str]] = {}
        self._groups_of: tuple | None = None
        self._watching = False
        self._hub = None
        self._expired_at = -1e9
        self._suggestions: Suggestions | None = None
        self._listeners: list = []  # windows that redraw on a change
        self._done = None  # set up on first use: a Qt signal back to the UI thread
        self.reset()

    # Settings and state

    def set(self, **changes) -> dict[str, Any]:
        before = self.settings["sensitivity"]
        self.settings = clean_settings({**self.settings, **changes})
        save_settings(self.settings)
        if self.settings["sensitivity"] != before:
            self.reset()  # the usual levels learned so far still hold for the other settings
        self._sync_chat()
        self.changed()
        return self.settings

    def reset(self) -> None:
        """Fresh detectors (a new sensitivity, or clips just got ready: learn the usual levels again)."""
        sens = self.settings["sensitivity"]
        self.detectors = {"mic": SpikeDetector(sens), "game": SpikeDetector(sens), "chat": ChatBurst(sens)}

    def clips_ready(self) -> bool:
        try:
            return bool(self.api.host.clips_ready())
        except Exception:  # noqa: BLE001
            return False

    @property
    def suggestions(self) -> Suggestions:
        folder = Path(self.api.host.recordings_folder()) / FOLDER
        if self._suggestions is None or self._suggestions.folder != folder:
            self._suggestions = Suggestions(folder)
        return self._suggestions

    def changed(self) -> None:
        for fn in list(self._listeners):
            try:
                fn()
            except RuntimeError:  # its window has gone
                self._listeners.remove(fn)

    # Listening

    def on_levels(self, levels: dict, now: float | None = None) -> None:
        on = self.settings["on"] and self.clips_ready()
        if on != self._watching:
            self._watching = on
            self.reset()
            self._sync_chat()
            if on:
                self.expire()
        if not on:
            return
        now = time.monotonic() if now is None else now
        if now - self._expired_at > self.EXPIRE_EVERY:
            self.expire()
        groups = self._source_groups()
        found = None
        for name in ("mic", "game"):
            if not self.settings[name] or not groups.get(name):
                continue
            rms = [levels[sid][1] for sid in groups[name] if sid in levels]
            if not rms:
                continue
            over = self.detectors[name].feed(max(rms), now)
            if over is not None and found is None:
                found = (name, over)
        if self.settings["chat"]:
            count = self.detectors["chat"].check(now)
            if count is not None and found is None:
                found = ("chat", None)
        if found is not None and not self.pending and now - self.last_at >= self.settings["gap"]:
            self.suggest(found[0], found[1], now)

    def _source_groups(self) -> dict[str, set[str]]:
        col = self.api.col
        key = id(col), id(col.sources), len(col.sources)
        if key != self._groups_of:  # sources come and go rarely; work the groups out again only then
            self._groups, self._groups_of = _groups(col), key
        return self._groups

    def chat_message(self, message) -> None:
        import sys

        if __name__ not in sys.modules:  # the plugin was removed: let chat go
            self._watching = False
            self._sync_chat()
            return
        if "broadcaster" in getattr(message, "badges", ()):
            return  # your own messages aren't chat getting excited
        self.detectors["chat"].message(time.monotonic())

    def _sync_chat(self) -> None:
        """Read chat (and count it) only while clips are ready and Chat is ticked."""
        want = self._watching and self.settings["on"] and self.settings["chat"]
        if want and self._hub is None:
            hub = getattr(self.api.host, "chat_hub", lambda: None)()
            if hub is not None:
                hub.message.connect(self.chat_message)
                hub.want("auto_clip", True)
                self._hub = hub
        elif not want and self._hub is not None:
            try:
                self._hub.message.disconnect(self.chat_message)
                self._hub.want("auto_clip", False)
            except (RuntimeError, TypeError):
                pass
            self._hub = None

    # Saving

    def after_seconds(self) -> float:
        """Wait a little after the moment so the reaction is in the clip, but keep the moment itself."""
        try:
            seconds = float(self.api.col.recording.replay_seconds)
        except (AttributeError, TypeError, ValueError):
            seconds = 30.0
        return max(0.0, min(5.0, seconds / 3))

    def suggest(self, reason: str, over: float | None = None, now: float | None = None) -> bool:
        """Save a suggestion in a few seconds. False when one is already on its way or clips aren't ready."""
        if self.pending or not self.clips_ready():
            return False
        self.pending = True
        self.last_at = time.monotonic() if now is None else now
        log.info("auto clip: %s%s, saving a suggestion", reason, f" (+{over:.0f} dB)" if over else "")
        self._later(self.after_seconds(), lambda: self._save(reason, over))
        return True

    def _later(self, seconds: float, fn) -> None:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(int(seconds * 1000), fn)

    def _save(self, reason: str, over: float | None) -> None:
        if not self.clips_ready():
            self.pending = False
            return
        folder = self.suggestions.folder
        host = self.api.host

        def work():
            try:
                return host.save_clip_into(folder), None
            except Exception as exc:  # noqa: BLE001 - clips stopped meanwhile, disk full...
                return [], exc

        self._in_background(work, lambda result: self._saved(result, reason, over))

    def _in_background(self, work, then) -> None:
        """work() on a thread of its own (an encoder writes the clip; the window never waits), then(result)
        back on the UI thread."""
        if self._done is None:
            from PySide6.QtCore import QObject, Signal

            class _Done(QObject):
                ready = Signal(object, object)

            self._done = _Done()
            self._done.ready.connect(lambda fn, result: fn(result))
        done = self._done
        threading.Thread(target=lambda: done.ready.emit(then, work()), name="sprigcast-auto-clip",
                         daemon=True).start()

    def _saved(self, result, reason: str, over: float | None) -> None:
        self.pending = False
        files, error = result
        if error is not None or not files:
            log.warning("auto clip: couldn't save a suggestion: %s", error)
            return
        item = self.suggestions.add([Path(f) for f in files], reason, over)
        log.info("auto clip: suggested %s", ", ".join(Path(f).name for f in files))
        try:
            send_event(self.api, "autoclip.suggested", describe(item))
        except Exception:  # noqa: BLE001
            log.debug("couldn't tell apps about the suggestion", exc_info=True)
        say = getattr(self.api.host, "say", None)
        if say is not None:
            say(f"Suggested clip: {REASONS.get(reason, reason)}")
        self.changed()

    def expire(self) -> None:
        self._expired_at = time.monotonic()
        try:
            if self.suggestions.expire(self.settings["days"]):
                self.changed()
        except Exception:  # noqa: BLE001
            log.exception("auto clip: tidying suggestions failed")

    def keep(self, sid: str) -> list[Path]:
        moved = self.suggestions.keep(sid, Path(self.api.host.recordings_folder()) / "Clips")
        self.changed()
        return moved

    def delete(self, sid: str) -> bool:
        gone = self.suggestions.delete(sid)
        self.changed()
        return gone


_ONE: AutoClip | None = None


def controller(api) -> AutoClip:
    global _ONE
    if _ONE is None or _ONE.api is not api:
        _ONE = AutoClip(api)
    return _ONE


def _levels(api, levels):
    controller(api).on_levels(levels)


register_level_watcher(_levels)


# The window ----------------------------------------------------------------------------------------

_WINDOW = None


def open_window(api) -> None:
    global _WINDOW
    try:
        if _WINDOW is not None and _WINDOW.isVisible():
            _WINDOW.raise_()
            _WINDOW.activateWindow()
            return
    except RuntimeError:  # deleted on close
        pass
    _WINDOW = SuggestedClipsWindow(controller(api))
    _WINDOW.show()


register_tool("Suggested clips…", open_window)


def _day_time(item: dict[str, Any]) -> str:
    when = _when(item)
    today = datetime.now().date()
    day = "Today" if when.date() == today else "Yesterday" if when.date() == today - timedelta(days=1) \
        else when.strftime("%b %d").replace(" 0", " ")
    return f"{day}, {when.strftime('%H:%M')}"


def _window_class():
    from PySide6.QtCore import QUrl, Qt
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
                                   QScrollArea, QSpinBox, QVBoxLayout, QWidget)

    from sprigcast.app import theme
    from sprigcast.app.plugin_store_page import _card, _pill

    class Window(QDialog):
        """Tools → Suggested clips…: the suggestions (play, keep, delete) and what Auto clip listens for."""

        def __init__(self, auto: AutoClip):
            super().__init__(None)
            self.auto = auto
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)  # nothing stays behind (#381)
            self.setWindowTitle("Suggested clips")
            self.setMinimumSize(640, 560)
            self.setStyleSheet(f"QDialog {{ background: {theme.PANEL}; }}")
            outer = QVBoxLayout(self)
            outer.setContentsMargins(24, 24, 24, 24)
            outer.setSpacing(16)

            card, box = _card()
            top = QHBoxLayout()
            title = QLabel("Suggested clips")
            title.setStyleSheet(f"color: {theme.TEXT_BRIGHT}; font-size: 15px; font-weight: 600;")
            top.addWidget(title)
            top.addStretch(1)
            folder = _pill("Open folder")
            folder.clicked.connect(self._open_folder)
            top.addWidget(folder)
            box.addLayout(top)
            self.rows = QVBoxLayout()
            self.rows.setSpacing(8)
            holder = QWidget()
            holder.setLayout(self.rows)
            holder.setStyleSheet("background: transparent;")
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setStyleSheet("QScrollArea { background: transparent; }")
            scroll.setWidget(holder)
            box.addWidget(scroll, 1)
            outer.addWidget(card, 1)

            card, box = _card("Listen for")
            s = auto.settings
            self.on = QCheckBox("Suggest clips")
            self.on.setChecked(s["on"])
            box.addWidget(self.on)
            grid = QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(10)
            self.ticks = {}
            for col, key in enumerate(("chat", "game", "mic")):  # A-Z
                tick = QCheckBox(key.capitalize())
                tick.setChecked(s[key])
                tick.toggled.connect(lambda on, key=key: self.auto.set(**{key: on}))
                self.ticks[key] = tick
                grid.addWidget(tick, 0, col)
            self.sens = QComboBox()
            for value in SENSITIVITY:
                self.sens.addItem(value.capitalize(), value)
            self.sens.setCurrentIndex(SENSITIVITY.index(s["sensitivity"]))
            self.sens.currentIndexChanged.connect(lambda _i: self.auto.set(sensitivity=self.sens.currentData()))
            self.gap = QSpinBox()
            self.gap.setRange(10, 600)
            self.gap.setSuffix(" s")
            self.gap.setValue(s["gap"])
            self.gap.valueChanged.connect(lambda v: self.auto.set(gap=v))
            self.days = QSpinBox()
            self.days.setRange(0, 30)
            self.days.setSpecialValueText("Never")
            self.days.setSuffix(" days")
            self.days.setValue(s["days"])
            self.days.valueChanged.connect(lambda v: self.auto.set(days=v))
            for row, (label, widget) in enumerate((("Sensitivity", self.sens), ("Gap between clips", self.gap),
                                                   ("Delete unkept after", self.days)), start=1):
                grid.addWidget(QLabel(label), row, 0, 1, 2)
                grid.addWidget(widget, row, 2)
            box.addLayout(grid)
            self.on.toggled.connect(self._switched)
            self._switched(s["on"])
            outer.addWidget(card)

            auto._listeners.append(self._fill)
            self._fill()

        def _switched(self, on: bool) -> None:
            for widget in (*self.ticks.values(), self.sens, self.gap, self.days):
                widget.setEnabled(on)
            if on != self.auto.settings["on"]:
                self.auto.set(on=on)

        def _open_folder(self) -> None:
            folder = self.auto.suggestions.folder
            folder.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

        def _fill(self) -> None:
            while self.rows.count():
                item = self.rows.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            items = list(reversed(self.auto.suggestions.items))  # newest first
            if not items:
                empty = QLabel("None yet.")
                empty.setStyleSheet(theme.MUTED_STYLE)
                self.rows.addWidget(empty)
            for item in items:
                self.rows.addWidget(self._row(item))
            self.rows.addStretch(1)

        def _row(self, item: dict[str, Any]) -> QWidget:
            row = QFrame()
            row.setObjectName("clipRow")
            row.setStyleSheet(f"#clipRow {{ background: {theme.PANEL}; border-radius: 14px; }}")
            line = QHBoxLayout(row)
            line.setContentsMargins(18, 10, 12, 10)
            line.setSpacing(10)
            what = QLabel(REASONS.get(item["reason"], item["reason"]))
            what.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
            when = QLabel(_day_time(item))
            when.setStyleSheet(theme.MUTED_STYLE)
            line.addWidget(what)
            line.addWidget(when)
            line.addStretch(1)
            for text, primary, fn in (("Play", False, self._play), ("Keep", True, self._keep),
                                      ("Delete", False, self._delete)):
                button = _pill(text, primary)
                button.clicked.connect(lambda _=False, fn=fn, sid=item["id"]: fn(sid))
                line.addWidget(button)
            return row

        def _play(self, sid: str) -> None:
            item = self.auto.suggestions.find(sid)
            files = [f for f in (item or {}).get("files", []) if Path(f).is_file()]
            if files:
                QDesktopServices.openUrl(QUrl.fromLocalFile(files[0]))

        def _keep(self, sid: str) -> None:
            self.auto.keep(sid)

        def _delete(self, sid: str) -> None:
            self.auto.delete(sid)

        def closeEvent(self, event) -> None:
            if self._fill in self.auto._listeners:
                self.auto._listeners.remove(self._fill)
            super().closeEvent(event)

    return Window


def SuggestedClipsWindow(auto: AutoClip):  # noqa: N802 - made like a class
    return _window_class()(auto)


# For apps ------------------------------------------------------------------------------------------

def _bool(data: dict, key: str) -> bool | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.lower() in ("true", "false", "on", "off", "1", "0"):
        return value.lower() in ("true", "on", "1")  # web links send text
    if isinstance(value, bool):
        return value
    raise ApiError("bad_request", f"{key} must be true or false.")


def _status(api, data):
    auto = controller(api)
    return {**auto.settings, "watching": auto.settings["on"] and auto.clips_ready(),
            "suggestions": len(auto.suggestions.items)}


def _set(api, data):
    changes: dict[str, Any] = {}
    for key in ("on", "mic", "game", "chat"):
        value = _bool(data, key)
        if value is not None:
            changes[key] = value
    if data.get("sensitivity") is not None:
        sens = str(data["sensitivity"]).lower()
        if sens not in SENSITIVITY:
            raise ApiError("bad_request", "sensitivity is low, medium or high.")
        changes["sensitivity"] = sens
    for key in ("gap", "days"):
        if data.get(key) is not None:
            try:
                changes[key] = int(data[key])
            except (TypeError, ValueError):
                raise ApiError("bad_request", f"{key} must be a whole number.") from None
    controller(api).set(**changes)
    return _status(api, data)


def _list(api, data):
    return {"clips": [describe(i) for i in reversed(controller(api).suggestions.items)]}


def _id(data) -> str:
    sid = data.get("id")
    if not sid:
        raise ApiError("bad_request", "id is the suggestion's id (autoclip.list).")
    return str(sid)


def _keep(api, data):
    auto = controller(api)
    sid = _id(data)
    if auto.suggestions.find(sid) is None:
        raise ApiError("not_found", "No suggested clip with that id.")
    return {"files": [str(p) for p in auto.keep(sid)]}


def _delete(api, data):
    if not controller(api).delete(_id(data)):
        raise ApiError("not_found", "No suggested clip with that id.")
    return {}


def _mark(api, data):
    auto = controller(api)
    if not auto.clips_ready():
        raise ApiError("not_allowed", "Clips aren't ready (press Clip first).")
    if not auto.suggest("mark"):
        raise ApiError("not_allowed", "A suggested clip is already being saved.")
    return {"in": auto.after_seconds()}


register_action("autoclip.status", "Auto clip's settings, whether it's listening now, and how many suggestions wait.",
                {}, _status)
register_action("autoclip.set", "Change Auto clip's settings.",
                {"on": "?true or false", "mic": "?listen to your mic", "game": "?listen to other sound",
                 "chat": "?count chat", "sensitivity": "?low, medium or high", "gap": "?seconds between suggestions",
                 "days": "?delete unkept suggestions after this many days (0: never)"}, _set)
register_action("autoclip.list", "The suggested clips, newest first.", {}, _list)
register_action("autoclip.keep", "Keep a suggested clip: it moves to your Clips folder.",
                {"id": "the suggestion's id"}, _keep)
register_action("autoclip.delete", "Delete a suggested clip.", {"id": "the suggestion's id"}, _delete)
register_action("autoclip.mark", "Suggest a clip now (a few seconds from now, so what follows is in it).", {}, _mark)
