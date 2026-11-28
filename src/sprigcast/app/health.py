"""Stream health: how the last stream went, kept so it can be read back without opening the logs.

While live, `SessionHealth` watches each destination (frames, drops, reconnects, auto-pilot), the
render loop (hitches: a second with more than one late frame) and SprigCast's memory. When the stream
ends it is saved to QSettings ("health/sessions", the last few streams) and Live's Health pop-up
shows it. `notice()` is the short line Live shows on the stage while a destination reconnects or
auto-pilot has lowered its bitrate.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

KEEP = 5  # streams remembered
KEY = "health/sessions"


@dataclass
class PlaceHealth:
    name: str
    frames: int = 0
    dropped: int = 0
    reconnects: int = 0
    lowest_kbps: int = 0  # auto-pilot's lowest bitrate; 0 when it never stepped down
    target_kbps: int = 0
    steps_down: int = 0

    @property
    def dropped_pct(self) -> float:
        return 100 * self.dropped / max(1, self.frames + self.dropped)


@dataclass
class HealthRecord:
    started: str  # local time, "2026-11-15 20:10"
    seconds: int = 0
    places: list[PlaceHealth] = field(default_factory=list)
    peak_mb: int = 0
    hitches: int = 0  # seconds with more than one late frame
    late_frames: int = 0

    @property
    def dropped(self) -> int:
        return sum(p.dropped for p in self.places)

    @property
    def reconnects(self) -> int:
        return sum(p.reconnects for p in self.places)

    def verdict(self) -> tuple[str, str]:
        """(level, one short line) for the top of the page."""
        if self.reconnects or any(p.dropped_pct > 1 for p in self.places):
            return "bad", "Rough: the connection struggled."
        if self.dropped or self.hitches > max(3, self.seconds // 600) or any(p.steps_down for p in self.places):
            return "warning", "Mostly smooth, with a few bumps."
        return "good", "Smooth all the way."

    @classmethod
    def from_dict(cls, data: dict) -> HealthRecord:
        places = [PlaceHealth(**p) for p in data.get("places", [])]
        return cls(**{**data, "places": places})


class SessionHealth:
    """Watches one stream. Call tick() about once a second and render() with each render Stats."""

    def __init__(self, session, clock=time.monotonic, now: datetime | None = None, memory_mb=None):
        self.session = session
        self.clock = clock
        self._started = clock()
        self.record = HealthRecord(started=f"{(now or datetime.now()):%Y-%m-%d %H:%M}")
        self._late_before: int | None = None
        self._memory_mb = memory_mb or _memory_mb

    def tick(self) -> None:
        self.record.seconds = int(self.clock() - self._started)
        places = []
        for _, st in self.session.streamers:
            pilot = getattr(st, "autopilot", None)
            places.append(PlaceHealth(
                name=st.name, frames=int(getattr(st, "frames", 0)), dropped=int(getattr(st, "dropped", 0)),
                reconnects=int(getattr(st, "reconnects", 0)),
                lowest_kbps=pilot.lowest_kbps if pilot is not None and pilot.steps_down else 0,
                target_kbps=pilot.target_kbps if pilot is not None else int(getattr(st, "bitrate_kbps", 0) or 0),
                steps_down=pilot.steps_down if pilot is not None else 0))
        self.record.places = places
        mb = self._memory_mb()
        if mb:
            self.record.peak_mb = max(self.record.peak_mb, int(mb))

    def render(self, stats) -> None:
        """A render Stats event (about once a second): count late frames and hitches."""
        late_total = int(getattr(stats, "dropped_ticks", 0))
        if self._late_before is None:
            self._late_before = late_total
            return
        late = max(0, late_total - self._late_before)
        self._late_before = late_total
        self.record.late_frames += late
        if late > 1:
            self.record.hitches += 1

    def finish(self, settings) -> HealthRecord:
        self.tick()
        save(settings, self.record)
        return self.record


def _memory_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2**20
    except Exception:
        return 0.0


def save(settings, record: HealthRecord) -> None:
    sessions = [record] + load(settings)
    settings.setValue(KEY, json.dumps([asdict(r) for r in sessions[:KEEP]]))


def load(settings) -> list[HealthRecord]:
    """The remembered streams, newest first."""
    try:
        raw = json.loads(str(settings.value(KEY, "[]") or "[]"))
        return [HealthRecord.from_dict(r) for r in raw]
    except (ValueError, TypeError, KeyError):
        return []


OFFLINE_WORDS = "can't reach the internet"  # output/stream.py retry_plan puts it in the status
BACK_LIVE_S = 8.0  # how long "Back live" shows after a place reconnects


def notice(streamers) -> tuple[str, str]:
    """(level, text) for Live's stage while something needs a glance; ("", "") when all is well.

    Places sharing one encoder are lowered together, so they share one phrase: "Bitrate lowered to 3,600 kbps:
    Twitch, Kick and YouTube (horizontal)", not the same words once per place (live 2026-11-22: four of them made a
    banner as big as the preview)."""
    from sprigcast.output.stream import REFUSED_SAY

    parts, level = [], ""
    lowered: dict[int, list[str]] = {}
    waiting, offline, back = [], [], []
    for _, st in streamers:
        status = str(getattr(st, "status", ""))
        if not getattr(st, "connected", True) and getattr(st, "reconnects", 0) and \
                status.startswith(("reconnecting", "disconnected")):
            if getattr(st, "refused", 0) >= REFUSED_SAY:  # say it plainly, not "try 80"
                parts.append(f"{st.name} may have ended this stream. Start a new one there, or check its "
                             "stream key")
            else:
                waiting.append(st)
                if OFFLINE_WORDS in status:
                    offline.append(st)
            level = "bad"
            continue
        if getattr(st, "connected", False) and time.monotonic() - getattr(st, "back_live_at", -1e9) < BACK_LIVE_S:
            back.append(str(st.name))
        pilot = getattr(st, "autopilot", None)
        if pilot is not None and pilot.lowered:
            lowered.setdefault(int(pilot.current_kbps), []).append(str(st.name))
            level = level or "warning"
    every = len(waiting) == len(streamers) and len(streamers) > 1
    if waiting and (every or len(offline) == len(waiting)) and (offline or every):
        # Not one place's trouble: every place dropped together, or this PC can't look the servers up (the
        # user's stream, 2026-11-27: five "Reconnecting to…" in a row said nothing about the internet).
        parts.insert(0, "Your internet seems down. Waiting for it to come back, retrying…" if every or
                     len(waiting) > 1 else f"Waiting for internet, retrying {waiting[0].name}…")
    else:
        parts[:0] = [f"Reconnecting to {st.name}… try {st.reconnects}" for st in waiting]
    if back and not level:
        return "good", f"Back live: {_names(back)}"
    for kbps, names in lowered.items():
        parts.append(f"Bitrate lowered to {kbps:,} kbps: {_names(names)}")
    return level, "  ·  ".join(parts)


def _names(names: list[str]) -> str:
    """"Twitch, Kick and YouTube (horizontal)": a canvas they all share is said once, at the end."""
    import re

    canvases = {m.group(1) if (m := re.search(r" \(([^()]+)\)$", n)) else None for n in names}
    tail = ""
    if len(names) > 1 and len(canvases) == 1 and None not in canvases:
        tail = f" ({canvases.pop()})"
        names = [re.sub(r" \([^()]+\)$", "", n) for n in names]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return joined + tail


def duration(seconds: int) -> str:
    h, m = seconds // 3600, seconds // 60 % 60
    return f"{h} h {m} min" if h else f"{m} min" if m else f"{seconds} s"
