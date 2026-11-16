"""File names for recordings and clips, and turning finished MKV recordings into MP4.

Names come from a pattern with tokens, set in Settings → Advanced:
{date} 2026-11-14, {time} 19-30-05, {canvas} horizontal, {year} {month} {day} {hour} {minute} {second}.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

RECORDING_PATTERN = "SprigCast {date} {time} {canvas}"
REPLAY_PREFIX = "SprigCast Replay"
TOKENS = ("{date}", "{time}", "{canvas}", "{year}", "{month}", "{day}", "{hour}", "{minute}", "{second}")
BAD = '<>:"/\\|?*'

_patterns = {"recording": RECORDING_PATTERN, "replay": REPLAY_PREFIX}


def set_patterns(recording: str | None = None, replay_prefix: str | None = None) -> None:
    """Called by the app at start and after Settings is applied; blank falls back to the default."""
    _patterns["recording"] = (recording or "").strip() or RECORDING_PATTERN
    _patterns["replay"] = (replay_prefix or "").strip() or REPLAY_PREFIX


def fill(pattern: str, canvas_id: str, when: datetime) -> str:
    """The pattern with its tokens filled in and characters Windows refuses in names taken out."""
    text = pattern
    for token, value in (("{date}", f"{when:%Y-%m-%d}"), ("{time}", f"{when:%H-%M-%S}"), ("{canvas}", canvas_id),
                         ("{year}", f"{when:%Y}"), ("{month}", f"{when:%m}"), ("{day}", f"{when:%d}"),
                         ("{hour}", f"{when:%H}"), ("{minute}", f"{when:%M}"), ("{second}", f"{when:%S}")):
        text = text.replace(token, value)
    text = "".join("-" if c in BAD else c for c in text).strip(" .")
    return text or f"SprigCast {when:%Y-%m-%d %H-%M-%S} {canvas_id}"


def unique(folder: Path, base: str, container: str) -> Path:
    path = folder / f"{base}.{container}"
    n = 2
    while path.exists():
        path = folder / f"{base} ({n}).{container}"
        n += 1
    return path


def recording_name(canvas_id: str, when: datetime) -> str:
    pattern = _patterns["recording"]
    if "{canvas}" not in pattern:  # two canvases recorded at once must not share a name
        pattern += " {canvas}"
    if not any(t in pattern for t in ("{time}", "{second}")):  # nor two recordings a minute apart
        pattern += " {time}"
    return fill(pattern, canvas_id, when)


def replay_name(canvas_id: str, when: datetime) -> str:
    return fill(f"{_patterns['replay']} {{date}} {{time}} {{canvas}}", canvas_id, when)


def remux_to_mp4(path: Path, delete_original: bool = False) -> Path | None:
    """Repot: copy a recording's video and sound into an MP4 next to it, without re-encoding (a remux).
    Works for MKV, FLV, MOV and TS. Returns the MP4, or None (the original is left as it is)."""
    import av

    import time

    target = unique(path.parent, path.stem, "mp4")
    began = time.monotonic()
    try:
        with av.open(str(path)) as src, av.open(str(target), "w", format="mp4") as dst:
            pairs = {}
            for stream in src.streams:
                if stream.type in ("video", "audio"):
                    pairs[stream.index] = dst.add_stream_from_template(stream)
            for packet in src.demux(*[s for s in src.streams if s.index in pairs]):
                if packet.size == 0:  # the demuxer's end-of-stream marker
                    continue
                packet.stream = pairs[packet.stream.index]
                dst.mux(packet)
    except Exception as exc:  # noqa: BLE001 (any failure leaves the MKV as it is)
        log.warning("couldn't turn %s into MP4: %s", path.name, exc)
        target.unlink(missing_ok=True)
        return None
    try:
        made = target.stat().st_size
        size = f"{made / 1e9:.2f} GB" if made >= 1e9 else f"{made / 1e6:.0f} MB"
    except OSError:
        size = "?"
    log.info("repot: saved %s as %s (%s) in %.1f s%s", path.name, target.name, size, time.monotonic() - began,
             "; original deleted" if delete_original else "")
    if delete_original:
        path.unlink(missing_ok=True)
    return target


def remux_later(paths: list[Path], delete_original: bool = False, done=None) -> threading.Thread:
    """Repot finished recordings on a background thread; `done(list_of_mp4s)` runs on that thread."""
    def work():
        made = [m for p in paths if p.suffix.lower() != ".mp4" and (m := remux_to_mp4(p, delete_original))]
        if done:
            done(made)
    thread = threading.Thread(target=work, name="remux", daemon=True)
    thread.start()
    return thread
