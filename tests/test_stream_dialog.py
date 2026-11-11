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
