"""A PC with little memory (Mikedog151: AMD E2-7110, 4 GB, hard drive, 2026-11-25) does its background work
later and lighter: the start-up warm-up froze the window 125 s, a report being made froze Settings 25 s, pop-ups
painting their shadows waited 7-12 s on the disk, and finding FFmpeg's files at go-live held the engine 28 frames."""

import zipfile

from sprigcast.app import tester_report, theme
from sprigcast.core import pacing, warm


def test_under_six_gb_counts_as_little_memory():
    assert pacing.low_memory(4.0) and not pacing.low_memory(8.0) and not pacing.low_memory(6.0)


def test_the_warm_up_waits_longer_and_goes_slower_with_little_memory(monkeypatch):
    monkeypatch.setattr(pacing, "low_memory", lambda ram_gb=None: True)
    assert warm.warm_after_ms() == warm.LOW_MEMORY_WARM_AFTER_MS and warm.pause_between() == warm.LOW_MEMORY_PAUSE_S
    monkeypatch.setattr(pacing, "low_memory", lambda ram_gb=None: False)
    assert warm.warm_after_ms() == warm.WARM_AFTER_MS and warm.pause_between() < 0.1


def test_ffmpeg_files_are_looked_for_once():
    from sprigcast import sprigcore

    assert hasattr(sprigcore._ffmpeg_file, "cache_info")  # kept: no folder look at each encoder start


def test_logs_are_cleaned_a_few_lines_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(tester_report, "CHUNK_BYTES", 64)
    lines = [f"line {i} key=live_123456_abcdefghijkl from 192.0.2.{i % 250}\n" for i in range(200)]
    log = tmp_path / "sprigcast 2026-11-25.log"
    log.write_text("".join(lines) + "no line end at the end key=hunter2secret", encoding="utf-8")
    calls = []

    def scrub(text):
        calls.append(text)
        return text.replace("live_123456_abcdefghijkl", "<hidden>")

    target = tmp_path / "out.zip"
    with zipfile.ZipFile(target, "w") as z:
        tester_report._copy_scrubbed(z, "logs/a.log", log, scrub, pause_s=0)
    with zipfile.ZipFile(target) as z:
        text = z.read("logs/a.log").decode()
    assert len(calls) > 10 and all(c.endswith("\n") for c in calls[:-1])  # whole lines each time
    assert "live_123456" not in text and text.count("<hidden>") == 200
    assert text.endswith("hunter2secret") and text.count("\n") == 200


def test_a_tail_starts_near_the_end(tmp_path):
    log = tmp_path / "crash.log"
    log.write_text("".join(f"start {i}\n" for i in range(1000)), encoding="utf-8")
    target = tmp_path / "out.zip"
    with zipfile.ZipFile(target, "w") as z:
        tester_report._copy_scrubbed(z, "logs/crash.log", log, lambda t: t, tail=100, pause_s=0)
    with zipfile.ZipFile(target) as z:
        text = z.read("logs/crash.log").decode()
    assert len(text) == 100 and text.endswith("start 999\n")


def test_fewer_logs_go_in_a_report_with_little_memory(tmp_path, monkeypatch):
    from datetime import datetime

    for day in range(20, 26):
        (tmp_path / f"sprigcast 2026-11-{day}.log").write_bytes(b"x\n" * (2 * 2**20))
    now = datetime(2026, 11, 26)
    assert len(tester_report._log_files(tmp_path, now, 30)) == 6
    assert len(tester_report._log_files(tmp_path, now, 30, tester_report.LOW_MEMORY_LOG_BYTES)) == 2


def test_no_card_shadow_with_little_memory(monkeypatch):
    monkeypatch.setattr(pacing, "low_memory", lambda ram_gb=None: True)
    made = []
    monkeypatch.setattr(theme, "card_shadow", lambda *a: made.append(a))

    class Box:
        def getRect(self):
            return (0, 0, 200, 100)

    class Card:
        def geometry(self):
            return Box()

    theme.paint_card_shadow(object(), Card())
    assert made == []
