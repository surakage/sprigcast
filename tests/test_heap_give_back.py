"""At rest the heaps give back what they keep, and the memory map says how much a heap holds against what it hands
out (stream review 2026-11-23: commit stayed 1.3 GB above the start after a stream)."""
import sys

from sprigcast.core import memory_map, pacing

MB = 1024 * 1024


def test_heap_line_says_committed_against_in_use():
    line = memory_map.heap_line([(1, 330 * MB, 520 * MB), (2, 10 * MB, 12 * MB), (3, 1000, 4096)])
    assert line.startswith("heaps 532 MB committed, 340 MB in use")
    assert "520/330" in line and "12/10" in line
    assert memory_map.heap_line([]) == ""


def test_rest_compacts_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(pacing, "trim_working_set", lambda: calls.append("trim") or True)
    monkeypatch.setattr(memory_map, "compact_heaps", lambda: calls.append("compact") or 0)
    monkeypatch.setattr(sys, "platform", "win32")
    done = []

    class Now:  # run the thread's work right here
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(pacing.threading, "Thread", Now)
    pacing.trim_working_set_later(done.append)
    assert calls == ["trim"] and done == [True]
    pacing.trim_working_set_later(done.append, compact=True)
    assert calls == ["trim", "compact", "trim"]


def test_a_failing_compact_never_stops_the_trim(monkeypatch):
    calls = []

    def broken():
        raise OSError("no HeapSummary")

    monkeypatch.setattr(pacing, "trim_working_set", lambda: calls.append("trim") or False)
    monkeypatch.setattr(memory_map, "compact_heaps", broken)
    monkeypatch.setattr(sys, "platform", "win32")

    class Now:
        def __init__(self, target, **_):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(pacing.threading, "Thread", Now)
    pacing.trim_working_set_later(compact=True)
    assert calls == ["trim"]
