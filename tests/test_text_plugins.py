import os
import time
from datetime import datetime

import pytest

from sprigcast.core.source import create_source, known_kinds, source_class
from sprigcast.plugins import EXAMPLE, ensure_example, load_plugins
from sprigcast.sources.text import DEFAULTS, fill_tokens


def test_tokens_are_filled():
    now = datetime(2026, 11, 11, 15, 4, 5)
    assert fill_tokens("{time} {time12} {date}", now) == "15:04:05 3:04 PM 2026-11-11"


def test_text_source_renders_and_resizes_with_text():
    src = create_source("text", "t1", {"text": "Hi", "size": 40})
    w1, h1 = src.size
    frame = src.video_frame()
    assert frame is not None and frame.shape == (h1, w1, 4)
    assert frame[..., 3].max() == 255  # some opaque text pixels
    src.update({"text": "Hello there, a much longer line", "size": 40})
    src.video_frame()
    assert src.size[0] > w1


def test_text_source_reads_file(tmp_path):
    f = tmp_path / "now_playing.txt"
    f.write_text("Song A", encoding="utf-8")
    src = create_source("text", "t2", {**DEFAULTS, "file": str(f)})
    src.video_frame()
    width_a = src.size[0]
    f.write_text("A much longer song title", encoding="utf-8")
    src._next_check = 0.0
    src.video_frame()
    assert src.size[0] > width_a


def test_example_plugin_loads(tmp_path):
    ensure_example(tmp_path)
    assert (tmp_path / "countdown_example.py").read_text(encoding="utf-8") == EXAMPLE
    report = load_plugins(tmp_path)
    assert report.loaded == ["countdown_example.py"] and not report.failed
    assert "countdown" in known_kinds()
    cls = source_class("countdown")
    assert cls.label == "Countdown" and "minutes" in cls.defaults
    src = create_source("countdown", "c1", {**cls.defaults, "minutes": 2, "label": "Back in "})
    assert src._current_text() in ("Back in 2:00", "Back in 1:59")
    assert src.video_frame() is not None


def test_example_not_rewritten_when_folder_has_plugins(tmp_path):
    (tmp_path / "mine.py").write_text("X = 1\n", encoding="utf-8")
    ensure_example(tmp_path)
    assert not (tmp_path / "countdown_example.py").exists()


def test_broken_plugin_is_reported_not_raised(tmp_path):
    (tmp_path / "broken.py").write_text("raise RuntimeError('oops')\n", encoding="utf-8")
    (tmp_path / "_private.py").write_text("raise RuntimeError('never imported')\n", encoding="utf-8")
    (tmp_path / "fine.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = load_plugins(tmp_path)
    assert report.loaded == ["fine.py"]
    assert list(report.failed) == ["broken.py"] and "oops" in report.failed["broken.py"]


def test_settings_dialog_round_trips_values():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.settings_dialog import SettingsDialog

    defaults = {"text": "a", "size": 10, "scale": 1.5, "shadow": False, "color": "#ffffffff", "file": ""}
    dialog = SettingsDialog("Test", defaults, {"text": "line 1\nline 2", "size": 33, "shadow": True})
    values = dialog.values()
    assert values == {"text": "line 1\nline 2", "size": 33, "scale": 1.5, "shadow": True, "color": "#ffffffff", "file": ""}
    assert all(type(values[k]) is type(defaults[k]) for k in defaults)


def test_text_refreshes_clock_once_a_second():
    src = create_source("text", "t3", {"text": "{time}"})
    assert src.video_frame() is not None
    assert src.video_frame() is None  # nothing changed within the same second
    time.sleep(1.05)
    assert src.video_frame() is not None
