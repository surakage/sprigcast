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


def test_a_sound_over_0_db_is_said_and_not_every_minute(caplog):
    log = LevelLog(now=0.0)
    with caplog.at_level("WARNING"):
        for minute in range(1, 4):
            log.feed({"sound-1": (11.0, 0.0), "mic": (-12.0, -20.0)}, now=minute * 60.0 - 1)
            log.feed({}, now=minute * 60.0 + 1)
    said = [r.getMessage() for r in caplog.records]
    assert said == ["sound sound-1 peaked at +11 dB. Above 0 dB it distorts: lower it by about 14 dB"]


def test_an_open_input_that_gives_only_silence_is_said_once(caplog):
    log = LevelLog(now=0.0, inputs=lambda: {"sound-1": "Digital Audio Interface (fifine Audio Capture)", "mic": "Mic"})
    with caplog.at_level("WARNING"):
        for minute in range(1, 7):
            log.feed({"sound-1": (-60.0, -60.0), "mic": (-20.0, -30.0)}, now=minute * 60.0 - 1)
            log.feed({}, now=minute * 60.0 + 1)
    said = [r.getMessage() for r in caplog.records]
    assert len(said) == 1 and said[0].startswith(
        "sound sound-1: only silence from Digital Audio Interface (fifine Audio Capture) for 3 min")
    assert "console" in said[0]
