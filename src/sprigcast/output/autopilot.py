"""Auto-pilot bitrate: lower a stream's bitrate when the upload can't keep up, climb back when it can.

The streamer's send buffer tells us how the upload is doing: seconds of video waiting to go out.
A growing buffer means the network is slower than the bitrate, so the bitrate steps down (bigger
steps the further behind it is) instead of frames being dropped. Once the buffer has stayed empty
for a while the bitrate climbs back in small steps, never above what you set.
"""

from __future__ import annotations

from dataclasses import dataclass

FLOOR_SHARE = 0.35  # never go below this share of the bitrate you set ...
FLOOR_KBPS = 800  # ... or below this, whichever is higher (unless you set less)
BEHIND_S, FAR_BEHIND_S = 0.5, 1.5  # buffered seconds that trigger a small / big step down
DOWN_STEP, BIG_DOWN_STEP = 0.8, 0.6  # multiply the bitrate by this
DOWN_EVERY_S = 2.0  # give each step down time to drain the buffer before judging again
CLEAR_S = 0.15  # buffer under this counts as caught up
UP_AFTER_S = 10.0  # caught up this long before climbing
UP_AFTER_DROP_S = 30.0  # ... and this long after the last step down
UP_STEP = 0.1  # climb by this share of the bitrate you set


@dataclass
class Autopilot:
    target_kbps: int  # what you set: the ceiling
    current_kbps: int = 0
    lowest_kbps: int = 0
    steps_down: int = 0
    _last_down: float = -1e9
    _clear_since: float | None = None

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
        if backlog_s > BEHIND_S:
            self._clear_since = None
            if now - self._last_down < DOWN_EVERY_S or self.current_kbps <= self.floor_kbps:
                return None
            step = BIG_DOWN_STEP if backlog_s > FAR_BEHIND_S else DOWN_STEP
            self._last_down = now
            self.steps_down += 1
            return self._set(max(self.floor_kbps, int(self.current_kbps * step)))
        if backlog_s > CLEAR_S or not self.lowered:
            self._clear_since = None
            return None
        if self._clear_since is None:
            self._clear_since = now
        if now - self._clear_since < UP_AFTER_S or now - self._last_down < UP_AFTER_DROP_S:
            return None
        self._clear_since = now  # wait again before the next step up
        return self._set(min(self.target_kbps, int(self.current_kbps + self.target_kbps * UP_STEP)))

    def _set(self, kbps: int) -> int | None:
        if kbps == self.current_kbps:
            return None
        self.current_kbps = kbps
        self.lowest_kbps = min(self.lowest_kbps, kbps)
        return kbps
