import sys

from sprigcast.tools import memwhere


def test_blocks_are_grouped_by_size_biggest_use_first():
    mb = 1 << 20
    blocks = [(8 * mb, 8 * mb, 1), (8 * mb, 6 * mb, 2), (512 * 1024, 300_000, 3), (300 * mb, 2 * mb, 4)]
    assert memwhere.size_buckets(blocks) == [
        ("4-16 MB", 2, 14 * mb), ("256 MB and up", 1, 2 * mb), ("under 1 MB", 1, 300_000)]


def test_report_never_raises(capsys):
    memwhere.report("in a test")
    out = capsys.readouterr().out
    if sys.platform != "win32":
        assert "needs Windows" in out
    else:
        assert "Where the Task Manager memory sits, in a test" in out and "Libraries" in out


def test_growth_between_snapshots_names_new_blocks_and_libraries():
    before = {"block 0x10": (8_000_000, 1_000_000), "nvml.dll": (0, 500_000)}
    after = {"block 0x10": (8_000_000, 7_000_000), "block 0x20": (1_073_741_824, 50_000_000),
             "nvml.dll": (0, 20_500_000), "small.dll": (0, 600_000)}
    assert memwhere.grown(before, after) == ["new 1074 MB block +50 MB", "nvml.dll +20 MB", "8 MB block +6 MB"]
    assert memwhere.grown(None, after) == []
