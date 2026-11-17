"""Source record: alongside the recording, a second file per canvas with only what you picked.

For example game + webcam without alerts: the engine draws a copy of each recorded canvas with the
left-out sources hidden ("clean:<canvas id>", see engine.set_clean), and the mixer's "clean" track is
the recording's sound without theirs (mixer.clean_out). Each canvas gets its own encoder, because
the picture differs from the main recording's.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path

from sprigcast.output.recorder import Recorder, recording_path, resolve_codec, shrink_to

log = logging.getLogger(__name__)

TITLE = "Source record"


class SourceRecording:
    def __init__(self, engine, recorders: list[tuple[str, Recorder]]):
        self.engine = engine
        self.recorders = recorders  # (canvas id, recorder)
        self.started_at = time.monotonic()

    @classmethod
    def start(cls, engine, canvases: list, folder: Path, hidden, bitrate_kbps: int, container: str = "mkv",
              codec: str | None = None, encoder=None, short_side: int | None = None,
              when: datetime | None = None) -> SourceRecording:
        codec = codec or resolve_codec(encoder)
        when = when or datetime.now()
        start_time = time.perf_counter()
        hidden = frozenset(hidden)
        engine.mixer.clean_out = hidden
        recorders = []
        for canvas in canvases:
            path = recording_path(folder, f"{canvas.id} source", when, container)
            size = shrink_to(canvas.width, canvas.height, short_side or getattr(canvas, "output_short_side", None))
            rec = Recorder(path, canvas.width, canvas.height, canvas.fps, bitrate_kbps, codec, start_time,
                           container, size, encoder=encoder, tracks=("clean",))
            rec.track_titles["clean"] = TITLE
            rec.start()
            recorders.append((canvas.id, rec))
            engine.set_clean(canvas.id, hidden)
            engine.add_frame_tap(f"clean:{canvas.id}", rec.push_frame, rec.gpu_size)
            engine.mixer.add_tap(rec.push_audio, ("clean",))
        return cls(engine, recorders)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def dropped(self) -> int:
        return sum(rec.dropped for _, rec in self.recorders)

    @property
    def errors(self) -> list[str]:
        return [f"{TITLE.lower()} {cid}: {rec.error}" for cid, rec in self.recorders if rec.error]

    @property
    def paths(self) -> list[Path]:
        return [rec.path for _, rec in self.recorders]

    def stop(self) -> None:
        for canvas_id, rec in self.recorders:
            self.engine.remove_frame_tap(f"clean:{canvas_id}", rec.push_frame)
            self.engine.mixer.remove_tap(rec.push_audio)
            self.engine.set_clean(canvas_id, None)
        for _, rec in self.recorders:
            rec.stop()
        self.engine.mixer.clean_out = frozenset()


class WithSourceRecord:
    """The recording and its source record as one session (what the app holds while recording)."""

    def __init__(self, main, extra: SourceRecording):
        self.main, self.extra = main, extra

    @property
    def recorders(self):
        return list(self.main.recorders)  # the stats panel follows the main recording

    @property
    def elapsed(self) -> float:
        return self.main.elapsed

    @property
    def dropped(self) -> int:
        return self.main.dropped + self.extra.dropped

    @property
    def errors(self) -> list[str]:
        return self.main.errors + self.extra.errors

    @property
    def paths(self) -> list[Path]:
        return self.main.paths + self.extra.paths

    @property
    def paused(self) -> bool:
        return self.main.paused

    def pause(self) -> bool:
        for _, rec in self.extra.recorders:
            rec.pause()
        return self.main.pause()

    def resume(self) -> bool:
        for _, rec in self.extra.recorders:
            rec.resume()
        return self.main.resume()

    def mark_chapter(self, name: str) -> bool:
        return self.main.mark_chapter(name)  # the copy without some sources is for cutting, not chapters

    @property
    def chapters(self) -> dict:
        return self.main.chapters

    def stop(self) -> None:
        try:
            self.extra.stop()
        finally:
            self.main.stop()
