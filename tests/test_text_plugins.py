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
    assert "example_countdown" in known_kinds()  # "countdown" is the Countdown store plugin's
    cls = source_class("example_countdown")
    assert cls.label == "Example countdown" and "minutes" in cls.defaults  # the store's Countdown is "Countdown"
    src = create_source("example_countdown", "c1", {**cls.defaults, "minutes": 2, "label": "Back in "})
    assert src._current_text() in ("Back in 2:00", "Back in 1:59")
    assert src.video_frame() is not None


@pytest.mark.parametrize("which", [0, 1])
def test_an_untouched_old_example_is_brought_up_to_date(tmp_path, which):
    """The old example took kind "countdown", the Countdown store plugin's (PC, build eac4928: source.new made the
    example's plain text and countdown.start said "Add a Countdown first"). Updated, its countdowns get the card."""
    from sprigcast.plugins import OLD_EXAMPLES

    old = tmp_path / "countdown_example.py"
    old.write_text(OLD_EXAMPLES[which], encoding="utf-8")
    ensure_example(tmp_path)
    assert old.read_text(encoding="utf-8") == EXAMPLE and '@register("countdown")' not in EXAMPLE
    assert (tmp_path / "countdown.py").exists()  # the store Countdown, added for the countdowns made from it
    old.write_text(OLD_EXAMPLES[0] + "# mine now\n", encoding="utf-8")
    ensure_example(tmp_path)
    assert old.read_text(encoding="utf-8").endswith("# mine now\n")  # changed by you: left alone


def test_the_store_countdown_wins_its_kind_over_an_edited_old_example(tmp_path):
    from sprigcast import plugin_store
    from sprigcast.plugins import OLD_EXAMPLES
    from sprigcast.core.source import unregister_module

    pytest.importorskip("PySide6.QtWidgets")
    (tmp_path / "countdown_example.py").write_text(OLD_EXAMPLES[0] + "# mine now\n", encoding="utf-8")
    plugin = next(p for p in plugin_store.catalog() if p.id == "countdown")
    import shutil

    shutil.copyfile(plugin.file, tmp_path / "countdown.py")
    try:
        report = load_plugins(tmp_path)
        assert report.loaded == ["countdown_example.py", "countdown.py"]  # the store's last
        assert source_class("countdown").__module__ == "sprigcast_plugin_countdown"
    finally:
        plugin_store.remove(tmp_path, "countdown")
        unregister_module("sprigcast_plugin_countdown_example")


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


def test_a_changed_text_is_drawn_on_a_worker(monkeypatch):
    """Heavy run 2026-11-17: a text redraw took 10-13 ms of the render thread. Only the first picture is drawn
    there now; a change is drawn on a worker and handed over on a later frame."""
    import threading
    import time as _time

    from sprigcast.sources import text as text_mod

    monkeypatch.setattr(text_mod, "DRAW_ASIDE", True)
    src = text_mod.TextSource("t", {"text": "one", "size": 20})
    assert src.video_frame() is not None  # the first picture: in place
    drawn_on: list = []
    real = src._render

    def render():
        drawn_on.append(threading.current_thread().name)
        return real()

    monkeypatch.setattr(src, "_render", render)
    src.update({"text": "a much longer line", "size": 20})
    first = src.video_frame()  # starts the worker's drawing; the old picture stays meanwhile
    deadline = _time.monotonic() + 5
    frame = first
    while frame is None and _time.monotonic() < deadline:
        _time.sleep(0.005)
        frame = src.video_frame()
    assert frame is not None and frame.shape[1] > 60
    assert drawn_on and drawn_on[0].startswith("sprigcast-pictures")  # the one pictures lane


def test_a_text_keeps_its_picture_while_asleep_and_draws_again_only_if_it_changed(tmp_path):
    """Heavy run 2026-11-17: texts and clock cards were drawn again on every bloom switch (10-14 ms each). Asleep
    their texture stays (the engine doesn't forget it); woken, nothing is drawn unless the words changed."""
    from sprigcast import plugin_store
    from sprigcast.sources.text import TextSource

    assert TextSource.keeps_picture
    # Added and removed as the store does: importing sprigcast.store.* here left the kinds, actions and right-click
    # entries registered for the rest of the run (test_stream_timer failed when it ran after this file).
    for plugin_id in ("local_time", "stream_timer"):
        plugin = next(p for p in plugin_store.catalog() if p.id == plugin_id)
        assert plugin_store.install(tmp_path, plugin) is None
        try:
            assert source_class(plugin_id).keeps_picture
        finally:
            plugin_store.remove(tmp_path, plugin_id)
        assert plugin_id not in known_kinds()
    src = TextSource("t", {"text": "Hello", "size": 20})
    assert src.video_frame() is not None
    src.sleep()
    src.wake()
    assert src.video_frame() is None  # the same words: the kept texture is right
    src.sleep()
    src.update({"text": "Bye", "size": 20})
    src.wake()
    assert src.video_frame() is not None

