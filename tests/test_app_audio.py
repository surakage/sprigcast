"""App sound: which apps each type catches, and the starter kit's sound-by-type choices."""

import os

import pytest

from sprigcast.core.scene import Canvas, SceneCollection, SourceConfig
from sprigcast.sources import app_audio
from sprigcast.sources.app_audio import (
    TYPES,
    AppAudioSource,
    describe,
    follows_game,
    root_processes,
    wanted_apps,
)


def test_presets_and_picks():
    assert "spotify.exe" in wanted_apps({"preset": "music"})
    assert wanted_apps({"preset": "music", "apps": ["Foo.exe"]}) == {"foo.exe"}  # picked by hand: just that
    assert follows_game({"preset": "games"}) and not follows_game({"preset": "music"})
    assert not follows_game({"preset": "games", "apps": ["VALORANT.exe"]})
    assert follows_game({"preset": "music", "auto_game": True})
    assert describe({"preset": "voice"}) == TYPES["voice"].hint
    assert describe({"preset": "voice", "apps": ["Discord.exe", "a.exe", "b.exe", "c.exe"]}) == "Discord, a, b…"


def test_one_capture_per_app_tree():
    procs = [(1, 0, "explorer.exe"), (10, 1, "Discord.exe"), (11, 10, "Discord.exe"), (12, 10, "Discord.exe"),
             (20, 1, "Spotify.exe"), (21, 20, "Spotify.exe"), (30, 1, "chrome.exe")]
    assert root_processes(procs, wanted_apps({"preset": "voice"})) == {10: "Discord.exe"}
    assert root_processes(procs, wanted_apps({"preset": "music"})) == {20: "Spotify.exe"}
    source = AppAudioSource("s", {"preset": "browser"})
    assert source.targets(procs) == {30: "chrome.exe"}
    assert source.read_block(4).shape == (4, 2)  # nothing caught: silence


def test_one_program_by_its_process_id():
    """StreamBridge's raid clip Edge (its own profile) without the streamer's own Edge tabs (2026-11-20)."""
    procs = [(1, 0, "explorer.exe"), (40, 1, "msedge.exe"), (41, 40, "msedge.exe"),  # the streamer's Edge
             (50, 1, "msedge.exe"), (51, 50, "msedge.exe")]  # the one StreamBridge started
    source = AppAudioSource("s", {"preset": "custom", "apps": ["msedge.exe"], "pids": [50]})
    assert source.targets(procs) == {50: "msedge.exe"}  # its whole tree, by Windows' process loopback
    assert source.targets([p for p in procs if p[0] < 50]) == {}  # closed: nothing caught
    assert AppAudioSource("s", {"preset": "custom", "apps": ["msedge.exe"]}).targets(procs) == {
        40: "msedge.exe", 50: "msedge.exe"}  # by name: every Edge
    assert not follows_game({"preset": "games", "pids": [50]})
    assert describe({"preset": "custom", "pids": ["50", "x", -1]}) == "Programs an app picked"
    assert app_audio.wanted_pids({"pids": ["50", "x", -1, None]}) == {50}


def test_starter_makes_a_row_per_type_and_no_desktop():
    from sprigcast.app.starter import StarterChoice, build_starter

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)], sources=[], scenes=[])
    build_starter(col, StarterChoice(mic=None, blooms=["Ending"],
                                     app_sounds={"games": None, "music": None, "voice": ["Discord.exe"]}))
    rows = [c for c in col.sources if c.kind == "app_audio"]
    assert [c.name for c in rows] == ["Games", "Music", "Voice chat"]
    assert rows[2].settings["apps"] == ["Discord.exe"] and rows[2].settings["auto_game"] is False
    assert not [c for c in col.sources if c.kind == "audio_output"]  # nothing plays twice
    build_starter(col, StarterChoice(mic=None, blooms=["Ending"], replace_example=False,
                                     app_sounds={"music": None}))
    assert len([c for c in col.sources if c.kind == "app_audio"]) == 3  # Music was already there


def test_starter_dialog_sound_by_type():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.starter import StarterDialog

    _app = QApplication.instance() or QApplication([])
    dlg = StarterDialog([], [], [], True, True, running_apps=["Discord.exe"])
    dlg.type_rows["browser"][0].setChecked(False)
    dlg.type_rows["voice"][1].setCurrentIndex(1)
    picks = dlg.choice(lambda d: {}).app_sounds
    assert "browser" not in picks and picks["voice"] == ["Discord.exe"] and picks["music"] is None
    dlg.whole.setChecked(True)
    choice = dlg.choice(lambda d: {})
    assert choice.app_sounds is None and choice.desktop == "default"
    dlg.deleteLater()


def test_app_sound_is_a_sound_everywhere():
    config = SourceConfig(id="m", kind="app_audio", name="Music", settings={"preset": "music"})
    from sprigcast.core.scene import SOUND_KINDS

    assert config.kind in SOUND_KINDS


