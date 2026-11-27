"""What a tester build carries: its own version label, the license files, and Twitch's vertical kept off."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_build_label_names_a_test_build(tmp_path, monkeypatch):
    import sprigcast

    monkeypatch.setattr(sprigcast, "__file__", str(tmp_path / "__init__.py"))
    shown = sprigcast.display_version()
    assert sprigcast.build_label() == shown  # a release (or source) has no build.json
    (tmp_path / "build.json").write_text(json.dumps({"kind": "test", "run": "44932007527", "commit": "eafa358"}),
                                         encoding="utf-8-sig")
    assert sprigcast.build_label() == f"{shown} test build 44932007527 (eafa358)"
    (tmp_path / "build.json").write_text("not json", encoding="utf-8")
    assert sprigcast.build_label() == shown


def test_versions_show_as_betas_and_whole_numbered_releases():
    from sprigcast import display_version

    assert display_version("0.3.0") == "Beta 0.3"
    assert display_version("0.3.1") == "Beta 0.3.1"
    assert display_version("v0.2.0") == "Beta 0.2"
    assert display_version("1.0.0") == "Version 1"
    assert display_version("1.2.0") == "Beta 1.2"  # a beta toward Version 2
    assert display_version("2.0.0") == "Version 2"
    assert display_version("0.0.0") == "Beta 0.0"
    assert display_version("0.3.0-dev") == "0.3.0-dev"


def test_logs_and_reports_use_the_build_label(tmp_path, monkeypatch):
    import sprigcast
    from sprigcast.app import logbook

    monkeypatch.setattr(sprigcast, "build_label", lambda: "0.2.0 test build 1 (abc1234)")
    assert logbook._version() == "0.2.0 test build 1 (abc1234)"


def test_notices_list_every_bundled_package_and_both_licenses(tmp_path):
    sys.path.insert(0, str(ROOT / "packaging"))
    try:
        import notices
    finally:
        sys.path.remove(str(ROOT / "packaging"))
    home = tmp_path / "python"
    site = home / "Lib" / "site-packages"
    info = site / "tinylib-1.2.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.4\nName: tinylib\nVersion: 1.2\nLicense-Expression: MIT\n"
                                   "Project-URL: Source, https://example.com/tinylib\n", encoding="utf-8")
    (info / "licenses").mkdir()
    (info / "licenses" / "LICENSE").write_text("MIT License text for tinylib", encoding="utf-8")
    (info / "RECORD").write_text("tinylib-1.2.dist-info/METADATA,,\ntinylib-1.2.dist-info/licenses/LICENSE,,\n",
                                 encoding="utf-8")
    (home / "LICENSE.txt").write_text("PYTHON SOFTWARE FOUNDATION LICENSE", encoding="utf-8")

    target = notices.write(home, ROOT)
    text = target.read_text(encoding="utf-8")
    assert "- tinylib 1.2: MIT" in text and "MIT License text for tinylib" in text
    assert "https://example.com/tinylib" in text
    assert "FFmpeg" in text and "Qt 6" in text and "Chromium" in text
    assert "PYTHON SOFTWARE FOUNDATION LICENSE" in text  # Python's own, read before ours took its name
    assert "GNU GENERAL PUBLIC LICENSE" in (home / notices.LICENSE).read_text(encoding="utf-8")


def test_twitch_vertical_follows_the_place_and_can_be_switched_off(monkeypatch):
    from types import SimpleNamespace

    from sprigcast.output import enhanced

    place = SimpleNamespace(enhanced=True, enhanced_vertical=True)
    monkeypatch.delenv(enhanced.VERTICAL_ENV, raising=False)
    assert enhanced.sends_vertical(place)
    assert not enhanced.sends_vertical(SimpleNamespace(enhanced=True, enhanced_vertical=False))
    monkeypatch.setenv(enhanced.VERTICAL_ENV, "0")
    assert not enhanced.sends_vertical(place)
    assert not enhanced.sends_vertical(SimpleNamespace(enhanced=False, enhanced_vertical=True))


def test_stream_page_hides_the_vertical_row_and_keeps_a_saved_choice(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.core.scene import Canvas, Output, SceneCollection

    app = QApplication.instance() or QApplication([])  # noqa: F841
    monkeypatch.setenv("SPRIGCAST_TWITCH_VERTICAL", "0")
    out = Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/twitch",
                 enhanced=True, enhanced_vertical=True)
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080),
                                    Canvas(id="vertical", width=1080, height=1920)], outputs=[out])
    page = StreamPage(col)
    page.show()
    assert not page.extra_canvas.isVisible()
    page.bitrate.setValue(page.bitrate.value() + 500)  # any edit saves the form
    assert page.outputs[0].enhanced_vertical  # kept for when it works, just not used
    page.close()


def test_about_names_a_test_build_and_links_the_licenses(monkeypatch):
    from PySide6.QtWidgets import QApplication, QLabel

    import sprigcast
    from sprigcast.app import about

    app = QApplication.instance() or QApplication([])  # noqa: F841
    monkeypatch.setattr(sprigcast, "build_label", lambda: f"{sprigcast.display_version()} test build 7 (abc1234)")
    page = about.about_page()
    assert page.findChild(QLabel, "aboutBuild").text() == "Test build 7 (abc1234)"
    from PySide6.QtWidgets import QPushButton

    assert page.findChild(QPushButton, "aboutLicenses").toolTip() == "https://sprigcast.slothbloom.com/source"
    assert page.findChild(QPushButton, "aboutLink").toolTip() == "https://sprigcast.slothbloom.com"  # never GitHub


def test_x264_picked_in_settings_isnt_called_no_gpu_encoder(monkeypatch):
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder, "_picked", "h264_amf")
    assert recorder.codec_label("libx264") == "x264 (CPU, picked in Settings)"
    monkeypatch.setattr(recorder, "_picked", "libx264")
    assert recorder.codec_label("libx264") == "x264 (CPU, no GPU encoder found)"


def test_about_buttons_stay_round_under_the_app_style():
    """The app's QPushButton min-height once beat setFixedHeight and drew them as flat 20 px boxes."""
    from PySide6.QtWidgets import QApplication, QPushButton

    from sprigcast.app import about, theme

    app = QApplication.instance() or QApplication([])
    old = app.styleSheet()
    app.setStyleSheet(theme.STYLESHEET)
    try:
        page = about.about_page()
        page.show()
        app.processEvents()
        for name in ("aboutLink", "aboutLicenses", "aboutTip"):
            assert page.findChild(QPushButton, name).height() == about.PILL_H
        page.close()
    finally:
        app.setStyleSheet(old)


def test_numbered_test_builds_say_their_number(tmp_path, monkeypatch):
    import sprigcast

    monkeypatch.setattr(sprigcast, "__file__", str(tmp_path / "__init__.py"))
    (tmp_path / "build.json").write_text(json.dumps({"kind": "test", "run": "449", "commit": "eafa358", "test": 3,
                                                     "tag": "test-0.5-3"}), encoding="utf-8-sig")
    assert sprigcast.build_label() == f"{sprigcast.display_version()} test build 3 (eafa358)"
    assert sprigcast.test_build() == 3
    assert sprigcast.short_version("0.5.0", 3) == "Beta 0.5 test build 3"
    assert sprigcast.short_version("0.5.0") == "Beta 0.5"
