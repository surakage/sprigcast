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