def test_starter_vertical_versions_switch_the_vertical_canvas_on():
    from sprigcast.app.starter import StarterChoice, build_starter

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080),
                                    Canvas(id="vertical", width=1080, height=1920, enabled=False)],
                          sources=[], scenes=[])
    made = build_starter(col, StarterChoice(mic=None, blooms=["Ending"], vertical=True))
    assert col.canvas("vertical").enabled and len(made) == 2
    col.canvas("vertical").enabled = False
    made = build_starter(col, StarterChoice(mic=None, blooms=["Gaming"], vertical=False))
    assert not col.canvas("vertical").enabled and len(made) == 1


def test_starter_dialog_always_offers_vertical_versions():
    pytest.importorskip("PySide6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.starter import StarterDialog

    _app = QApplication.instance() or QApplication([])
    dlg = StarterDialog([], [], [], False, False)  # not a fresh install, vertical canvas off
    assert not dlg.vertical.isHidden() and not dlg.vertical.isChecked()
    dlg.vertical.setChecked(True)
    assert dlg.choice(lambda d: {}).vertical
    dlg.deleteLater()


def test_process_list_is_shared_and_cheap(monkeypatch):
    import os
    import sys
    import time

    from sprigcast.sources import app_audio

    calls = []
    monkeypatch.setattr(app_audio, "_shared", None)
    monkeypatch.setattr(app_audio, "_list_processes", lambda: calls.append(1) or [(1, 0, "a.exe")])
    assert app_audio.running_processes() == app_audio.running_processes() == [(1, 0, "a.exe")]
    assert len(calls) == 1  # six App sound rows scanning together share one list
    monkeypatch.setattr(app_audio, "_shared", (time.monotonic() - 5, []))
    app_audio.running_processes()
    assert len(calls) == 2  # an old list is taken again
    if sys.platform == "win32":  # one snapshot, not psutil's walk per parent (8 s on a PC with 450 processes)
        started = time.perf_counter()
        procs = app_audio._toolhelp_processes()
        assert time.perf_counter() - started < 1.0
        assert any(pid == os.getpid() and parent == os.getppid() for pid, parent, _name in procs)


def test_the_once_a_second_scan_leaves_nothing_behind():
    """Soak 2026-11-15: a ctypes structure made inside the scan (and given to ctypes.POINTER) is kept by
    ctypes forever: ~33 objects a second, so Python's clean-up pauses grew to 289 ms overnight."""
    import ctypes
    import gc

    from sprigcast.app import resources
    from sprigcast.sources import app_audio

    for _ in range(3):
        ctypes.POINTER(app_audio._process_entry())
        app_audio._monitor_info()
        resources._counters_type()
    gc.collect()
    before = len(gc.get_objects())
    for _ in range(200):
        ctypes.POINTER(app_audio._process_entry())
        app_audio._monitor_info()
        resources._counters_type()
    gc.collect()
    assert len(gc.get_objects()) - before < 20


def test_an_app_windows_refuses_is_asked_again_later_and_later(monkeypatch):
    """Asking every 2 s scan for an app Windows wouldn't hand over cost handles each time."""
    tries = []

    def refuse(pid, name, owner):
        tries.append(pid)
        raise OSError("Windows refused the app's sound")

    clock = [1000.0]
    monkeypatch.setattr(app_audio, "_ProcessCapture", refuse)
    monkeypatch.setattr(app_audio.time, "monotonic", lambda: clock[0])
    src = AppAudioSource("game", {"apps": ["Game.exe"]})
    for _ in range(5):  # 10 s of scans
        src._sync({42: "Game.exe"})
        clock[0] += 2
    assert tries == [42]  # once, then a wait
    clock[0] += app_audio.RETRY_FIRST_S
    src._sync({42: "Game.exe"})
    assert tries == [42, 42]
    assert src._failed[42][1] == 2 * app_audio.RETRY_FIRST_S  # the next wait is longer
    src._sync({})  # the app closed
    assert 42 not in src._failed


def test_the_gpu_query_is_opened_fresh_every_few_minutes(monkeypatch):
    from sprigcast.app import gpu_usage

    made, closed = [], []

    class Fake:
        encode = 0.0

        def __init__(self):
            made.append(self)

        def read(self):
            return 5.0

        def close(self):
            closed.append(self)

    clock = [0.0]
    monkeypatch.setattr(gpu_usage, "GpuCounter", Fake)
    monkeypatch.setattr(gpu_usage, "_counter", None)
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    for _ in range(100):  # 300 s of reads, every 3 s
        assert gpu_usage.windows_gpu_load() == 5.0
        clock[0] += 3
    assert len(made) == 1 and not closed
    clock[0] += 10
    gpu_usage.windows_gpu_load()
    assert len(made) == 2 and closed == [made[0]]  # the old query closed: its handles go back
