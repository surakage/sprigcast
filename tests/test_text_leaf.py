"""The text leaf: a text file followed live, its new looks, its own settings window, and the Control API names."""

import os
import time

import pytest

from sprigcast.core import file_watch
from sprigcast.core.source import create_source
from sprigcast.sources.text import DEFAULTS, LOOK_KEYS, pick_lines, set_case


def _settle(key, words, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if file_watch.text(key) == words:
            return True
        time.sleep(0.01)
    return False


def test_a_followed_file_is_seen_within_half_a_second(tmp_path):
    f = tmp_path / "deaths.txt"
    f.write_text("Deaths: 1", encoding="utf-8")
    file_watch.follow("w1", str(f))
    try:
        assert file_watch.wait_read("w1") and file_watch.text("w1") == "Deaths: 1"
        for n in range(2, 5):
            started = time.monotonic()
            temp = tmp_path / "deaths.tmp"  # written beside it and renamed over it, as many editors do
            temp.write_text(f"Deaths: {n}", encoding="utf-8")
            os.replace(temp, f)
            assert _settle("w1", f"Deaths: {n}")
            assert time.monotonic() - started < 0.5
    finally:
        file_watch.forget("w1")
    assert file_watch.version("w1") == -1


def test_a_missing_file_keeps_its_last_words_and_comes_back(tmp_path):
    f = tmp_path / "count.txt"
    f.write_text("7", encoding="utf-8")
    file_watch.follow("w2", str(f))
    try:
        file_watch.wait_read("w2")
        f.unlink()
        file_watch.check_now()
        assert file_watch.missing("w2") and file_watch.text("w2") == "7"
        f.write_text("8", encoding="utf-8")
        file_watch.check_now()
        assert not file_watch.missing("w2") and file_watch.text("w2") == "8"
    finally:
        file_watch.forget("w2")


def test_utf8_bom_utf16_and_windows_line_ends_read_cleanly(tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes("\ufeffMuerte 3\r\nzweite".encode("utf-8"))
    assert file_watch.read_words(str(f)) == "Muerte 3\nzweite"
    f.write_bytes("Ünïcode".encode("utf-16"))
    assert file_watch.read_words(str(f)) == "Ünïcode"


def test_the_text_leaf_shows_the_files_new_words_without_reading_on_the_render_thread(tmp_path, monkeypatch):
    f = tmp_path / "deaths.txt"
    f.write_text("Deaths: 1", encoding="utf-8")
    src = create_source("text", "live1", {**DEFAULTS, "file": str(f)})
    try:
        src.video_frame()
        assert src._current_text() == "Deaths: 1"
        file_watch.wait_read(src._watch_key)
        f.write_text("Deaths: 22", encoding="utf-8")
        file_watch.check_now()
        reads = []
        monkeypatch.setattr(file_watch, "read_words", lambda p: reads.append(p) or "")
        frame = src.video_frame()  # sees the new version at once: no waiting for the one-second look
        assert frame is not None and src._current_text() == "Deaths: 22" and not reads
    finally:
        src.stop()
    assert file_watch.following(src._watch_key) is None


def test_read_file_off_shows_the_text_and_lines_pick_first_or_last(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("one\n\ntwo\nthree\n", encoding="utf-8")
    src = create_source("text", "live2", {**DEFAULTS, "text": "Typed", "file": str(f), "read_file": False})
    assert src._current_text() == "Typed"
    src.update({**DEFAULTS, "file": str(f), "file_lines": "last"})
    assert src._current_text() == "three"
    assert pick_lines("\n a \nb", "first") == "a" and pick_lines("", "last") == ""
    assert set_case("Hi", "upper") == "HI" and set_case("Hi", "as_typed") == "Hi"
    src.stop()


def test_looks_change_the_picture_and_its_size_matches_its_layout():
    base = {**DEFAULTS, "text": "Deaths 12", "size": 40}
    plain = create_source("text", "p", base)
    plain_frame = plain.video_frame()
    assert plain_frame.shape[:2] == plain.size[::-1]
    for extra in ({"box": True}, {"shadow": True}, {"italic": True}, {"bold": True, "italic": True},
                  {"gradient": True, "outline_width": 3}, {"gradient": True, "gradient_direction": "across"}):
        src = create_source("text", "x", {**base, **extra})
        frame = src.video_frame()
        assert frame is not None and frame.shape[:2] == src.size[::-1], extra
        assert frame[..., 3].max() == 255
        assert src.size[0] >= plain.size[0], extra
    boxed = create_source("text", "b", {**base, "box": True, "box_padding": 10})
    assert boxed.size == (plain.size[0] + 20, plain.size[1] + 20)
    assert boxed.video_frame()[0, boxed.size[0] // 2, 3] > 0  # the card fills behind the words


def test_wrap_breaks_long_lines_and_detail_draws_sharper():
    long = {**DEFAULTS, "text": "a fairly long line of words for wrapping", "size": 30}
    wide = create_source("text", "w", long)
    narrow = create_source("text", "n", {**long, "wrap_width": 200})
    assert narrow.size[0] < wide.size[0] and narrow.size[1] > wide.size[1]
    narrow.video_frame()
    narrow.set_detail(2.0)
    big = narrow.video_frame()
    assert abs(big.shape[1] - narrow.size[0] * 2) <= 12  # the same wrap, twice the pixels


def test_obs_style_apps_can_turn_reading_a_file_on_and_off():
    from sprigcast.control.api import settings_for_obs, settings_from_obs

    assert settings_from_obs("text", {"read_from_file": True, "file": "C:/d.txt"}) == {"read_file": True,
                                                                                       "file": "C:/d.txt"}
    assert settings_for_obs("text", {"file": "C:/d.txt", "read_file": True})["read_from_file"] is True
    assert settings_for_obs("text", {"file": "", "read_file": True})["read_from_file"] is False


def test_font_names_split_into_families_and_styles():
    from sprigcast.sources.fonts import split_name

    assert split_name("Arial Bold Italic (TrueType)") == ("Arial", "bold italic")
    assert split_name("Segoe UI Semibold (TrueType)") == ("Segoe UI Semibold", "regular")
    assert split_name("Cambria & Cambria Math (TrueType)") == ("Cambria", "regular")
    assert split_name("Verdana Italic (TrueType)") == ("Verdana", "italic")


@pytest.fixture
def app():
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_the_window_gives_back_every_setting_and_looks_fill_it(app, tmp_path):
    from sprigcast.app.text_leaf_dialog import LOOKS, TextLeafDialog

    f = tmp_path / "deaths.txt"
    f.write_text("Deaths: 3", encoding="utf-8")
    d = TextLeafDialog("Deaths", {"text": "Hi", "file": str(f), "size": 50, "align": "center", "group": "kept"})
    d.show()
    values = d.values()
    assert set(values) == set(DEFAULTS)
    assert values["file"] == str(f) and values["read_file"] and values["align"] == "center" and values["size"] == 50
    assert values["font"] == DEFAULTS["font"]  # untouched: written back as it was
    d.set_values(dict(LOOKS)["Canopy card"])
    assert d.values()["box"] and not d.values()["shadow"]
    d.opacity.setValue(50)
    assert d.values()["color"].endswith("80")
    d.read_file.setChecked(False)
    assert not d.values()["read_file"] and d.values()["file"] == str(f)  # the path stays for next time
    assert [name for name, _ in LOOKS] == sorted(name for name, _ in LOOKS)
    d.reject()
    assert file_watch.following(d._source._watch_key) is None  # the preview stops following on close


def test_text_plugins_keep_their_short_form(app):
    from sprigcast.app.settings_dialog import SettingsDialog
    from sprigcast.plugins import EXAMPLE  # noqa: F401  (the example is a TextSource subclass)
    from sprigcast.sources.text import TextSource

    class Counter(TextSource):
        defaults = {**TextSource.defaults, "minutes": 5}

    d = SettingsDialog("Counter", Counter.defaults, Counter.defaults, cls=Counter)
    assert not set(LOOK_KEYS) & set(d.fields) and {"text", "minutes", "color"} <= set(d.fields)
