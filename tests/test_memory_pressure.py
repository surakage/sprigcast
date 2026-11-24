"""Long waits say how much memory the PC had free and how fast SprigCast was faulting pages (Mikedog151's 12.8 s
Settings wait, 2026-11-23)."""
from types import SimpleNamespace

import psutil

from sprigcast.core import pacing


def test_memory_pressure_says_free_memory_and_a_fault_rate(monkeypatch):
    faults = iter([1000, 3000])
    monkeypatch.setattr(psutil, "virtual_memory", lambda: SimpleNamespace(available=1.1e9, total=4e9))

    class Proc:
        def memory_info(self):
            return SimpleNamespace(num_page_faults=next(faults))

    monkeypatch.setattr(psutil, "Process", Proc)
    clock = iter([10.0, 12.0])
    monkeypatch.setattr(pacing, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(pacing, "_faults_seen", None)
    assert pacing.memory_pressure() == "; PC memory free 1,100 of 4,000 MB"
    assert pacing.memory_pressure() == "; PC memory free 1,100 of 4,000 MB, page faults 1,000/s"


def test_memory_pressure_never_fails(monkeypatch):
    monkeypatch.setattr(psutil, "virtual_memory", lambda: 1 / 0)
    assert pacing.memory_pressure() == ""
