"""A busy PC is named as such, with the other apps keeping it busy."""

from sprigcast.app import busy_pc


def test_warns_once_after_a_busy_minute():
    clock = [0.0]
    watch = busy_pc.BusyWatch(clock=lambda: clock[0])
    assert not watch.feed(85, None)
    clock[0] = 30
    assert not watch.feed(90, 40)
    clock[0] = 61
    assert watch.feed(82, None)
    clock[0] = 120
    assert not watch.feed(95, None)  # not again for a while
    assert not watch.feed(50, 50)  # calm again: the minute starts over
    clock[0] = 200
    assert not watch.feed(None, 90)


def test_message_names_the_apps():
    text = busy_pc.busy_message(["OBS", "Meld Studio", "Camera Hub"], 81, 40)
    assert "CPU 81%" in text and "OBS, Meld Studio and Camera Hub are using it" in text
    assert "graphics card 92%" in busy_pc.busy_message(["OBS"], 30, 92) and "OBS is using" in busy_pc.busy_message(["OBS"], 90, 0)
    assert "not from SprigCast" in busy_pc.busy_message([], 85, None)
    assert busy_pc.friendly("obs64.exe") == "OBS" and busy_pc.friendly("Game.exe") == "Game"


def test_heavy_apps_runs_here():
    assert isinstance(busy_pc.heavy_apps(wait=0.05), list)


def test_a_pinned_cpu_is_logged_with_who_had_it():
    """Live on the PC (2026-11-21) the CPU sat at 100% for three minutes with SprigCast at 15%, and the log
    couldn't say who had it: now 5 s at 90% or more logs the three heaviest apps (at most once a minute)."""
    clock = [0.0]
    spike = busy_pc.spike_watch(clock=lambda: clock[0])
    assert not spike.feed(95, None)
    clock[0] = 3
    assert not spike.feed(99, None)
    clock[0] = 5
    assert spike.feed(100, None)
    clock[0] = 30
    assert not spike.feed(100, None)  # once a minute at most
    clock[0] = 70
    assert spike.feed(97, None)
    assert not spike.feed(60, None)
    line = busy_pc.spike_line([("Game", 61.4), ("Chrome", 20.2), ("Streamfog", 9.0)], 100)
    assert line == ("PC CPU 100% for 5 s or more (SprigCast not counted); heaviest: Game 61%, Chrome 20%, "
                    "Streamfog 9%")
    used = busy_pc.heavy_apps(wait=0.05, with_use=True)
    assert isinstance(used, list) and all(isinstance(pct, float) for _, pct in used)


def test_who_has_the_cpu_comes_from_two_looks_at_everything(monkeypatch):
    """PC 2026-11-21, a Defender scan: naming the heaviest apps asked psutil about each process in turn, and for
    the ones it may not open (MsMpEng itself) psutil walked every process with Python's lock held. The window
    waited up to 3.4 s. Two looks at everything, one call each, name them the same."""
    import os

    from sprigcast.core import processes

    R = processes.Running
    looks = [[R(1, 0, "MsMpEng.exe", 10.0), R(2, 0, "obs64.exe", 5.0), R(3, 0, "gone.exe", 1.0),
              R(os.getpid(), 0, "SprigCast.exe", 1.0), R(9, 0, "reused.exe", 100.0)],
             [R(1, 0, "MsMpEng.exe", 10.6), R(2, 0, "obs64.exe", 5.1), R(os.getpid(), 0, "SprigCast.exe", 9.0),
              R(9, 0, "new.exe", 0.2), R(0, 0, "", 99.0)]]
    monkeypatch.setattr(processes, "snapshot", lambda: looks.pop(0))
    from types import SimpleNamespace

    clock = iter([0.0, 1.0])
    monkeypatch.setattr(busy_pc, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda s: None))
    use = busy_pc.cpu_by_app(1.0)
    assert set(use) == {"Windows Defender", "OBS"}  # not SprigCast, not a process id used again by another app
    assert round(use["Windows Defender"]) == 60 and round(use["OBS"]) == 10  # % of one core


def test_a_job_of_windows_own_is_named_first_with_what_to_do():
    """PC 2026-11-21, live: a Defender scan pinned the CPU; the note said only that the PC was busy."""
    assert busy_pc.friendly("MsMpEng.exe") == "Windows Defender" and busy_pc.friendly("TiWorker.exe") == "Windows Update"
    jobs = busy_pc.background_jobs([("OBS", 30.0), ("Windows Update", 12.0), ("Windows Defender", 41.0),
                                    ("Windows Search", 2.0)])
    assert jobs == [("Windows Defender", 41.0)]  # Update at 12% under OBS's 30% isn't what holds the PC up
    # A tester's note (2026-11-27): "Windows Defender is using 10% of the CPU" with svchost at 20%.
    assert busy_pc.background_jobs([("svchost", 20.0), ("Windows Defender", 10.0)]) == []
    assert busy_pc.background_jobs([("Windows Defender", 12.0), ("dwm", 3.0)]) == [("Windows Defender", 12.0)]
    assert busy_pc.job_message(*jobs[0]) == \
        "Windows Defender is using 41% of the CPU: schedule its scans outside stream hours."
    assert busy_pc.busy_message(["Windows Defender", "OBS"], 100.0, None) == \
        "Your PC is busy (CPU 100%): Windows Defender is using it. Schedule its scans outside stream hours."
    assert "OBS and Chrome are using it too" in busy_pc.busy_message(["OBS", "Chrome"], 90.0, None)


