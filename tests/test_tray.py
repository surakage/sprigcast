"""Settings → General → System tray: the icon only while it's on, and the window never hidden with no way back."""

import os

import pytest

pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from sprigcast.app import tray  # noqa: E402


class Icon:
    def __init__(self):
        self.shown, self.said, self.menu, self.clicked = False, [], None, None
        self.activated = type("Signal", (), {"connect": lambda _s, fn: setattr(self, "clicked", fn)})()

    def setContextMenu(self, menu):  # noqa: N802
        self.menu = menu

    def setToolTip(self, text):  # noqa: N802
        self.tip = text

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False

    def isVisible(self):  # noqa: N802
        return self.shown

    def showMessage(self, *words):  # noqa: N802
        self.said.append(words)

    def deleteLater(self):  # noqa: N802
        pass


class Window(QWidget):
    asked, may = 0, True
    recording = None
    pressed: list = []

    def _on_live_clicked(self):
        self.pressed.append("live")

    def _on_record_clicked(self):
        self.pressed.append("record")

    def _may_close(self):
        self.asked += 1
        return self.may


@pytest.fixture()
def clean():
    QApplication.instance() or QApplication([])
    for key in (tray.SHOW, tray.MINIMIZE_TO, tray.CLOSE_TO, tray.TOLD):
        QSettings().remove(key)
    yield
    for key in (tray.SHOW, tray.MINIMIZE_TO, tray.CLOSE_TO, tray.TOLD):
        QSettings().remove(key)


def test_off_by_default_and_the_window_closes_and_minimizes_as_usual(clean):
    made = []
    window = Window()
    t = tray.Tray(window, make_icon=lambda w: made.append(Icon()) or made[-1])
    t.sync()
    assert not made and not t.active() and not t.closing() and not t.minimized()
    QSettings().setValue(tray.CLOSE_TO, True)  # left on from before, with the icon off: means nothing
    assert not tray.close_to() and not t.closing()


def test_the_icon_follows_the_setting_and_hides_the_window_only_when_asked(clean):
    made = []
    window = Window()
    window.show()
    t = tray.Tray(window, make_icon=lambda w: made.append(Icon()) or made[-1])
    QSettings().setValue(tray.SHOW, True)
    t.sync()
    t.sync()  # again: still one icon
    assert len(made) == 1 and t.active()
    assert [a.text() for a in made[0].menu.actions() if a.text()] == ["Open SprigCast", "Go live", "Start recording",
                                                                     "Quit"]
    assert made[0].tip == "SprigCast"
    t.show_state(True, True)
    assert made[0].tip == "SprigCast · Live and Recording"
    assert (t.live_action.text(), t.record_action.text()) == ("End stream", "Stop recording")
    t.show_state(False, True)
    assert made[0].tip == "SprigCast · Recording" and t.live_action.text() == "Go live"
    window.pressed.clear()
    t.record_action.trigger()
    t.live_action.trigger()
    assert window.pressed == ["record", "live"]
    t.show_state(False, False)
    assert not t.closing() and not t.minimized() and window.isVisible()  # shown in the tray only
    QSettings().setValue(tray.CLOSE_TO, True)
    assert t.closing() and not window.isVisible()
    assert len(made[0].said) == 1  # said once where it went
    t.open()
    assert window.isVisible()
    assert t.closing() and len(made[0].said) == 1  # not said again
    QSettings().setValue(tray.SHOW, False)
    t.sync()  # switched off while hidden there: the window comes back
    assert not t.active() and window.isVisible() and not made[0].shown


def test_no_tray_on_this_pc_never_hides_the_window(clean):
    window = Window()
    window.show()
    QSettings().setValue(tray.SHOW, True)
    QSettings().setValue(tray.CLOSE_TO, True)
    QSettings().setValue(tray.MINIMIZE_TO, True)
    t = tray.Tray(window, make_icon=lambda w: None)
    t.sync()
    assert not t.active() and not t.closing() and not t.minimized() and window.isVisible()

    def broken(_w):
        raise RuntimeError("no shell")

    t = tray.Tray(window, make_icon=broken)
    t.sync()
    assert not t.active() and not t.closing()


def test_quit_on_the_icon_asks_first_like_the_windows_close(clean):
    made = []
    window = Window()
    window.show()
    QSettings().setValue(tray.SHOW, True)
    QSettings().setValue(tray.CLOSE_TO, True)
    t = tray.Tray(window, make_icon=lambda w: made.append(Icon()) or made[-1])
    t.sync()
    assert t.closing()
    window.may = False  # live, and the answer was No
    t.quit()
    assert window.asked == 1 and window.isVisible()  # shown for the question, and still running
    window.may = True
    t.quit()
    assert window.asked == 2 and not window.isVisible()


def test_the_main_window_close_button_hides_to_the_tray_and_quit_closes(clean, tmp_path, monkeypatch):
    from PySide6.QtGui import QCloseEvent

    from test_add_menu import _window

    col, w = _window(tmp_path, monkeypatch)
    try:
        assert not w.tray.active()  # off: no icon made
        made = []
        w.tray._make_icon = lambda _w: made.append(Icon()) or made[-1]
        QSettings().setValue(tray.SHOW, True)
        QSettings().setValue(tray.CLOSE_TO, True)
        w.tray.sync()
        w.show()

        class Pressed(QCloseEvent):  # the × itself, not close() from code
            def spontaneous(self):
                return True

        event = Pressed()
        w.closeEvent(event)
        assert not event.isAccepted() and not w.isVisible() and made[0].shown  # hidden, still running
        assert w.timer.interval() == w.POLL_HIDDEN_MS  # and resting while out of sight
        w.tray.open()
        assert w.isVisible()
    finally:
        w.close()
    assert not made[0].shown  # closing for real takes the icon with it


def test_settings_general_has_the_system_tray_section(clean):
    from test_settings_window import _collection, _window

    w = _window(_collection(), tab="General")
    assert not w.tray_show.isChecked() and not w.tray_close.isEnabled() and not w.tray_minimize.isEnabled()
    w.tray_show.setChecked(True)
    assert w.tray_close.isEnabled()
    w.tray_close.setChecked(True)
    assert w.apply()
    assert tray.wanted() and tray.close_to() and not tray.minimize_to()
    w.close()


def test_settings_import_closes_settings_before_anything_is_imported(clean):
    """An import switches gardens: Settings, still open on the old one, must never save over it."""
    from test_settings_window import _collection, _window

    w = _window(_collection(), tab="General")
    asked = []
    w.on_import = asked.append
    w.import_button.click()
    assert asked == [w]
    w.close()


def test_the_gardens_import_choices_list_a_file_and_the_other_apps(clean, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMenu

    from test_add_menu import _window

    col, w = _window(tmp_path, monkeypatch)
    try:
        scenes = tmp_path / "obs-studio" / "basic" / "scenes"
        scenes.mkdir(parents=True)
        (scenes / "My_Stream.json").write_text("{}")
        monkeypatch.setenv("APPDATA", str(tmp_path))
        menu = QMenu()
        w.gardens.fill_imports(menu)
        assert menu.actions()[0].text() == "From a file…"
        subs = [a.menu() for a in menu.actions() if a.menu() is not None]
        assert len(subs) == 1 and [a.text() for a in subs[0].actions()] == ["My Stream"]
    finally:
        w.close()
