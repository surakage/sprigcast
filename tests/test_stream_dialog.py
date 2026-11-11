import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, Output, SceneCollection  # noqa: E402


@pytest.fixture
def fake_keyring(monkeypatch):
    import keyring

    store = {}
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: store.get((svc, ref)))
    monkeypatch.setattr(keyring, "set_password", lambda svc, ref, key: store.__setitem__((svc, ref), key))
    monkeypatch.setattr(keyring, "delete_password", lambda svc, ref: store.pop((svc, ref), None))
    return store


def test_dialog_saves_outputs_and_keys_only_to_keyring(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        outputs=[Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/twitch")],
    )
    d = StreamPage(col)
    d.key.setText("live_abc")
    d.key.textEdited.emit("live_abc")
    d._add()
    d.service.setCurrentIndex(d.service.findData("tiktok"))  # switches canvas to vertical
    d.server.setText("rtmp://push.tiktok.example/live")
    d.server.textEdited.emit("")
    d.key.setText("tt_key")
    d.key.textEdited.emit("")
    assert d.apply_to(col)
    assert [(o.service, o.canvas) for o in col.outputs] == [("twitch", "horizontal"), ("tiktok", "vertical")]
    assert col.outputs[1].server == "rtmp://push.tiktok.example/live"
    assert fake_keyring[("SprigCast", "sprigcast/twitch")] == "live_abc"
    assert fake_keyring[("SprigCast", col.outputs[1].key_ref)] == "tt_key"
    assert "live_abc" not in col.model_dump_json() and "tt_key" not in col.model_dump_json()


def test_picking_tiktok_applies_its_preset(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
    )
    d = StreamPage(col)
    d._add()
    d.service.setCurrentIndex(d.service.findData("tiktok"))
    out = d.outputs[0]
    assert (out.canvas, out.size, out.fps, out.bitrate_kbps) == ("vertical", (720, 1280), 30, 4000)
    assert not d.bandwidth_test.isVisibleTo(d)
    d.service.setCurrentIndex(d.service.findData("twitch"))
    assert (out.canvas, out.size, out.fps, out.bitrate_kbps) == ("horizontal", None, None, 6000)
    assert d.bandwidth_test.isVisibleTo(d)
    d.bandwidth_test.setChecked(True)
    assert out.bandwidth_test


def test_twitch_account_row(fake_keyring, monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.output import twitch_auth

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        outputs=[Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/twitch")],
    )
    d = StreamPage(col)
    assert d.twitch_row.isVisibleTo(d) and d.twitch_button.text() == "Connect Account (recommended)"
    assert not d.use_account.isVisibleTo(d)
    monkeypatch.setattr(twitch_auth, "connected_account", lambda: "surakage")
    d._show_service(d.outputs[0])
    assert d.twitch_label.text() == "Connected as surakage" and d.use_account.isVisibleTo(d)
    d.use_account.setChecked(True)
    assert d.outputs[0].twitch_account
    assert not d.key_row.isVisibleTo(d)  # the key comes from the account, nothing to paste
    d.service.setCurrentIndex(d.service.findData("tiktok"))
    assert not d.twitch_row.isVisibleTo(d) and not d.outputs[0].twitch_account


def test_multitrack_and_advanced_options(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        outputs=[Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/twitch")],
    )
    d = StreamPage(col)
    assert not d.extra_canvas.isEnabled()  # Enhanced off
    d.enhanced.setChecked(True)
    assert d.enhanced_kbps_auto.isChecked() and not d.enhanced_kbps.isEnabled()
    d.enhanced_kbps_auto.setChecked(False)
    assert d.outputs[0].enhanced_max_kbps == 8000  # a sensible start, not 0
    d.enhanced_tracks_auto.setChecked(False)
    d.enhanced_tracks.setValue(2)
    d.extra_canvas.setCurrentIndex(d.extra_canvas.findData("vertical"))
    out = d.outputs[0]
    assert out.enhanced and out.enhanced_vertical and out.enhanced_max_tracks == 2
    d.enhanced_tracks_auto.setChecked(True)
    assert d.outputs[0].enhanced_max_tracks == 0
    assert d.limits.isVisibleTo(d) and "6000 kbps" in d.limits.text()
    d.ignore_recommendations.setChecked(True)
    assert d.outputs[0].ignore_recommendations and not d.limits.isVisibleTo(d)
    d2 = StreamPage(SceneCollection(canvases=col.canvases, outputs=[d.outputs[0]]))
    assert d2.extra_canvas.currentData() == "vertical" and not d2.enhanced_kbps_auto.isChecked()
