import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app.names import (short_name, short_status, sound_name, sound_name_is_ours, status_summary,
                                 window_name)


def test_short_names_drop_brands_and_model_codes():
    assert short_name("Elgato Facecam MK.2", "Camera") == "Facecam"
    assert short_name("Logitech HD Pro Webcam C920", "Camera") == "Pro Webcam"
    assert short_name("Mic In (Elgato Wave:XLR)", "Mic") == "Mic In"
    assert short_name("USB Video Device", "Camera") == "Camera"  # nothing left: the plain kind
    assert len(short_name("A very long device name that goes on and on", "Camera")) <= 18
    assert window_name("Stream - Google Chrome", r"C:\Program Files\chrome.exe") == "Chrome"
    assert window_name("Untitled - Notepad", "") == "Notepad"


def test_statuses_are_short():
    long = "capturing 1920x1080 @ 120, using 30, shrunk to 1280x720"
    assert short_status(long) == "" and short_status("ok") == ""
    assert status_summary(long) == "1080p120 → 720p30"
    assert status_summary("capturing 1920x1080 @ 60") == "1080p60"
    assert short_status("paused (not on screen)") == "asleep"
    assert short_status("camera disconnected, retrying") == "unplugged"
    assert short_status("error: something very long went wrong in the driver") == "problem"
    assert status_summary("capturing", (1920, 1080)) == "1920×1080"


def test_name_dialog_keeps_or_renames(monkeypatch):
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import names

    QApplication.instance() or QApplication([])
    assert names.ask_name(None, "Facecam", "Facecam") == "Facecam"  # nothing to ask
    dialog = names.NameDialog("Facecam", "Elgato Facecam MK.2")
    assert dialog.name() == "Facecam"
    dialog.edit.setText("Face")
    assert dialog.name() == "Face"
    dialog._keep()
    assert dialog.name() == "Elgato Facecam MK.2"


def test_only_a_sound_name_sprigcast_made_follows_the_device():
    assert sound_name("audio_input", "default") == "Mic"
    assert sound_name("audio_output", "default") == "Desktop audio"
    assert sound_name("audio_input", "Microphone (Voicemod)") == "Microphone"
    for made in ("Mic", "Mic (UG-08S)", short_name("UG-08S", "Mic")):
        assert sound_name_is_ours(made, "audio_input", "UG-08S")
    assert sound_name_is_ours("Desktop audio", "audio_output", "default")
    assert not sound_name_is_ours("[AIC] Microphone", "audio_input", "UG-08S")
    assert not sound_name_is_ours("Mic (Microphone (Voicemod))", "audio_input", "UG-08S")


def test_window_detail_drops_the_app_name():
    from sprigcast.app.names import window_app, window_detail

    assert window_detail("YouTube — Mozilla Firefox", "Firefox", "firefox.exe") == "YouTube"
    assert window_detail("#general | My Server - Discord", "Discord", "Discord.exe") == "#general | My Server"
    assert window_detail("main.py - sprigcast - Visual Studio Code", "Visual Studio Code", "Code.exe") == \
        "main.py - sprigcast"
    assert window_detail("Calculator", "Calculator", "ApplicationFrameHost.exe") == ""
    assert window_detail("Elden Ring", "ELDEN RING", "eldenring.exe") == ""
    assert window_app("Stream - Google Chrome", "chrome.exe") == "Chrome"
    assert window_app("Stream - Google Chrome", "chrome.exe", "Google Chrome") == "Google Chrome"


def test_sort_skips_a_leading_emoji_and_counts_numbers():
    from sprigcast.app.names import sort_key

    names = ["Window Capture", "🟣 Webcam", "🟣 Elgato", "Chat 10", "chat 2", "★ Alerts"]
    assert sorted(names, key=sort_key) == ["★ Alerts", "chat 2", "Chat 10", "🟣 Elgato", "🟣 Webcam", "Window Capture"]
    assert sorted(names, key=sort_key, reverse=True)[0] == "Window Capture"
