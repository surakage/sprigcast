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
