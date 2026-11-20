"""PC 2026-11-17..20: the card stopped ~0.75 s for the whole app a few times a day, idle or live; who used it?"""

import os

from sprigcast.app import gpu_usage as gu


def test_the_busiest_processes_each_at_their_busiest_engine():
    samples = [
        ("pid_10_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 20.0),
        ("pid_10_luid_0x0_0x1_phys_0_eng_5_engtype_VideoEncode", 30.0),
        ("pid_42_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 85.0),
        ("pid_42_luid_0x0_0x1_phys_0_eng_1_engtype_Copy", 5.0),
        ("pid_7_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 0.2),  # idle: left out
        ("odd name", 50.0),
    ]
    assert gu.busiest_processes(samples) == [(42, "3D", 85.0), (10, "VideoEncode", 30.0)]


def test_the_line_names_each_program_and_marks_this_one():
    line = gu.describe_processes([(os.getpid(), "3D", 12.0), (2 ** 30, "Copy", 3.0)])
    assert f"(pid {os.getpid()}, this app) 3D 12%" in line and f"? (pid {2 ** 30}) Copy 3%" in line
    assert gu.describe_processes([]) == "nothing busy"


def test_a_stall_is_looked_into_once_a_minute_and_never_off_windows(monkeypatch):
    started = []
    monkeypatch.setattr(gu.threading.Thread, "start", lambda self: started.append(self.name))
    monkeypatch.setattr(gu.sys, "platform", "linux")
    gu.log_gpu_users_later(750.0)
    assert started == []
    monkeypatch.setattr(gu.sys, "platform", "win32")
    monkeypatch.setattr(gu, "_stall_logged", 0.0)
    monkeypatch.setattr(gu.time, "monotonic", lambda: 1000.0)
    gu.log_gpu_users_later(750.0)
    gu.log_gpu_users_later(760.0)  # the same minute
    assert started == ["sprigcast-gpu-stall"]
