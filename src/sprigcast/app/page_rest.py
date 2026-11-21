"""Pages that grow, and pages that rest.

On a 65 minute stream (PC 2026-11-21) the page helper grew from 1.76 GB to 2.46 GB, one page process alone by
11 MB a minute, with 18 pages kept running off screen, and nothing said which page it was. Windows can't say:
every page shares two page processes (browser_host.RENDERER_LIMIT). So each page is asked itself, every few
minutes, how much it holds (PageSizes), and a page kept running that has been off screen REST_AFTER_S is put to
rest: Chromium's "frozen" state, in which its scripts and timers stop and it stays loaded, until it is shown again.
"""

from __future__ import annotations

import os

from sprigcast.sources.browser import REST_AFTER_S  # noqa: F401 (the pages import it from here)

MEASURE_EVERY_S = 300.0  # each running page is asked how much it holds
MEASURE_FIRST_S = 60.0  # the first time, so a short run's log has the line too (PC test 2026-11-21: 8 minutes, none)
LOG_EVERY = 3  # the first time and every third after it is written to the log (15 minutes), and whenever a page
# starts to grow
AUDIBLE_RETRY_S = 60.0  # a page still making sound isn't put to rest: looked at again after this
GREW_TIMES, GREW_BY = 1.5, 300  # elements: this many times its first count and this many more is "growing"
GREW_MEDIA = 5  # videos and sounds on the page: this many more than at first
SAME_HEAP = 0.03  # script memory figures this close are one page process's, asked a moment apart
ACTIVE, FROZEN = 0, 1  # QWebEnginePage.LifecycleState / WebEngineView.LifecycleState

# Elements on the page, the script memory of its page process (shared with the other pages in it), videos and
# sounds, frames. One text, so it passes through QML as it is.
MEASURE_JS = ("(function(){try{var m=performance.memory;return [document.getElementsByTagName('*').length,"
              "m?m.usedJSHeapSize:0,document.querySelectorAll('video,audio').length,"
              "document.querySelectorAll('iframe').length].join(',')}catch(e){return ''}})()")


def may_rest(page) -> bool:
    """Whether a hidden page may be put to rest: not when its sound goes to SprigCast's mixer (heard whatever is
    on screen), its window for clicking in it is open, its source says never ("never_rest"), or
    SPRIGCAST_KEEP_PAGES_AWAKE is set."""
    if os.environ.get("SPRIGCAST_KEEP_PAGES_AWAKE") or page.settings.get("never_rest"):
        return False
    return page.sound is None and page.interacting is None


class PageSizes:
    """What each page said it holds, first and last, and the log line made of it."""

    def __init__(self):
        self.first: dict[str, tuple[float, int, int, int, int]] = {}  # id -> (when, elements, heap, media, frames)
        self.last: dict[str, tuple[float, int, int, int, int]] = {}
        self.growing: set[str] = set()
        self.rounds = 0

    def feed(self, page_id: str, text: str, now: float) -> None:
        try:
            elements, heap, media, frames = (int(float(v)) for v in str(text).split(","))
        except ValueError:
            return  # the page couldn't say (no document yet, an error page)
        seen = (now, elements, heap, media, frames)
        was = self.first.get(page_id)
        if was is None or elements < was[1] / 2:  # loaded again (far fewer elements): counted from here
            self.first[page_id] = seen
            self.growing.discard(page_id)
        self.last[page_id] = seen

    def forget(self, page_id: str) -> None:
        self.first.pop(page_id, None)
        self.last.pop(page_id, None)
        self.growing.discard(page_id)

    def grew(self, page_id: str) -> bool:
        first, last = self.first.get(page_id), self.last.get(page_id)
        if first is None or last is None:
            return False
        return (last[1] >= first[1] * GREW_TIMES and last[1] - first[1] >= GREW_BY) or last[3] - first[3] >= GREW_MEDIA

    def line(self, resting: int = 0) -> str | None:
        """The log line after a round of asking, or None when this round has nothing new to say: the first round,
        every LOG_EVERY rounds, and at once when a page starts to grow. Pages by source id only, never an address."""
        self.rounds += 1
        growing = {page_id for page_id in self.last if self.grew(page_id)}
        new = growing - self.growing
        self.growing = growing
        if not self.last or (not new and self.rounds != 1 and self.rounds % LOG_EVERY):
            return None
        parts = [f"{len(self.last)} asked" + (f", {resting} resting" if resting else "")]
        ranked = sorted(growing, key=lambda p: self.last[p][1] - self.first[p][1], reverse=True)
        for page_id in ranked[:3]:
            first, last = self.first[page_id], self.last[page_id]
            minutes = max(1, round((last[0] - first[0]) / 60))
            said = f"{page_id} {last[1]:,} elements (+{last[1] - first[1]:,} in {minutes} min)"
            if last[3] - first[3] >= GREW_MEDIA:
                said += f", {last[3]} videos and sounds (+{last[3] - first[3]})"
            parts.append(said)
        if not ranked:
            parts.append("none growing")
        # Script memory is one figure for each page process: pages that say about the same share it. (Each page is
        # asked a moment apart, so the figures of one process differ by a little: told apart by the exact number,
        # the PC's line said "script memory 150 MB for 1 page" three times, 2026-11-21.)
        groups: list[tuple[int, list[str]]] = []  # (the biggest figure, its pages), biggest first
        for heap, page_id in sorted(((seen[2], p) for p, seen in self.last.items() if seen[2] > 0), reverse=True):
            if groups and heap >= groups[-1][0] * (1 - SAME_HEAP):
                groups[-1][1].append(page_id)
            else:
                groups.append((heap, [page_id]))
        for heap, pages in groups[:3]:
            began = min(self.first[p] for p in pages)  # the earliest look of any page in it
            minutes = max(1, round((max(self.last[p][0] for p in pages) - began[0]) / 60))
            more = heap - began[2]
            parts.append(f"script memory {heap / 1e6:.0f} MB for {len(pages)} page{'s' * (len(pages) != 1)}"
                         + (f" ({more / 1e6:+.0f} MB in {minutes} min)" if abs(more) >= 1e6 else ""))
        return "pages: " + "; ".join(parts)
