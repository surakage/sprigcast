import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QCoreApplication, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.app.palette import Command, CommandPalette, rank, score  # noqa: E402


@pytest.fixture
def app(tmp_path):
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTest")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    QSettings().clear()
    return app


def _commands(log):
    return [Command(t, g, lambda t=t: log.append(t)) for t, g in [
        ("Go live", "Action"), ("Start recording", "Action"), ("Stream outputs", "Settings"),
        ("Just Chatting  (horizontal)", "Scene"), ("Game  (horizontal)", "Scene"), ("Scenes", "Dock"),
    ]]


def test_letters_in_order_and_word_starts_win():
    assert score("Go live", "gl") > score("Big pile", "gl")
    assert score("Go live", "lg") is None
    titles = [c.title for c in rank(_commands([]), "set str")]
    assert titles[0] == "Stream outputs"  # matches "Settings Stream outputs"
    assert [c.title for c in rank(_commands([]), "gam")][0] == "Game  (horizontal)"


def test_empty_query_puts_recent_first():
    cmds = _commands([])
    assert [c.title for c in rank(cmds, "", recent=["Scene:Game  (horizontal)"])][:2] == [
        "Game  (horizontal)", "Go live"]


def test_typing_and_enter_runs_the_best_match_and_remembers_it(app):
    ran = []
    palette = CommandPalette(_commands(ran))
    palette.search.setText("just")
    assert palette.shown()[0].title.startswith("Just Chatting")
    palette._run(palette.list.currentItem())
    assert ran == ["Just Chatting  (horizontal)"]
    again = CommandPalette(_commands(ran))
    assert again.shown()[0].title.startswith("Just Chatting")  # recent first next time
    again.search.setText("zzzz")
    assert again.shown() == [None]  # just the "Nothing matches" row
