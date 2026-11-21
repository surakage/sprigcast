from sprigcast.audio.native_mixer import LevelLog


def test_level_log_once_a_minute():
    log = LevelLog(now=0.0)
    assert log.feed({"mic-1": (-30.0, -40.0), "duck": (0.0, 0.0)}, now=10.0) is None
    log.feed({"mic-1": (-12.4, -30.0), "browser-2": (-60.0, -60.0)}, now=20.0)
    line = log.feed({"mic-1": (-50.0, -55.0)}, now=61.0)
    assert line == "sound levels, loudest in the last minute: mic-1 -12 dB, 1 silent"
    assert log.feed({"mic-1": (-50.0, -55.0)}, now=70.0) is None  # a new minute started


def test_level_log_names_a_stinger_by_its_file_only():
    log = LevelLog(now=0.0)
    log.feed({"stinger:C:\\Users\\someone\\Videos\\swoosh.webm": (-6.0, -9.0), "stinger:/home/a/b.webm": (-3.0, -5.0)},
             now=1.0)
    line = log.feed({}, now=61.0)
    assert line == "sound levels, loudest in the last minute: stinger:b.webm -3 dB, stinger:swoosh.webm -6 dB"
    assert "someone" not in line


def test_level_log_all_silent_is_one_count():
    log = LevelLog(now=0.0)
    log.feed({f"browser-{n}": (-60.0, -60.0) for n in range(70)}, now=1.0)
    assert log.feed({}, now=61.0) == "sound levels, loudest in the last minute: 70 silent"
