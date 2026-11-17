"""Stopping a recording doesn't hold the window (PC test 2026-11-17: 583 ms without an answer while
Recorder.stop waited for its thread on the UI thread)."""

from __future__ import annotations

import os
import threading
import time
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


class SlowEncoder:
    def __init__(self):
        self.stopped_on = None

    def stop(self):
        time.sleep(0.3)  # frames still queued being written, the file closing
        self.stopped_on = threading.current_thread()


class Session:
    def __init__(self):
        self.enc = SlowEncoder()
        self.asked = []

    def stop(self, later=None):
        self.asked.append(later)
        if later is None:
            self.enc.stop()
        else:
            later.append(self.enc)


class Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


def window(monkeypatch):
    from sprigcast import plugin_hooks
    from sprigcast.app.main_window import MainWindow

    monkeypatch.setattr(plugin_hooks, "output_changed", lambda *a, **k: None)
    done = []
    w = types.SimpleNamespace(recording=Session(), _say=lambda text: None, _side_status=lambda *a: None,
                              live_side=types.SimpleNamespace(set_recording=lambda on: None),
                              _record_finished=Signal(), done=done)
    w._recording_stopped = lambda session, low_disk=False: done.append((session, low_disk,
                                                                        session.enc.stopped_on is not None))
    w._stop_recording = types.MethodType(MainWindow._stop_recording, w)
    w._wait_for_stopped_recording = types.MethodType(MainWindow._wait_for_stopped_recording, w)
    return w


def test_stop_returns_at_once_and_finishes_on_another_thread(monkeypatch):
    w = window(monkeypatch)
    session = w.recording
    began = time.perf_counter()
    w._stop_recording()
    assert time.perf_counter() - began < 0.15  # the window is free while the file closes
    assert w.recording is None and w.done == []
    w._record_stopping.join(5)
    assert w.done == [(session, False, True)]  # told once, after the encoder had stopped
    assert session.enc.stopped_on is not threading.current_thread()


def test_closing_waits_for_the_file(monkeypatch):
    w = window(monkeypatch)
    session = w.recording
    w._stop_recording(wait=True)
    assert session.asked == [None] and w.done == [(session, False, True)]
    assert session.enc.stopped_on is threading.current_thread()


def test_the_next_recording_waits_for_the_last_ones_files(monkeypatch):
    w = window(monkeypatch)
    session = w.recording
    w._record_finished = Signal()  # its word never arrives (SprigCast is closing): the wait wraps it up itself
    w._record_finished_wired = True
    w._stop_recording(low_disk=True)
    assert w.done == []
    w._wait_for_stopped_recording()
    assert w.done == [(session, True, True)]
