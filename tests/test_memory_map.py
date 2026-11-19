from sprigcast.core import memory_map as mm


def test_the_summary_groups_private_memory_by_block_and_names_the_biggest():
    MB = mm.MB
    regions = [
        (0x1000, 40 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE),  # one 64 MB block in two regions
        (0x1000, 24 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE),
        (0x9000, 2 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE),
        (0xA000, 8 * MB, 0x2000, mm.MEM_PRIVATE),  # reserved, not committed: not counted
        (0xB000, 10 * MB, mm.MEM_COMMIT, mm.MEM_MAPPED),
        (0xC000, 30 * MB, mm.MEM_COMMIT, mm.MEM_IMAGE),
    ]
    line = mm.summarize(regions)
    assert line == ("private 66 MB in 2 blocks (biggest 64, 2 MB; 64 MB in 1 of 16 MB or more), shared 10 MB, "
                    "program files 30 MB")


def test_off_windows_there_is_no_map_and_nothing_is_started(monkeypatch):
    import threading

    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self))
    monkeypatch.setattr(mm.sys, "platform", "linux")
    assert mm.memory_map_line() is None and mm.graphics_memory() is None
    mm.log_memory_map()
    assert started == []


def test_the_memory_line_also_logs_the_map(monkeypatch):
    from sprigcast.core import pacing

    asked = []
    monkeypatch.setattr(mm, "log_memory_map", lambda: asked.append(1))
    monkeypatch.setattr(pacing, "_memory_logged", None)
    assert pacing.watch_memory(now=1000.0).startswith("memory now:")
    assert asked == [1]


def test_big_blocks_say_how_they_were_allocated_and_which_are_heaps():
    MB = mm.MB
    regions = [
        (0x1000, 32 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE, 0x04),
        (0x5000, 21 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE, 0x404),  # a driver's write-combined upload memory
        (0x9000, 40 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE, 0x40),
        (0xA000, 2 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE, 0x04),  # small: left out
    ]
    assert mm.big_blocks(regions, heaps=frozenset({0x1000})) == \
        "40 MB execute-read-write, 32 MB read-write heap, 21 MB read-write write-combined"
    assert mm.big_blocks([(0x1000, 20 * MB, mm.MEM_COMMIT, mm.MEM_PRIVATE)]) == "20 MB"  # no protection known


def test_program_files_are_added_up_per_module():
    MB = mm.MB
    regions = [(0x1, 100 * MB, mm.MEM_COMMIT, mm.MEM_IMAGE, 0x80), (0x1, 80 * MB, mm.MEM_COMMIT, mm.MEM_IMAGE, 0x80),
               (0x2, 30 * MB, mm.MEM_COMMIT, mm.MEM_IMAGE, 0x80), (0x3, 5 * MB, 0x2000, mm.MEM_IMAGE, 0x80)]
    names = {0x1: "Qt6WebEngineCore.dll", 0x2: "avcodec-61.dll"}
    assert mm.program_files(regions, names.get) == "Qt6WebEngineCore.dll 180 MB, avcodec-61.dll 30 MB"


def test_an_encoder_starting_logs_the_map_before_and_after_once_for_encoders_starting_together(monkeypatch):
    asked = []
    monkeypatch.setattr(mm, "log_memory_map", lambda reason="", after_s=0.0: asked.append((reason, after_s)))
    monkeypatch.setattr(mm, "_encoder_logged", 0.0)
    mm.around_encoder("stream-horizontal")
    mm.around_encoder("horizontal")  # the recording, a moment later: the same pair covers it
    assert asked == [("before stream-horizontal starts", 0.0),
                     ("10 s after stream-horizontal started", mm.ENCODER_AFTER_S)]


def test_off_windows_the_threads_line_names_no_native_threads(monkeypatch):
    from sprigcast.core import pacing

    monkeypatch.setattr(pacing.sys, "platform", "linux")
    assert pacing.native_threads() == ""
