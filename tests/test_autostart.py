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


def test_command_runs_this_python_minimised():
    cmd = autostart.command()
    assert cmd.endswith("-m sprigcast.app.main --minimized")
    assert cmd.startswith('"') and str(sys.executable).rsplit("python", 1)[0] in cmd


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
