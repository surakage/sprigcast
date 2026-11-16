"""Clips kept ready pause by themselves when nothing uses them, and come back for a stream or recording."""

import types

from sprigcast.core.scene import RecordingSettings


class _Window:
    def __init__(self, minutes=15):
        from sprigcast.app.main_window import MainWindow

        self.check = MainWindow._check_clips_idle.__get__(self)
        self.collection = types.SimpleNamespace(recording=RecordingSettings(clips_idle_minutes=minutes))
        self.streaming = self.recording = None
        self.replay = object()
        self._clips_used_at, self._clips_idle_paused = 0.0, False
        self.messages = []
        self.statusBar = lambda: types.SimpleNamespace(showMessage=lambda text, ms: self.messages.append(text))

    def _toggle_replay(self, on):
        self.replay = object() if on else None
        if on:
            self._clips_idle_paused = False


def test_clips_pause_when_unused_and_come_back_for_a_recording(monkeypatch):
    from sprigcast.app import main_window

    clock = [100.0]
    monkeypatch.setattr(main_window.time, "monotonic", lambda: clock[0])
    w = _Window()
    w._clips_used_at = 100.0
    clock[0] += 14 * 60
    w.check()
    assert w.replay is not None  # not yet
    clock[0] += 2 * 60
    w.check()
    assert w.replay is None and w._clips_idle_paused and "Clips paused" in w.messages[-1]
    w.recording = object()  # a recording starts: clips come back
    w.check()
    assert w.replay is not None and not w._clips_idle_paused
    w.recording = None
    clock[0] += 14 * 60  # the recording counted as use
    w.check()
    assert w.replay is not None


def test_never_keeps_clips_ready(monkeypatch):
    from sprigcast.app import main_window

    monkeypatch.setattr(main_window.time, "monotonic", lambda: 10_000.0)
    w = _Window(minutes=0)
    w.check()
    assert w.replay is not None


def test_clips_are_off_at_start_by_default():
    settings = RecordingSettings()
    assert not settings.clips_on_start and settings.clips_idle_minutes == 15
