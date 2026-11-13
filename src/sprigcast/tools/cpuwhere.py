"""Which threads of this process use the CPU.

Windows and Linux count CPU time per thread; taking that twice and subtracting says who was busy in
between. Python threads are listed by name (the renderer, each encoder, the audio mixer); threads
started by libraries (the graphics driver, Qt, audio devices) have no name here and are added up.
"""

from __future__ import annotations

import os
import threading
import time


class CpuWatch:
    def __init__(self) -> None:
        self._at = 0.0
        self._before: dict[int, float] = {}
        self.restart()

    @staticmethod
    def _times() -> dict[int, float]:
        try:
            import psutil

            return {t.id: t.user_time + t.system_time for t in psutil.Process().threads()}
        except Exception:
            return {}

    def restart(self) -> None:
        self._at, self._before = time.monotonic(), self._times()

    def busiest(self) -> tuple[float, list[tuple[str, float]]]:
        """(share of the whole CPU in %, [(thread name, % of one core)] busiest first) since restart()."""
        now, after = time.monotonic(), self._times()
        seconds = max(1e-6, now - self._at)
        names = {t.native_id: t.name for t in threading.enumerate()}
        used: dict[str, float] = {}
        for tid, total in after.items():
            spent = total - self._before.get(tid, 0.0)
            if spent > 0:
                name = names.get(tid, "library threads (graphics driver, Qt, audio)")
                used[name] = used.get(name, 0.0) + spent
        whole = 100.0 * sum(used.values()) / seconds / (os.cpu_count() or 1)
        return whole, sorted(((name, 100.0 * spent / seconds) for name, spent in used.items()), key=lambda u: -u[1])

    def report(self, when: str, top: int = 8) -> None:
        """Print it. Never raises."""
        try:
            whole, threads = self.busiest()
        except Exception as exc:
            print(f"  (couldn't read CPU use: {exc})", flush=True)
            return
        if not threads:
            return
        print(f"  CPU {when}: {whole:.1f}% of the whole CPU ({os.cpu_count() or 1} threads). "
              "By thread, in % of one core:", flush=True)
        for name, share in threads[:top]:
            if share >= 0.05:
                print(f"      {share:5.1f}%  {name}", flush=True)
