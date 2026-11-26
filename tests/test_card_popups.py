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


def test_a_tall_pop_up_scrolls_instead_of_running_past_a_short_screen(app, monkeypatch):
    """Text from a file was 926 px tall and the starter kit 849 px: more than a 900 px all-in-one has free
    (Mikedog151, 2026-11-23)."""
    from sprigcast.app import card
    from sprigcast.app.text_leaf_dialog import TextLeafDialog

    room = card.screen_room(None)
    monkeypatch.setattr(card.FitScroll, "_room", lambda self: 600 - self.chrome)
    d = TextLeafDialog("Deaths", {"text": "Hi"})
    d.show()
    app.processEvents()
    assert d.height() <= 600 and d._scroll.verticalScrollBar().maximum() > 0
    d.close()
    monkeypatch.undo()
    small = card.CardDialog("Rename bloom")  # one that fits: as tall as it was, nothing to scroll
    small.show()
    app.processEvents()
    assert small.height() < room.height() and small._scroll.verticalScrollBar().maximum() == 0
    small.close()


def test_a_pop_up_past_the_screen_edge_is_moved_onto_it(app):
    from sprigcast.app.card import CardWindow, keep_on_screen, screen_room

    room = screen_room(None)
    w = CardWindow("Test", size=(room.width() + 300, 300))
    w.show()
    app.processEvents()
    assert w.width() <= room.width()
    w.move(room.right() - 50, room.bottom() - 50)
    keep_on_screen(w)
    assert room.contains(w.frameGeometry())
    w.close()


def test_a_scroll_bar_only_when_the_screen_is_too_small(app, monkeypatch):
    """The Android Screen pop-up showed a scroll bar with room to spare, Cancel and Save cut off under it (PC
    2026-11-26): once there, the bar took width, the text wrapped longer, and the bar stayed. A pop-up that fits
    shows everything with no bar; only one taller than its screen's free part scrolls."""
    import time

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel, QWidget

    from sprigcast.app import card

    def settle():
        for _ in range(10):
            app.processEvents()
            time.sleep(0.01)

    d = card.CardDialog("Fits")
    box = d.section("Phone")
    line = QLabel("Looking…")
    line.setWordWrap(True)
    box.addWidget(line)
    d.add_buttons("Save")
    d.show()
    settle()
    line.setText("No phone found. Plug it in with USB debugging on, then Look again. " * 3)  # grows once it's up
    settle()
    bar = d._scroll.verticalScrollBar()
    assert d._scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert bar.maximum() == 0 and d._scroll.widget().height() <= d._scroll.viewport().height()
    d.close()

    monkeypatch.setattr(card.FitScroll, "_room", lambda self: 500 - self.chrome)
    tall = card.CardDialog("Too tall")
    tall.body.addWidget(QWidget(minimumHeight=900))
    tall.show()
    settle()
    assert QWidget.height(tall) <= 500
    assert tall._scroll.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    assert tall._scroll.verticalScrollBar().maximum() > 0
    tall.close()
