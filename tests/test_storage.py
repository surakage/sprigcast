"""Disk space warnings and tidy screenshots and clips."""

import os
import time
from datetime import datetime

from sprigcast.output import storage
from sprigcast.output.storage import GB, SpaceWatch


def test_minutes_left_and_warning_text():
    kbps = 24000  # two canvases at 12000: 180 MB a minute
    assert storage.space_warning(500 * GB, kbps) == ""
    low = storage.space_warning(2 * GB + 180_000_000 * 10, kbps)
    assert low.startswith("Low disk space: room for about 10 min")
    assert "full" in storage.space_warning(GB, kbps)
    assert storage.space_warning(None, kbps) == ""


def test_watch_warns_once_per_level():
    free = [100 * GB]
    watch = SpaceWatch("x", 24000, free=lambda _folder: free[0])
    assert watch.check() == "" and storage.rec_note(watch.left) == ""
    free[0] = 2 * GB + 180_000_000 * 20  # 20 min left
    assert "20 min" in watch.check()
    assert watch.check() == ""  # said once
    assert storage.rec_note(watch.left) == "low disk: 20 min left"
    free[0] = 2 * GB + 180_000_000 * 3
    assert "3 min" in watch.check()
    assert watch.check() == ""


def test_watch_starting_very_low_warns_once():
    watch = SpaceWatch("x", 24000, free=lambda _folder: 2 * GB + 180_000_000 * 2)
    assert watch.check() and watch.check() == ""


def test_day_folders_and_keep_newest(tmp_path):
    day = storage.day_folder(tmp_path, datetime(2026, 11, 15, 20, 0))
    assert day == tmp_path / "2026-11-15" and day.is_dir()
    old_day = storage.day_folder(tmp_path, datetime(2026, 11, 1))
    now = time.time()
    for i in range(5):
        f = old_day / f"old{i}.png"
        f.write_bytes(b"x")
        os.utime(f, (now - 1000 + i, now - 1000 + i))
    for i in range(3):
        f = day / f"new{i}.png"
        f.write_bytes(b"x")
        os.utime(f, (now + i, now + i))
    (day / "notes.txt").write_text("not a screenshot")
    assert storage.keep_newest(tmp_path, 0, storage.SCREENSHOT_TYPES) == []
    gone = storage.keep_newest(tmp_path, 3, storage.SCREENSHOT_TYPES)
    assert len(gone) == 5 and not old_day.exists()  # its day folder emptied, so it went too
    assert sorted(p.name for p in day.iterdir()) == ["new0.png", "new1.png", "new2.png", "notes.txt"]


def test_folder_size(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.mkv").write_bytes(b"1" * 1000)
    (tmp_path / "y.png").write_bytes(b"1" * 24)
    assert storage.folder_size(tmp_path) == 1024
    assert storage.size_text(int(11.5 * GB)) == "11.5 GB"


def test_clips_go_in_a_folder_per_day(tmp_path):
    from sprigcast.output.replay import replay_path

    path = replay_path(tmp_path, "vertical", datetime(2026, 11, 15, 19, 30, 5), "mkv")
    assert path.parent == tmp_path / "2026-11-15" and path.parent.is_dir() and path.suffix == ".mkv"


def test_space_box_reads_and_saves_the_limits(tmp_path):
    from PySide6.QtWidgets import QApplication, QLineEdit

    from sprigcast.app.output_page import SpaceBox
    from sprigcast.core.scene import Canvas, SceneCollection

    QApplication.instance() or QApplication([])
    (tmp_path / "r.mkv").write_bytes(b"1" * 2048)
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    col.recording.keep_screenshots = 50
    box = SpaceBox(col, QLineEdit(str(tmp_path)))
    assert box.keep_screenshots.value() == 50 and box.keep_clips.text() == "Keep all"
    deadline = time.time() + 5
    while "Counting" in box.usage.text() and time.time() < deadline:
        QApplication.processEvents()
        time.sleep(0.02)
    assert box.usage.text().startswith("2 KB in this folder")
    box.keep_clips.setValue(20)
    box.apply_to(col)
    assert col.recording.keep_clips == 20 and col.recording.keep_screenshots == 50
