import os
import sys

import pytest

# Tests draw with Qt's "offscreen" platform. On Windows it finds no fonts of its own (Qt no longer ships any) and
# draws every letter as the same box, so "hi chat" and "HI CHAT" came out alike on the PC (2026-11-17). Point it at
# Windows' fonts, as the real app's Windows platform uses, before anything starts Qt.
# Browser pages are drawn in SprigCast itself in tests (the helper process has tests of its own, which start
# one on purpose): a window test with a browser source doesn't start Chromium in another process.
os.environ.setdefault("SPRIGCAST_BROWSER_IN_PROCESS", "1")
# The native engine is the app's default on Windows. Tests use the Python engine unless they ask for the native one
# (or check the default itself, tests/test_sprigcore.py).
os.environ.setdefault("SPRIGCAST_ENGINE", "python")
# Plugin sources run in SprigCast in tests, unless a test starts the plugin helper itself (test_plugin_helper.py).
os.environ.setdefault("SPRIGCAST_PLUGINS_IN_PROCESS", "1")
# The capture helper is another program too: tests capture in the engine, tests/test_capture_helper.py aside.
os.environ.setdefault("SPRIGCAST_CAPTURE_IN_ENGINE", "1")
# So is the program that copies the settings while SprigCast runs (tests/test_settings_keeper.py starts it).
os.environ.setdefault("SPRIGCAST_SETTINGS_COPY_HERE", "1")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))


def pytest_configure(config):
    _collect_on_main_thread()
    _settings_in_a_folder()


def _collect_on_main_thread():
    """As the app does (core/pacing.py): a collection on a test's stream or encoder thread used to free a closed
    page's widgets there, and the whole run crashed (Fatal Python error, 2026-11-18)."""
    from sprigcast.core import pacing

    pacing.collect_on_main_thread()


def _settings_in_a_folder():
    """Settings written by tests go to .ini files in a temporary folder, never the registry. On the PC, tests
    had left HKCU\\Software\\SprigCastTests and SprigCastAddMenuTest behind (2026-11-17), and a test that forgot
    to change the app's name would have written into the user's real SprigCast settings."""
    import atexit
    import shutil
    import tempfile

    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        return
    folder = tempfile.mkdtemp(prefix="sprigcast-test-settings-")
    atexit.register(shutil.rmtree, folder, True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, folder)


def _no_link_watch():
    """Tests make streams to servers that don't exist: the watch that asks a quiet stream's server whether it
    still answers (output/link_watch.py) stays off, so no test reaches out to the network."""
    from sprigcast.output import link_watch

    link_watch.WATCH.enabled = False


_no_link_watch()


@pytest.fixture(autouse=True)
def _keys_kept_in_memory(monkeypatch):
    """Stream keys and sign-ins saved by tests stay in memory, never in Windows Credential Manager (entries
    like "sprigcast/out-1@SprigCast" outlived a test run on the PC). A test can still patch keyring itself."""
    try:
        import keyring
    except ImportError:
        yield
        return
    saved: dict[tuple[str, str], str] = {}

    def delete_password(service, name):
        if saved.pop((service, name), None) is None:
            raise keyring.errors.PasswordDeleteError(name)

    monkeypatch.setattr(keyring, "get_password", lambda service, name: saved.get((service, name)))
    monkeypatch.setattr(keyring, "set_password", lambda service, name, value: saved.__setitem__((service, name), value))
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    yield


@pytest.fixture(autouse=True)
def _frames_faster_than_real_time(monkeypatch):
    """Tests hand an encoder a second of frames in a few milliseconds, often before it has opened.

    In the app that queue is kept short while the encoder opens (Recorder.opening_queue); here every
    frame has to arrive, so let the whole queue fill as it does once the encoder runs.
    """
    from sprigcast.output import recorder

    monkeypatch.setattr(recorder.Recorder, "opening_queue", recorder.VIDEO_QUEUE)


@pytest.fixture(autouse=True)
def _no_engine_lists_left_on():
    """A native bridge a test made and never closed would leave store plugins handing over lists of pictures
    (core/drawn.py ENGINE) in the next test."""
    from sprigcast.core import drawn

    drawn.ENGINE = False
    yield
    drawn.ENGINE = False


@pytest.fixture(autouse=True)
def _sources_closed_after_each_test():
    """Removed sources close on a helper thread (core/retire.py): let it finish and end after each test, so
    no test sees another's closing thread (some count threads)."""
    yield
    from sprigcast.core import retire

    retire.RETIRER.settle()


def pytest_sessionfinish(session, exitstatus):
    """Close what the tests left open and let Qt delete it while Python still runs. Left to the interpreter's own
    shutdown, Qt's wrappers were torn down after the modules they need, and the run ended in a crash (a segfault
    in Shiboken's unregisterWrapper) after every test had passed."""
    import gc

    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()


@pytest.fixture(autouse=True)
def _text_drawn_in_place(monkeypatch):
    """In the app a text that changes is drawn again on a worker (sources/text.py DRAW_ASIDE); tests that change
    a text check the new picture on the very next frame, so here it's drawn in place. test_text_plugins checks
    the worker path."""
    from sprigcast.core import painted
    from sprigcast.sources import image, text

    monkeypatch.setattr(text, "DRAW_ASIDE", False)
    monkeypatch.setattr(image, "DECODE_ASIDE", False)  # likewise a picture shown again (test_sources checks it)
    monkeypatch.setattr(painted, "DRAW_ASIDE", False)  # and clock cards (test_text_plugins checks the lane)
