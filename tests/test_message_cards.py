"""Messages and questions are rounded cards, and answer the way Qt's message boxes did."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _answer(monkeypatch, press):
    """Run the next card without showing it: press(card) clicks a button, or does nothing for Esc."""
    from sprigcast.app import message

    monkeypatch.setattr(message.MessageCard, "exec", lambda self: press(self) or 0)


def test_a_question_answers_like_a_message_box(app, monkeypatch):
    from sprigcast.app import message
    from sprigcast.app.message import Button

    by_text = lambda text: lambda card: next(b for b in card._buttons if b.text() == text).click()  # noqa: E731
    _answer(monkeypatch, by_text("Yes"))
    assert message.question(None, "SprigCast", "Remove it?") == Button.Yes
    _answer(monkeypatch, by_text("No"))
    assert message.question(None, "SprigCast", "Remove it?") == Button.No
    _answer(monkeypatch, lambda card: None)  # Esc
    assert message.question(None, "SprigCast", "Remove it?") == Button.No
    assert message.question(None, "Camera", "Set up?", Button.Ok | Button.Cancel) == Button.Cancel
    assert message.information(None, "SprigCast", "Select a scene first.") == Button.Ok


def test_the_card_shows_the_message_not_the_app_name(app):
    from sprigcast.app.card import CardDialog
    from sprigcast.app.message import MessageCard

    plain = MessageCard("SprigCast", "Select a scene first.")
    assert isinstance(plain, CardDialog) and plain.heading.isHidden() and plain.label.text() == "Select a scene first."
    named = MessageCard("Import settings", "Couldn't open the file.", kind="warn")
    assert not named.heading.isHidden() and "Import settings" in named.heading.text()
    cancel = named.add_button("Cancel", default=True)
    delete = named.add_button("Delete everything", danger=True)
    assert cancel.isDefault() and not delete.isDefault()  # Enter never deletes
    delete.click()
    assert named.clicked is delete


def test_no_square_message_boxes_left():
    from pathlib import Path

    import sprigcast

    root = Path(sprigcast.__file__).parent
    old = [p.name for p in root.rglob("*.py") for line in p.read_text(encoding="utf-8").splitlines()
           if "QMessageBox(" in line or "QMessageBox.information(" in line or "QMessageBox.warning(" in line
           or "QMessageBox.critical(" in line or "QMessageBox.question(" in line]
    assert not old


def test_the_small_windows_are_cards(app):
    from sprigcast.app.card import CardDialog
    from sprigcast.app.custom_docks import CustomDocksDialog
    from sprigcast.app.gardens_menu import ImportNotes, MissingFiles
    from sprigcast.app.names import NameDialog
    from sprigcast.app.states_dialog import StatesDialog
    from sprigcast.app.twitch_connect import TwitchConnectDialog
    from sprigcast.app.ducking_dialog import DuckingDialog

    for cls in (CustomDocksDialog, ImportNotes, MissingFiles, NameDialog, StatesDialog, TwitchConnectDialog,
                DuckingDialog):
        assert issubclass(cls, CardDialog), cls.__name__
    assert NameDialog("Cam", "Elgato Facecam MK.2").name() == "Cam"
