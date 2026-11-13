import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import optimize  # noqa: E402
from sprigcast.core.scene import Canvas, EncoderSettings, Output, SceneCollection  # noqa: E402


def _collection(**kw):
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=30),
                  Canvas(id="vertical", width=1080, height=1920, fps=30)],
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=3000,
                        size=(1280, 720), enabled=True),
                 Output(id="tt", type="stream", service="tiktok", canvas="vertical", bitrate_kbps=6000, enabled=True)],
        stream_encoder=EncoderSettings(preset="p7", b_frames=2, lookahead=True),
        **kw,
    )


STRONG = dict(threads=16, memory_gb=32.0, codec="h264_nvenc")


def test_strong_nvenc_pc_with_good_upload_gets_1080p60_and_tiktok_stays_720p30():
    c = _collection()
    p = optimize.PcProfile(**STRONG, upload_kbps=20000)
    plan = optimize.plan(p, c)
    assert "1080p at 60 fps on NVENC" in plan.headline
    labels = [ch.label for ch in plan.changes]
    assert "Twitch: 1920×1080, 60 fps, 6000 kbps" in labels
    assert "TikTok: 720×1280, 30 fps, 4000 kbps" in labels
    assert any(lab.startswith("Horizontal canvas: 60 fps") for lab in labels)
    fps = plan.apply()
    tw, tt = c.outputs
    assert tw.size is None and tw.bitrate_kbps == 6000 and tw.fps is None  # follows the canvas, soon 60
    assert tt.size == (720, 1280) and tt.bitrate_kbps == 4000
    assert fps["horizontal"] == 60 and fps["vertical"] == 60  # vertical records at 60 too
    assert tt.fps == 30
    enc = c.stream_encoder
    assert (enc.b_frames, enc.lookahead, enc.preset) == (0, False, "p5")  # stays on the direct NVENC path
    assert c.recording.short_side is None and c.recording.bitrate_kbps == 12000
    assert optimize.plan(p, c, waiting_fps=fps).changes == []  # nothing left to suggest


def test_slow_upload_lowers_the_step_and_shares_upload_between_streams():
    c = _collection()
    p = optimize.PcProfile(**STRONG, upload_kbps=6000)  # 4500 for video, 2250 each minus audio
    plan = optimize.plan(p, c)
    tw = next(ch for ch in plan.changes if ch.label.startswith("Twitch"))
    assert tw.label == "Twitch: 1280×720, 30 fps, 2000 kbps"


def test_weak_pc_gets_light_settings():
    c = _collection()
    p = optimize.PcProfile(threads=4, memory_gb=8.0, codec="h264_qsv", upload_kbps=20000)
    plan = optimize.plan(p, c)
    assert "light side" in plan.headline
    plan.apply()
    assert c.outputs[0].size == (1280, 720) and c.recording.short_side == 720
    assert c.stream_encoder.preset == "p3" and c.stream_encoder.multipass == "disabled"


def test_cpu_encoding_follows_the_measured_speed():
    fast = optimize.PcProfile(threads=16, memory_gb=32.0, codec="libx264", x264_ms_720=1.0)
    slow = optimize.PcProfile(threads=8, memory_gb=16.0, codec="libx264", x264_ms_720=5.0)
    assert optimize.target(fast, encodes=1).step == (1080, 60)
    assert optimize.target(slow, encodes=1).step == (720, 60)  # 0.3 of the CPU
    assert optimize.target(slow, encodes=4).step == (480, 30)
    unknown = optimize.PcProfile(threads=16, memory_gb=32.0, codec="libx264")
    assert optimize.target(unknown).step == (720, 30)


def test_record_only_leaves_streams_alone_and_notes_are_given():
    c = _collection()
    p = optimize.PcProfile(**STRONG, wants_stream=False)
    plan = optimize.plan(p, c)
    assert not any(ch.group == "Stream" for ch in plan.changes)
    c2 = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    plan2 = optimize.plan(optimize.PcProfile(**STRONG), c2)
    assert any("No stream set up" in n for n in plan2.notes) and any("Upload not tested" in n for n in plan2.notes)


def test_new_checks_and_rules_can_be_added():
    seen = []
    check = optimize.Check("extra-test", "Extra", lambda p: seen.append(p) or "ok")
    rule = lambda p, c: [optimize.Change("Extra", "Something", "Nothing", lambda: seen.append("applied"))]  # noqa: E731
    optimize.register_check(check, before="upload")
    optimize.register_rule(rule)
    try:
        keys = [c.key for c in optimize.CHECKS]
        assert keys.index("extra-test") == keys.index("upload") - 1
        plan = optimize.plan(optimize.PcProfile(**STRONG), _collection())
        extra = [ch for ch in plan.changes if ch.group == "Extra"]
        plan.apply(extra)
        assert seen == ["applied"]
    finally:
        optimize.CHECKS.remove(check)
        optimize.RULES.remove(rule)


def test_upload_speed_uses_the_big_blocks():
    import time as t

    sent = []

    def post(data):
        sent.append(len(data))
        t.sleep(len(data) / 2_000_000)  # 16 Mbps

    kbps = optimize.upload_speed(seconds=0.6, post=post)
    assert 12000 < kbps < 17000
    assert optimize.upload_speed(seconds=0.2, post=lambda d: (_ for _ in ()).throw(OSError("offline"))) is None


def test_x264_speed_measures_something():
    ms = optimize.x264_speed(frames=5, width=320, height=180)
    assert ms is not None and ms > 0


def test_wizard_runs_checks_and_applies_ticked_changes(monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.wizard import OptimizeWizard

    QApplication.instance() or QApplication([])

    def fake(p):
        p.threads, p.memory_gb, p.codec, p.upload_kbps = 16, 32.0, "h264_nvenc", 20000
        return "fake"

    monkeypatch.setattr(optimize, "CHECKS", [optimize.Check("all", "Everything", fake)])
    c = _collection()
    w = OptimizeWizard(c, run_in_thread=False)
    w.test_upload.setChecked(False)
    w.start_checks()
    assert w.pages.currentIndex() == 2 and w.upload.value() == 20.0
    assert "fake" in w.check_list.item(0).text()
    w.change_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    w.upload.setValue(6.0)  # retyped: the suggestions follow
    assert any("2000 kbps" in w.change_list.item(i).text() for i in range(w.change_list.count()))
    w.change_list.item(0).setCheckState(Qt.CheckState.Unchecked)
    w._apply()
    assert w.result() == w.DialogCode.Accepted
    assert len(w.chosen) == w.change_list.count() - 1
    w.plan.apply(w.chosen)


def test_first_run_offers_the_wizard_once(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("OptimizeWindowTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_collection(), tmp_path / "scenes.json")
    try:
        opened = []
        monkeypatch.setattr(w, "open_optimizer", lambda first_run=False: opened.append(first_run))
        w.first_run_optimizer()
        w.first_run_optimizer()
        assert opened == [True]
        assert any(c.title == "Optimize for this PC" for c in w._palette_commands())
    finally:
        w.close()
        QSettings().clear()
