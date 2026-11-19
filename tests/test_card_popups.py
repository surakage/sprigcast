"""The small pick-one and type-a-name pop-ups are rounded cards, and a long name never widens them."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_a_long_window_title_fits_the_card(app):
    from PySide6.QtCore import Qt

    from sprigcast.app.card import CardDialog, ChoiceDialog

    long = "[firefox.exe] Video details - YouTube Studio — Mozilla Firefox " * 3
    d = ChoiceDialog("Window capture", "Window", ["[Discord.exe] Discord", long], 1)
    assert isinstance(d, CardDialog) and d.choice() == 1
    d.show()
    app.processEvents()
    assert d.box.width() < d.width() and d.width() <= 460  # the green focus ring stays inside the card
    assert d.box.itemText(1) != long and "…" in d.box.itemText(1)
    assert d.box.itemData(1, Qt.ItemDataRole.ToolTipRole) == long and d.box.itemText(0) == "[Discord.exe] Discord"


def test_type_a_name_waits_for_text(app):
    from sprigcast.app.card import TextDialog

    d = TextDialog("Rename source", "Name", "Facecam", primary="Rename")
    assert d.ok.isEnabled() and d.text() == "Facecam"
    d.edit.setText("   ")
    assert not d.ok.isEnabled()


def test_no_square_system_pop_ups_left():
    """QInputDialog shows a square system window: every pick and name goes through card.ask_* instead."""
    from pathlib import Path

    import sprigcast

    root = Path(sprigcast.__file__).parent
    assert not [p.name for p in root.rglob("*.py") if "QInputDialog" in p.read_text(encoding="utf-8")]


def test_camera_and_plugin_settings_are_cards(app):
    from sprigcast.app.camera_dialog import CameraDialog
    from sprigcast.app.card import CardDialog
    from sprigcast.app.settings_dialog import SettingsDialog
    from sprigcast.sources.camera import CameraDevice

    assert isinstance(CameraDialog([CameraDevice(0, "Cam", "a")]), CardDialog)
    assert isinstance(SettingsDialog("Text", {"text": "Hi", "size": 72}, {}), CardDialog)


def test_the_big_windows_are_rounded_cards(app):
    from PySide6.QtCore import Qt

    from sprigcast.app.card import CardWindow
    from sprigcast.app.doctor import DoctorDialog
    from sprigcast.app.filters_dialog import FiltersDialog
    from sprigcast.app.hotkeys_dialog import HotkeysWindow
    from sprigcast.app.plugin_store_page import PluginStoreWindow
    from sprigcast.app.wizard import OptimizeWizard

    for cls in (DoctorDialog, FiltersDialog, HotkeysWindow, PluginStoreWindow, OptimizeWizard):
        assert issubclass(cls, CardWindow), cls.__name__
    window = CardWindow("Test", size=(400, 300))
    assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window.card.size().isValid() and window.width() == 440  # the card plus room for its shadow
    window.show()
    app.processEvents()
    assert window.card.width() == 400 and window.card.geometry().contains(window._grip.geometry())  # still resizable
    window.close()
