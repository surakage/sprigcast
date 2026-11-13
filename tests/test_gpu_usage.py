import sys

from sprigcast.app import gpu_usage


def test_task_manager_style_gpu_percent():
    samples = [
        ("pid_10_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 30.0),
        ("pid_20_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 25.0),  # same engine, another app: adds up
        ("pid_10_luid_0x0_0x1_phys_0_eng_5_engtype_VideoEncode", 40.0),
        ("pid_30_luid_0x0_0x2_phys_0_eng_0_engtype_3D", 5.0),  # another card
    ]
    assert gpu_usage.busiest_engine(samples) == 55.0
    assert gpu_usage.busiest_engine([]) == 0.0
    assert gpu_usage.busiest_engine([("pid_1_luid_a_eng_0", 80.0), ("pid_2_luid_a_eng_0", 70.0)]) == 100.0


def test_without_windows_counters_the_old_readers_are_used(monkeypatch):
    from sprigcast.app import doctor

    if sys.platform != "win32":
        monkeypatch.setattr(gpu_usage, "_counter", None)
        assert gpu_usage.windows_gpu_load() is None and gpu_usage._counter is False
    monkeypatch.setattr(gpu_usage, "windows_gpu_load", lambda: None)
    monkeypatch.setattr(doctor, "_nvml_load", lambda: 42.0)
    assert doctor.gpu_load() == 42.0
    monkeypatch.setattr(gpu_usage, "windows_gpu_load", lambda: 7.0)
    assert doctor.gpu_load() == 7.0
