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


def test_the_video_encoder_load_counts_only_encode_engines():
    from sprigcast.app.gpu_usage import busiest_engine

    samples = [("pid_1_luid_0x0_0x1_phys_0_eng_0_engtype_3D", 60.0),
               ("pid_1_luid_0x0_0x1_phys_0_eng_5_engtype_VideoEncode", 12.0),
               ("pid_2_luid_0x0_0x1_phys_0_eng_5_engtype_VideoEncode", 18.0)]
    assert busiest_engine(samples) == 60.0
    assert busiest_engine(samples, "VideoEncode") == 30.0
    assert busiest_engine(samples[:1], "VideoEncode") == 0.0


def test_load_by_card_tells_the_engines_card_from_the_screens_chip():
    """CRGPlays, 2026-11-27: "graphics card 100%" on a hybrid laptop, and nothing said which chip."""
    from sprigcast.app import gpu_usage

    nvidia, intel = gpu_usage.card_key((0xD1A2, 0)), gpu_usage.card_key((0x5E0F, 0))
    samples = [
        (f"pid_10_luid_{nvidia}_phys_0_eng_0_engtype_3D", 30.0),
        (f"pid_10_luid_{nvidia}_phys_0_eng_2_engtype_Copy", 12.0),
        (f"pid_20_luid_{intel}_phys_0_eng_0_engtype_3D", 64.0),
        (f"pid_10_luid_{intel}_phys_0_eng_0_engtype_3D", 40.0),
    ]
    cards = gpu_usage.by_card(samples)
    assert cards[nvidia][:2] == (30.0, "3D") and cards[intel][:2] == (100.0, "3D")
    assert cards[intel][2] == [(20, 64.0), (10, 40.0)]
    names = {10: "SprigCast.exe", 20: "dwm.exe"}
    line = gpu_usage.describe_cards(cards, nvidia, names.get)
    assert line == "SprigCast's card 3D 30% (SprigCast.exe 30%); another card 3D 100% (dwm.exe 64%, SprigCast.exe 40%)"
