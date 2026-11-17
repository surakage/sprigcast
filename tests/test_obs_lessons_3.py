"""From OBS, round three: garden backups, safe mode, split recordings, MP4 that survives a crash."""

import json
import os
import time
from datetime import date

import av
import pytest

from sprigcast.core import backups
from sprigcast.output.naming import finish_mp4
from sprigcast.output.shared import EncoderPool, SharedRecording, part_path

from test_shared_encoder import CANVASES, FakeEngine, _feed, _frames  # noqa: E402


# Backups -------------------------------------------------------------------------------


def _collection():
    from sprigcast.core.scene import Canvas, SceneCollection

    return SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])


def test_each_save_keeps_the_last_good_one_and_a_copy_a_day(tmp_path):
    path = tmp_path / "scenes.json"
    c = _collection()
    c.save(path)
    assert not backups.last_good(path).exists()  # nothing to keep the first time
    first = path.read_text()
    c.recording.bitrate_kbps = 9000
    c.save(path)
    assert backups.last_good(path).read_text() == first
    assert backups.copies(path) == [backups.dated(path)]
    for day in range(1, 12):  # a copy a day, the newest seven kept
        backups.keep(path, today=date(2026, 10, day))
    kept = backups.copies(path)
    assert len(kept) == backups.DAYS and kept[0] == backups.dated(path)
    assert backups.dated(path, date(2026, 10, 4)) not in kept and backups.dated(path, date(2026, 10, 11)) in kept


def test_an_unreadable_garden_opens_its_newest_readable_backup(tmp_path):
    from sprigcast.app import main

    path = tmp_path / "scenes.json"
    c = _collection()
    c.recording.bitrate_kbps = 7000
    c.save(path)
    c.recording.bitrate_kbps = 8000
    c.save(path)  # the .bak holds 7000
    backups.last_good(path).write_text("{ not json")  # ...and that copy is broken too: the dated one opens
    path.write_text("{ broken")
    main.RESTORED.clear()
    loaded = main.load_collection(path)
    assert loaded.recording.bitrate_kbps == 7000  # the day's copy (taken before the second save)
    assert main.RESTORED == [backups.dated(path)]
    assert path.with_suffix(".broken.json").read_text() == "{ broken"
    assert json.loads(path.read_text())["recording"]["bitrate_kbps"] == 7000  # put back in its place
    assert "copy saved" in backups.when(main.RESTORED[0])


def test_with_no_backup_it_still_starts_fresh(tmp_path):
    from sprigcast.app import main

    path = tmp_path / "scenes.json"
    path.write_text("nope")
    main.RESTORED.clear()
    loaded = main.load_collection(path)
    assert loaded.canvases and main.RESTORED == []


# Safe mode ----------------------------------------------------------------------------


def test_safe_mode_is_asked_after_a_crash_and_never_when_starting_minimized():
    from sprigcast.app import safe_mode

    asked = []

    def ask():
        asked.append(1)
        return True

    assert safe_mode.choose(["sprigcast"], None, False, ask) is False and not asked  # closed cleanly
    assert safe_mode.choose(["sprigcast"], object(), True, ask) is False and not asked  # with Windows: no pop-up
    assert safe_mode.choose(["sprigcast"], object(), False, ask) is True and asked == [1]
    assert safe_mode.on()
    assert safe_mode.choose(["sprigcast"], object(), False, lambda: False) is False
    assert safe_mode.choose(["sprigcast", "--safe-mode"], None, False, ask) is True and asked == [1]
    safe_mode.choose([], None, False, ask)  # leave it off for the other tests
    assert not safe_mode.on()


# Split recordings and crash-safe MP4 ----------------------------------------------------


def test_a_long_recording_splits_at_keyframes_with_nothing_lost(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    start = time.perf_counter()
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 1000, "mkv", codec="libx264", split_seconds=1.5)
    _feed(engine, start, 240, pool=pool)  # 8 s of video at 30 fps, keyframes every 2 s
    rec.stop()
    first = rec.paths[0]
    assert [p.name for p in rec.paths[1:]] == [part_path(first, n).name for n in range(2, len(rec.paths) + 1)]
    assert len(rec.paths) >= 3
    counts = [_frames(p) for p in rec.paths]
    assert all(n >= 30 for n in counts)  # each part a real stretch of video
    assert sum(counts) >= 225  # nothing lost between parts
    for p in rec.paths:
        with av.open(str(p)) as f:
            first_packet = next(pk for pk in f.demux(video=0) if pk.size)
            assert first_packet.is_keyframe  # every part starts at a keyframe


def test_split_by_size(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    start = time.perf_counter()
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 1000, "mkv", codec="libx264", split_bytes=1)
    _feed(engine, start, 150, pool=pool)
    rec.stop()
    assert len(rec.paths) >= 2  # a new part at each keyframe once over the size


def test_an_mp4_recording_plays_without_being_closed_and_is_made_plain_after(tmp_path):
    engine = FakeEngine()
    pool = EncoderPool(engine)
    start = time.perf_counter()
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 1000, "mp4", codec="libx264")
    _feed(engine, start, 120, pool=pool)
    (path,) = rec.paths
    time.sleep(0.3)
    # Mid-recording (as after a crash or power cut): a copy of what's on disk already plays.
    snapshot = tmp_path / "crashed.mp4"
    snapshot.write_bytes(path.read_bytes())
    assert _frames(snapshot) > 20
    rec.stop()
    before = _frames(path)
    with open(path, "rb") as f:
        assert b"moof" in f.read()  # fragments
    assert finish_mp4(path)
    with open(path, "rb") as f:
        data = f.read()
    assert b"moof" not in data and data.index(b"moov") < data.index(b"mdat")  # plain, and starts quickly
    assert _frames(path) == before
    assert not any(p.name.endswith(".finishing.mp4") for p in tmp_path.iterdir())


def test_finishing_a_file_that_cant_be_read_keeps_it(tmp_path):
    bad = tmp_path / "x.mp4"
    bad.write_bytes(b"not a video")
    assert finish_mp4(bad) is False and bad.read_bytes() == b"not a video"
    assert sorted(os.listdir(tmp_path)) == ["x.mp4"]


@pytest.fixture(autouse=True)
def _no_leftover_safe_mode():
    yield
    from sprigcast.app import safe_mode

    safe_mode._on = False
