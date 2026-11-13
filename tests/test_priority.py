import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import priority  # noqa: E402


def test_priority_is_harmless_off_windows(monkeypatch):
    monkeypatch.setattr(priority.sys, "platform", "linux")
    assert priority.raise_gpu_priority() == ""
    assert priority.set_cpu_priority("high") is False
    assert "unchanged" in priority.apply("above_normal")


def test_saved_level_falls_back_to_default(tmp_path):
    from PySide6.QtCore import QSettings

    QSettings().setValue(priority.SETTING, "turbo")
    try:
        assert priority.chosen() == priority.DEFAULT
    finally:
        QSettings().remove(priority.SETTING)


def test_gpu_meter_without_nvidia_is_quiet():
    from sprigcast.app import doctor

    doctor._nvml = None
    assert doctor._nvml_load() is None or isinstance(doctor._nvml_load(), float)
