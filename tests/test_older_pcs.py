"""A 2013 all-in-one (Core i3/i5 4th gen or AMD A-series, Intel HD 4000/4600 or Radeon APU graphics,
often a hard drive) is noticed and gets light settings; graphics without OpenGL 3.3 get a clear message."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app import lowend, optimize  # noqa: E402
from sprigcast.core.scene import Canvas, Output, SceneCollection, SourceConfig  # noqa: E402
from sprigcast.render import compositor  # noqa: E402

AIO_2013 = dict(threads=4, memory_gb=8.0, codec="h264_qsv", cpu_name="Intel(R) Core(TM) i5-4570S CPU @ 2.90GHz")
AIO_GPUS = ("Intel(R) HD Graphics 4600",)


def _collection(tiktok: bool = False):
    outputs = [Output(id="tw", type="stream", service="twitch", canvas="horizontal", bitrate_kbps=6000)]
    if tiktok:
        outputs.append(Output(id="tt", type="stream", service="tiktok", canvas="vertical", bitrate_kbps=3000))
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60),
                  Canvas(id="vertical", width=1080, height=1920, fps=60)],
        outputs=outputs,
        sources=[SourceConfig(id="a", kind="browser", name="Alerts", settings={"url": "https://x", "custom_fps": True,
                                                                             "fps": 60}),
                 SourceConfig(id="b", kind="browser", name="Chat", settings={"url": "https://y"}),
                 SourceConfig(id="c", kind="color", name="Back", settings={})],
    )


@pytest.mark.parametrize("cpu", [
    "Intel(R) Core(TM) i5-4570S CPU @ 2.90GHz", "Intel(R) Core(TM) i3-3220T CPU @ 2.80GHz",
    "Intel(R) Core(TM) i7-4770 CPU @ 3.40GHz", "AMD A8-6500 APU with Radeon(tm) HD Graphics",
    "Intel(R) Pentium(R) CPU G3220 @ 3.00GHz", "Intel(R) Celeron(R) CPU 1037U @ 1.80GHz", "AMD E1-2500 APU"])
def test_older_cpus_are_noticed(cpu):
    assert lowend.older_cpu(cpu)


@pytest.mark.parametrize("cpu", [
    "AMD Ryzen 7 8700F 8-Core Processor", "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz",
    "13th Gen Intel(R) Core(TM) i5-13400", "Intel(R) Core(TM) i5-6500 CPU @ 3.20GHz", "AMD Athlon 3000G", ""])
def test_newer_cpus_are_not(cpu):
    assert not lowend.older_cpu(cpu)


def test_older_built_in_graphics_count_only_without_a_graphics_card():
    assert lowend.older_gpu(("Intel(R) HD Graphics 4000",)) == "Intel(R) HD Graphics 4000"
    assert lowend.older_gpu(("AMD Radeon HD 8570D",)) and lowend.older_gpu(("AMD Radeon(TM) R7 Graphics",))
    assert lowend.older_gpu(("Intel(R) HD Graphics 4600", "NVIDIA GeForce GT 740M")) == ""
    for newer in ("Intel(R) UHD Graphics 630", "Intel(R) HD Graphics 530", "AMD Radeon(TM) Graphics",
                  "Intel(R) Iris(R) Xe Graphics", "NVIDIA GeForce RTX 4070"):
        assert lowend.older_gpu((newer,)) == ""


def test_a_2013_all_in_one_is_on_the_light_side():
    facts = lowend.PcFacts(4, 8.0, "h264_qsv", AIO_2013["cpu_name"], AIO_GPUS)
    reasons = lowend.weak_reasons(facts)
    assert "an older CPU" in reasons and "older built-in graphics (Intel HD Graphics 4600)" in reasons
    # even with 8 threads and 16 GB, an older CPU alone keeps it light
    assert lowend.weak_reasons(lowend.PcFacts(8, 16.0, "h264_qsv", "Intel(R) Core(TM) i7-4770 CPU")) == ["an older CPU"]


def test_older_pc_gets_one_canvas_clips_on_press_and_lighter_browser_sources():
    col = _collection()
    col.recording.clips_on_start = True
    rows = lowend.older_changes(col)
    assert [r[0] for r in rows] == ["Video", "Recording", "Video"]
    lowend.older_changes(col, apply=True)
    assert [c.enabled for c in col.canvases] == [True, False]  # vertical off, nothing streamed from it
    assert col.recording.canvases == ["horizontal"]
    assert not col.recording.clips_on_start and col.recording.clips_idle_minutes == lowend.OLDER_CLIPS_IDLE
    from sprigcast.sources.browser import page_fps

    assert [page_fps(s.settings) for s in col.sources if s.kind == "browser"] == [24, 24]
    assert col.sources[0].settings["url"] == "https://x"
    assert lowend.older_changes(col) == []  # nothing left to do


def test_older_pc_sends_twitch_one_quality_and_reads_cameras_at_720p30():
    col = _collection()
    col.sources.append(SourceConfig(id="cam", kind="camera", name="Camera",
                                    settings={"width": 1920, "height": 1080, "fps": 60}))
    for o in col.outputs:
        if o.type == "stream":
            o.enhanced = True
            break
    rows = lowend.older_changes(col)
    assert "Streaming" in [r[0] for r in rows] and any("camera" in r[1] for r in rows)
    lowend.older_changes(col, apply=True)
    assert not any(o.enhanced for o in col.outputs)
    cam = next(s for s in col.sources if s.id == "cam")
    assert (cam.settings["width"], cam.settings["height"], cam.settings["fps"]) == (1280, 720, 30)
    assert lowend.older_changes(col) == []


def test_a_canvas_with_a_stream_stays_on():
    col = _collection(tiktok=True)
    assert lowend.spare_canvas(col) is None
    col.outputs[0].canvas = "vertical"  # everything goes out vertical: the horizontal one is spare
    col.outputs = [o for o in col.outputs if o.id != "tt"]
    assert lowend.spare_canvas(col).id == "horizontal"


def test_hard_drive_records_one_mkv_file():
    col = _collection()
    col.recording.container, col.recording.source_record = "mp4", True
    lines, _ = lowend.light_changes(col, hdd=True)
    assert any("MKV" in line for line in lines) and not any("canvas off" in line for line in lines)
    lowend.light_changes(col, apply=True, hdd=True)
    assert col.recording.container == "mkv" and not col.recording.source_record
    assert all(c.enabled for c in col.canvases)  # hard drive alone: the rest stays


def test_optimize_plans_720p30_on_the_graphics_encoder_with_the_older_pc_changes():
    p = optimize.PcProfile(**AIO_2013, upload_kbps=10000)
    p.extra["gpus"], p.extra["hdd"] = AIO_GPUS, True
    col = _collection()
    col.recording.container = "mp4"
    assert p.older and p.hardware
    plan = optimize.plan(p, col)
    assert plan.recommended == (720, 30)
    labels = [c.label for c in plan.changes]
    assert any("Vertical canvas off" in label for label in labels)
    assert any("MKV" in label for label in labels)
    assert any("browser sources drawn at 24 fps" in label for label in labels)
    assert "older built-in graphics" in plan.headline
    plan.apply()
    assert col.recording.container == "mkv" and not col.canvas("vertical").enabled


def test_newer_pc_on_an_ssd_gets_no_older_changes():
    p = optimize.PcProfile(threads=16, memory_gb=32.0, codec="h264_nvenc", cpu_name="AMD Ryzen 7 8700F 8-Core Processor")
    p.extra["gpus"], p.extra["hdd"] = ("NVIDIA GeForce RTX 4070",), False
    assert not p.older and optimize.older_rule(p, _collection()) == []


def test_without_opengl_3_3_the_message_says_what_to_do():
    old = compositor.graphics_message("3.1.0 - Build 9.17.10.4459", "Intel(R) HD Graphics 3000")
    assert "OpenGL 3.3" in old and "3.1" in old and "Intel(R) HD Graphics 3000" in old and "driver" in old
    none = compositor.graphics_message("1.1.0", "GDI Generic")
    assert "without a graphics driver" in none
    assert "without a graphics driver" in compositor.graphics_message("", "")
    other = compositor.graphics_message("4.0.0 - Build 10.18.10.5161", "Intel(R) HD Graphics 4000", "boom")
    assert "Update the graphics driver" in other and "boom" in other
    assert compositor.gl_version("4.0.0 - Build 10.18") == (4, 0) and compositor.gl_version("x") is None


def test_a_failed_context_raises_the_message_not_a_crash(monkeypatch):
    calls = []

    class Probe:
        info = {"GL_VERSION": "3.1.0 - Build 9.17", "GL_RENDERER": "Intel(R) HD Graphics 3000"}

        def release(self):
            pass

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs.get("require") == 330:
            raise RuntimeError("cannot create context")
        return Probe()

    monkeypatch.setattr(compositor.moderngl, "create_standalone_context", fake)
    with pytest.raises(compositor.GraphicsError) as err:
        compositor.create_context()
    assert "offer OpenGL 3.1" in str(err.value)


def test_engine_reports_graphics_trouble_as_a_graphics_error(monkeypatch):
    import queue

    from sprigcast.core.events import EngineError
    from sprigcast.render import engine

    def broken(*a, **k):
        raise compositor.GraphicsError("SprigCast needs OpenGL 3.3 graphics.")

    monkeypatch.setattr(compositor, "Compositor", broken)
    eng = engine.Engine.__new__(engine.Engine)
    eng.events = queue.Queue()
    eng._run()
    event = eng.events.get_nowait()
    assert isinstance(event, EngineError) and event.graphics and "OpenGL 3.3" in event.message


def test_older_pc_decodes_videos_on_the_graphics_card():
    col = _collection()
    col.sources += [SourceConfig(id="v", kind="media", name="Intro", settings={"path": "C:/clips/intro.mp4"}),
                    SourceConfig(id="s", kind="media", name="Ding", settings={"path": "C:/sounds/ding.mp3"})]
    rows = [r for r in lowend.older_changes(col) if "graphics card" in r[1]]
    assert [r[1] for r in rows] == ["1 video decoded on the graphics card (no picture copied up to it each frame)"]
    lowend.older_changes(col, apply=True)
    assert col.sources[-2].settings["hw_decode"] and not col.sources[-1].settings.get("hw_decode")
    assert not [r for r in lowend.older_changes(col) if "graphics card" in r[1]]
    assert not [r for r in lowend.older_changes(col, older=False) if "graphics card" in r[1]]


MIKEDOG = dict(threads=2, memory_gb=4.0, codec="h264_amf", cpu_name="AMD E2-7110 APU with AMD Radeon R2 Graphics")


def test_older_amd_graphics_streams_with_x264_and_keeps_it():
    p = optimize.PcProfile(**MIKEDOG, upload_kbps=10000)
    p.extra["gpus"], p.extra["hdd"] = ("AMD Radeon(TM) R2 Graphics",), False
    assert p.cpu_encodes and not p.hardware and p.used_codec == "libx264"
    col = _collection()
    plan = optimize.plan(p, col)
    assert plan.recommended == (720, 30)
    moved = [c for c in plan.changes if c.label.startswith("Stream encoder")]
    assert [c.label for c in moved] == [
        "Stream encoder: x264 on the CPU, the built-in graphics only draws, superfast preset"]
    assert moved[0].now == "AMF"
    plan.apply()
    assert col.stream_encoder.encoder == "libx264" and col.stream_encoder.preset == lowend.CPU_PRESET
    again = optimize.plan(p, col)
    assert not [c for c in again.changes if c.label.startswith("Stream encoder")]


def test_intel_quick_sync_and_graphics_cards_keep_their_encoder():
    p = optimize.PcProfile(**AIO_2013)
    p.extra["gpus"] = AIO_GPUS
    assert not p.cpu_encodes and p.hardware
    card = optimize.PcProfile(**{**MIKEDOG, "codec": "h264_amf"})
    card.extra["gpus"] = ("AMD Radeon(TM) R2 Graphics", "AMD Radeon RX 580")
    assert not card.cpu_encodes


def test_light_settings_move_the_older_amd_pc_to_x264():
    col = _collection()
    lines, _ = lowend.light_changes(col, cpu_encodes=True)
    assert "Encoded with x264 on the CPU: the built-in graphics only draws" in lines
    assert any("superfast" in line for line in lines)
    lowend.light_changes(col, apply=True, cpu_encodes=True)
    assert col.stream_encoder.encoder == col.recording.encoder.encoder == "libx264"
    assert "Encoded with x264 on the CPU: the built-in graphics only draws" not in lowend.light_changes(
        col, cpu_encodes=True)[0]


def test_older_amd_graphics_skips_the_amd_encoder_check(monkeypatch):
    from sprigcast.output import recorder

    tried = []
    monkeypatch.setattr(recorder, "_picked", None)
    monkeypatch.setattr(recorder, "_draws_only", None)
    monkeypatch.setattr(recorder, "nvenc_works", lambda: False)
    monkeypatch.setattr(recorder, "encoder_works", lambda codec: tried.append(codec) or codec == "libx264")
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("AMD Radeon(TM) R2 Graphics",))
    monkeypatch.delenv("SPRIGCAST_ENCODER", raising=False)
    assert recorder.pick_codec() == "libx264" and tried == []
    assert recorder.codec_label("libx264") == "x264 (CPU, the built-in graphics only draws)"
    monkeypatch.setattr(recorder, "_picked", None)
    monkeypatch.setattr(recorder, "_draws_only", None)
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("Intel(R) HD Graphics 4600",))
    tried.clear()
    recorder.pick_codec()
    assert "h264_amf" in tried and "h264_qsv" in tried


def test_auto_already_on_x264_needs_no_encoder_row():
    p = optimize.PcProfile(**{**MIKEDOG, "codec": "libx264"}, upload_kbps=10000)
    p.extra["gpus"], p.extra["hdd"] = ("AMD Radeon(TM) R2 Graphics",), False
    assert "no graphics-card encoder, so the CPU encodes" not in p.weak_reasons
    plan = optimize.plan(p, _collection())
    assert not [c for c in plan.changes if "built-in graphics only draws" in c.label]
    assert "Encoded with x264 on the CPU: the built-in graphics only draws" not in lowend.light_changes(
        _collection(), cpu_encoding=True, cpu_encodes=True)[0]


def test_a_light_pc_never_optimized_is_reminded(monkeypatch):
    monkeypatch.setattr(lowend, "stored_tier", lambda: None)
    monkeypatch.setattr(lowend, "gpu_names", lambda: ("AMD Radeon(TM) R2 Graphics",))
    monkeypatch.setattr(lowend, "cpu_name", lambda: MIKEDOG["cpu_name"])
    assert lowend.light_unset()
    monkeypatch.setattr(lowend, "stored_tier", lambda: "light")
    assert lowend.light_unset() == []