def test_a_heavy_scan_comes_before_streaming_apps(monkeypatch):
    cores = busy_pc.os.cpu_count() or 1
    use = {"OBS": 20.0 * cores, "Meld Studio": 15.0 * cores, "Streamfog": 12.0 * cores, "Windows Defender": 40.0 * cores}
    monkeypatch.setattr(busy_pc, "cpu_by_app", lambda wait=1.0: dict(use))
    assert busy_pc.heavy_apps()[0] == "Windows Defender"
    use["Windows Defender"] = 3.0 * cores  # a light one waits its turn
    assert busy_pc.heavy_apps() == ["OBS", "Meld Studio", "Streamfog"]


def test_a_busy_graphics_card_doesnt_blame_windows_jobs():
    """Tester CRGPlays (2026-11-27): "graphics card 100%: Windows Defender is using it" while SprigCast drew on
    the laptop's small chip; a Defender scan is found by its CPU and doesn't draw."""
    text = busy_pc.busy_message(["Windows Defender"], 20, 100)
    assert "Defender" not in text and "graphics card 100%" in text and "Preview off" in text
    assert "OBS is using it" in busy_pc.busy_message(["Windows Defender", "OBS"], 20, 100)  # an app may draw
    assert "Defender" in busy_pc.busy_message(["Windows Defender"], 100, 100)  # the CPU is busy too: it counts


def test_a_pinned_graphics_card_eases_the_previews_until_it_calms(monkeypatch):
    """CRGPlays, 2026-11-27: the card pinned at 100% on a hybrid laptop; previews go to 15 a second meanwhile."""
    pytest = __import__("pytest")
    pytest.importorskip("PySide6.QtWidgets")
    import types

    from sprigcast.app import main_window

    clock = [0.0]
    monkeypatch.setattr(main_window.time, "monotonic", lambda: clock[0])
    w = types.SimpleNamespace()
    ease = lambda gpu: main_window.MainWindow._ease_previews(w, gpu)  # noqa: E731
    for t in range(0, 12, 2):
        clock[0] = float(t)
        ease(99.0)
    assert w._previews_eased
    clock[0] = 20.0
    ease(80.0)  # not calm yet
    for t in range(22, 60, 2):
        clock[0] = float(t)
        ease(40.0)
    assert not w._previews_eased


def test_a_card_still_pinned_at_15_gets_the_previews_at_a_few_a_second(monkeypatch):
    """Mikedog151, 2026-11-27: a Radeon R2 at 97-100% for half an hour with the previews at 15 a second."""
    pytest = __import__("pytest")
    pytest.importorskip("PySide6.QtWidgets")
    import types

    from sprigcast.app import main_window

    clock = [0.0]
    monkeypatch.setattr(main_window.time, "monotonic", lambda: clock[0])
    w = types.SimpleNamespace()
    def ease(gpu):
        main_window.MainWindow._ease_previews(w, gpu)

    for t in range(0, 12, 2):
        clock[0] = float(t)
        ease(92.0)  # busy, not pinned: 15 a second and no slower
    assert w._previews_eased and not getattr(w, "_previews_slow", False)
    for t in range(12, 30, 2):
        clock[0] = float(t)
        ease(99.0)
    assert not getattr(w, "_previews_slow", False)  # pinned for under 20 s
    for t in range(30, 36, 2):
        clock[0] = float(t)
        ease(99.0)
    assert w._previews_slow
    for t in range(36, 80, 2):
        clock[0] = float(t)
        ease(40.0)
    assert not w._previews_eased and not w._previews_slow


def test_a_hybrid_laptop_is_told_from_a_pc_with_one_card():
    """CRGPlays, 2026-11-27: the GTX 1070 has no screen wired to it; the HD 630 has the laptop's screen."""
    from sprigcast import sprigcore

    nvidia = {"name": "NVIDIA GeForce GTX 1070", "software": False, "screens": 0}
    intel = {"name": "Intel(R) HD Graphics 630", "software": False, "screens": 1}
    basic = {"name": "Microsoft Basic Render Driver", "software": True, "screens": 0}
    assert sprigcore.hybrid_split([nvidia, intel, basic]) == ("NVIDIA GeForce GTX 1070", "Intel(R) HD Graphics 630")
    assert sprigcore.hybrid_split([dict(nvidia, screens=2), basic]) is None  # a desktop
    assert sprigcore.hybrid_split([dict(nvidia, screens=1), intel]) is None  # a monitor on the card's own port
    assert sprigcore.hybrid_split([dict(nvidia, screens=0), dict(intel, screens=0)]) is None  # an older engine DLL
