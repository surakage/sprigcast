from sprigcast.audio.native_mixer import LevelLog


def test_level_log_once_a_minute():
    log = LevelLog(now=0.0)
    assert log.feed({"mic-1": (-30.0, -40.0), "duck": (0.0, 0.0)}, now=10.0) is None
    log.feed({"mic-1": (-12.4, -30.0), "browser-2": (-60.0, -60.0)}, now=20.0)
    line = log.feed({"mic-1": (-50.0, -55.0)}, now=61.0)
    assert line == "sound levels, loudest in the last minute: browser-2 silent, mic-1 -12 dB"
    assert log.feed({"mic-1": (-50.0, -55.0)}, now=70.0) is None  # a new minute started
