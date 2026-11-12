import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from sprigcast.app.doctor import Checkup, LastStream, PeakWatch, examine, headline, load_last_streams  # noqa: E402
from sprigcast.core.scene import Canvas, Output, SceneCollection, SourceConfig  # noqa: E402


def _collection(**output):
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080, fps=60)],
        sources=[SourceConfig(id="mic", kind="audio_input", name="Mic"),
                 SourceConfig(id="desk", kind="audio_output", name="Desktop audio"),
                 SourceConfig(id="cam", kind="camera", name="Webcam")],
        outputs=[Output(id="tw", type="stream", service="twitch", canvas="horizontal", enabled=True,
                        key_ref="tw", bitrate_kbps=6000, **output)],
    )


def _checkup(**kw):
    base = dict(collection=_collection(), has_key=lambda ref: True, render_fps=60, render_ms=4,
                loudest={"mic": -20, "desk": -30}, statuses={"cam": "capturing 1280x720 @ 30"},
                last_streams={"tw": LastStream(kbps=6000, network_pct=5, loss_pct=0, encode_pct=20, bitrate_kbps=6000)})
    base.update(kw)
    return Checkup(**base)


def test_all_clear():
    findings = examine(_checkup())
    assert findings[0].level == "good"
    assert headline(findings) == "Ready to go live"


def test_missing_key_is_the_headline():
    findings = examine(_checkup(has_key=lambda ref: False, statuses={"cam": "camera not found, retrying"}))
    assert findings[0].level == "bad" and findings[0].area == "Keys" and "stream key" in findings[0].text
    webcam = next(f for f in findings if f.area == "Sources")
    assert webcam.title == "Webcam isn't working (camera not found, retrying)" and webcam.fix.startswith("Check")
    assert headline(findings) == "2 things to fix"


def test_slow_upload_suggests_a_lower_bitrate_until_lowered():
    slow = {"tw": LastStream(kbps=5000, network_pct=80, loss_pct=6, encode_pct=20, bitrate_kbps=6000)}
    findings = examine(_checkup(last_streams=slow))
    assert findings[0].area == "Upload" and "4000 kbps" in findings[0].text
    lowered = _checkup(last_streams=slow)
    lowered.collection.outputs[0].bitrate_kbps = 4000
    assert examine(lowered)[0].level == "good"


def test_gpu_and_encoder_and_audio_warnings():
    findings = examine(_checkup(codec="libx264", render_fps=41, gpu_pct=97, loudest={"mic": -70, "desk": 0}))
    text = " ".join(f.text for f in findings)
    assert findings[0].area == "GPU" and "41 of 60 fps" in findings[0].text
    assert "NVIDIA encoder" in text and "97% busy" in text
    assert "Mic heard nothing" in text and "Desktop audio is clipping" in text


def test_muted_mic_and_no_destination():
    col = _collection()
    col.sources[0].settings["muted"] = True
    col.outputs[0].enabled = False
    text = " ".join(f.text for f in examine(_checkup(collection=col)))
    assert "Mic is muted" in text and "No destination is switched on" in text


def test_peak_watch_forgets_old_peaks():
    watch = PeakWatch(window=3)
    watch.feed({"mic": (-10, -20)}, now=0)
    watch.feed({"mic": (-40, -50)}, now=2)
    assert watch.loudest() == {"mic": -10}
    watch.feed({"mic": (-45, -50)}, now=5)
    assert watch.loudest() == {"mic": -40}


def test_last_streams_round_trip(tmp_path):
    from PySide6.QtCore import QSettings

    from sprigcast.app.doctor import save_last_streams

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)

    class FakeStreamer:
        output_id, bitrate_kbps, bytes_out, started_at = "tw", 6000, 750_000, 0.0

        def stats(self):
            return {"frames": 600, "dropped_pct": 2.0, "network_pct": 70.0, "encode_ms": 3.0, "budget_ms": 16.7}

    save_last_streams(settings, [("horizontal", FakeStreamer())])
    last = load_last_streams(settings)["tw"]
    assert last.loss_pct == 2.0 and last.network_pct == 70.0 and last.bitrate_kbps == 6000


def test_dialog_shows_findings():
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.doctor import DoctorDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dialog = DoctorDialog(lambda: _checkup(has_key=lambda ref: False), listen=0)
    assert "Checking" in dialog.headline.text()
    dialog.finish()
    assert dialog.headline.text().endswith("to fix") and dialog.badge.text() == "✕"
    assert not dialog.busy.isVisibleTo(dialog)
