"""Auto-pilot bitrate: lower a stream's bitrate when the upload can't keep up, climb back when it can.

The streamer's send buffer tells us how the upload is doing: seconds of video waiting to go out.
A buffer that stays full means the network is slower than the bitrate, so the bitrate steps down
(bigger steps the longer it stays far behind) instead of frames being dropped. One reading alone
never counts: a bloom switch or a keyframe fills the buffer for a moment on every place at once
(PC live 2026-11-21: one 3 s reading right after a switch cut four places from 5400 to 2592 kbps,
and they sat there with an empty buffer for minutes). A spike that empties within seconds goes
back to the bitrate before it; a real slowdown climbs back in steps, waiting longer each time the
climb brings it back.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

FLOOR_SHARE = 0.35  # never go below this share of the bitrate you set ...
FLOOR_KBPS = 800  # ... or below this, whichever is higher (unless you set less)
BEHIND_S, FAR_BEHIND_S = 0.5, 1.5  # buffered seconds (two readings in a row) that trigger a small / big step down
FAR_READINGS = 3  # a big step only when this many readings in a row are far behind
DOWN_STEP, BIG_DOWN_STEP = 0.8, 0.6  # multiply the bitrate by this
DOWN_EVERY_S = 2.0  # give each step down time to drain the buffer before judging again
CLEAR_S = 0.25  # buffer under this counts as caught up
SPIKE_CLEARS_S = 6.0  # caught up this soon after the last step down: a spike, not the network
SPIKE_BACK_S = 10.0  # ... and back to the bitrate before it once caught up this long
UP_AFTER_S = 5.0  # caught up this long between steps up
UP_AFTER_DROP_S = 20.0  # ... and this long after the last step down (doubles when a climb brings a drop back)
UP_AFTER_DROP_MAX_S = 120.0
CALM_S = 300.0  # no step down this long: the wait after a drop is back to UP_AFTER_DROP_S
UP_STEP = 0.15  # climb by this share of the bitrate you set


@dataclass
class Autopilot:
    target_kbps: int  # what you set: the ceiling
    current_kbps: int = 0
    lowest_kbps: int = 0
    steps_down: int = 0
    _last_down: float = -1e9
    _last_up: float = -1e9
    _clear_since: float | None = None
    _before: int = 0  # the bitrate before this slowdown's first step down
    _spike: bool = False  # this slowdown emptied within SPIKE_CLEARS_S: back to _before in one go
    _up_wait: float = UP_AFTER_DROP_S
    _readings: deque = field(default_factory=lambda: deque(maxlen=FAR_READINGS))
    down_at: list = field(default_factory=list)  # (when, kbps) of the first steps down: the end-of-stream line says when

    def __post_init__(self) -> None:
        self.current_kbps = self.current_kbps or self.target_kbps
        self.lowest_kbps = self.current_kbps

    @property
    def floor_kbps(self) -> int:
        return min(self.target_kbps, max(FLOOR_KBPS, int(self.target_kbps * FLOOR_SHARE)))

    @property
    def lowered(self) -> bool:
        return self.current_kbps < self.target_kbps

    def update(self, now: float, backlog_s: float) -> int | None:
        """Call about once a second with the send buffer in seconds. Returns a new bitrate, or None."""
        self._readings.append(backlog_s)
        recent = list(self._readings)
        level = min(recent[-2:])  # two in a row: one reading is a spike
        if len(recent) >= 2 and level > BEHIND_S:
            self._clear_since = None
            if now - self._last_down < DOWN_EVERY_S or self.current_kbps <= self.floor_kbps:
                return None
            far = len(recent) >= FAR_READINGS and min(recent) > FAR_BEHIND_S
            if not self.lowered or now - self._last_down > 60.0:
                self._before, self._spike = self.current_kbps, False  # a new slowdown
            if now - self._last_up < 60.0:  # the climb brought it back: wait longer before the next one
                self._up_wait = min(UP_AFTER_DROP_MAX_S, self._up_wait * 2)
            elif now - self._last_down > CALM_S:
                self._up_wait = UP_AFTER_DROP_S
            self._last_down = now
            self.steps_down += 1
            kbps = self._set(max(self.floor_kbps, int(self.current_kbps * (BIG_DOWN_STEP if far else DOWN_STEP))))
            if len(self.down_at) < 20:
                self.down_at.append((now, self.current_kbps))
            return kbps
        if level > CLEAR_S or not self.lowered:
            self._clear_since = None
            return None
        if self._clear_since is None:
            self._clear_since = now
            self._spike = now - self._last_down <= SPIKE_CLEARS_S and self._up_wait == UP_AFTER_DROP_S
        clear = now - self._clear_since
        if self._spike and self._before > self.current_kbps:
            if clear < SPIKE_BACK_S:
                return None
            self._spike, self._clear_since, self._last_up = False, now, now
            return self._set(min(self.target_kbps, self._before))
        if clear < UP_AFTER_S or now - self._last_down < self._up_wait:
            return None
        self._clear_since, self._last_up = now, now  # wait again before the next step up
        return self._set(min(self.target_kbps, int(self.current_kbps + self.target_kbps * UP_STEP)))

    def _set(self, kbps: int) -> int | None:
        if kbps == self.current_kbps:
            return None
        self.current_kbps = kbps
        self.lowest_kbps = min(self.lowest_kbps, kbps)
        return kbps
