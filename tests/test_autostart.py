import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from sprigcast.app import autostart


@pytest.fixture
def qapp():
    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("AutostartTest")
    return app


def test_command_runs_this_python_minimised(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"  # a Python with no SprigCast launcher beside it
    monkeypatch.setattr(sys, "executable", str(python))
    cmd = autostart.command()
    assert cmd == f'"{python}" -m sprigcast.app.main --minimized'


def test_a_development_venv_runs_python_not_its_console_script(tmp_path, monkeypatch):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (tmp_path / "pyvenv.cfg").write_text("home = x")
    (scripts / "SprigCast.exe").write_bytes(b"")  # pip's console script (sprigcast.exe on a real disk)
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "python.exe"))
    assert autostart.command() == f'"{scripts / "pythonw.exe"}" -m sprigcast.app.main --minimized'


def test_installed_copy_starts_through_the_launcher(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    (tmp_path / "SprigCast.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))
    assert autostart.command() == f'"{tmp_path / "SprigCast.exe"}" --minimized'


def test_start_minimised_from_flag_or_setting(qapp):
    from PySide6.QtCore import QSettings

    QSettings().setValue(autostart.START_MINIMIZED, False)
    assert autostart.start_minimized(["sprigcast", "--minimized"])
    assert not autostart.start_minimized(["sprigcast"])
    QSettings().setValue(autostart.START_MINIMIZED, True)
    assert autostart.start_minimized(["sprigcast"])
    QSettings().remove(autostart.START_MINIMIZED)


def test_off_windows_it_is_a_quiet_no_op():
    if autostart.supported():
        return
    assert not autostart.enabled()
    autostart.set_enabled(True)  # nothing to do, no error
    autostart.refresh()
