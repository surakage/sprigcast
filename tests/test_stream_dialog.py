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
    monkeypatch.setattr(twitch_auth, "remembered_login", lambda: "surakage")
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


def test_add_starts_a_blank_place_until_a_service_is_picked(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import NEW_PLACE, PICK_SERVICE, StreamPage

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        outputs=[Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/twitch")],
    )
    d = StreamPage(col)
    d._add()
    new = d.outputs[-1]
    assert new.service is None and not new.enabled
    assert d.service.currentText() == PICK_SERVICE and d.hero.text() == NEW_PLACE
    assert d.list.item(1).text() == f"{NEW_PLACE}\n{PICK_SERVICE}"
    assert not d._picture_section.isVisibleTo(d) and not d.key_row.isVisibleTo(d) and not d.enabled.isVisibleTo(d)
    assert d.apply_to(col) and [o.id for o in col.outputs] == ["out-twitch"]  # left blank: not kept
    d.list.setCurrentRow(0)
    d.list.setCurrentRow(1)
    assert d.service.currentText() == PICK_SERVICE  # still blank when you come back to it
    d.service.setCurrentIndex(d.service.findData("youtube"))
    assert new.service == "youtube" and new.enabled and d._picture_section.isVisibleTo(d)
    assert d.list.item(1).text().startswith("YouTube")
    assert d.apply_to(col) and [o.service for o in col.outputs] == ["twitch", "youtube"]
    d.list.setCurrentRow(0)
    assert d.service.currentData() == "twitch" and d._picture_section.isVisibleTo(d)


def test_switching_service_clears_the_old_server(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage

    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id="yt", type="stream", service="youtube", canvas="horizontal", key_ref="sprigcast/yt")],
    )
    d = StreamPage(col)
    assert "youtube.com" in d.server.text()
    d.service.setCurrentIndex(d.service.findData("kick"))
    assert d.server.text() == "" and d.outputs[0].server == ""
    d.service.setCurrentIndex(d.service.findData("custom"))
    d.server.setText("rtmp://mine.example/live")
    d.server.textEdited.emit("")
    assert d.outputs[0].server == "rtmp://mine.example/live"
    d.service.setCurrentIndex(d.service.findData("kick"))
    assert d.outputs[0].server == ""


def test_a_place_signs_in_once_then_gets_its_key_from_the_account(fake_keyring, monkeypatch):
    app = QApplication.instance() or QApplication([])
    import time

    from sprigcast.app import signin_dialog
    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.output import service_signin, stream_details

    monkeypatch.setattr(service_signin, "available", lambda s: s in ("youtube", "kick"))
    outcome = {"fetched": service_signin.Fetched(account="Sloth Bloom", key="yt-key"), "error": None}
    signed_in = {"youtube": ""}
    monkeypatch.setattr(stream_details, "account", lambda s: signed_in.get(s, ""))

    class FakeDialog:
        def __init__(self, service, parent, **kw):
            self.fetched, self.error = outcome["fetched"], outcome["error"]
            if self.fetched is not None:
                signed_in[service] = self.fetched.account  # the one sign-in signs the platform in everywhere

        def exec(self):
            return self.fetched is not None

        def deleteLater(self):
            pass

    monkeypatch.setattr(signin_dialog, "SignInDialog", FakeDialog)
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id="yt", type="stream", service="youtube", canvas="horizontal", key_ref="sprigcast/yt")],
    )
    d = StreamPage(col)
    assert d.signin_state.text() == "Not signed in" and d.sign_in_button.text() == "Sign in"
    d._sign_in_clicked()
    assert d.signin_state.text() == "Signed in as Sloth Bloom" and d.sign_in_button.text() == "Get key"
    assert d.sign_out_button.isVisibleTo(d)  # signing out lives on the place now (no Accounts tab)
    assert d.key.text() == "yt-key" and d.apply_to(col) and col.outputs[0].account == "Sloth Bloom"
    d.key.setText("")
    monkeypatch.setattr(service_signin, "key_from_account",
                        lambda s, vertical=False: service_signin.Fetched(account="Sloth Bloom", key="yt-key-2"))
    d._sign_in_clicked()  # Get key: straight from the account, on a worker
    deadline = time.time() + 3
    while d.key.text() != "yt-key-2" and time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert d.key.text() == "yt-key-2"
    signed_in["youtube"] = ""
    outcome.update(fetched=None, error="YouTube said 403: Live streaming isn't on for this YouTube channel yet.")
    d._sign_in_clicked()
    assert d.signin_error.isVisibleTo(d) and "isn't on" in d.signin_error.text()
    d.service.setCurrentIndex(d.service.findData("kick"))  # the error and the account belong to the old service
    assert not d.signin_error.isVisibleTo(d) and d.outputs[0].account == ""


def test_keys_are_read_only_when_a_place_is_shown(fake_keyring, monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app import stream_dialog

    reads = []
    real = stream_dialog.get_key
    monkeypatch.setattr(stream_dialog, "get_key", lambda ref: reads.append(ref) or real(ref))
    fake_keyring[("SprigCast", "k/b")] = "bee"
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id=i, type="stream", service="custom", canvas="horizontal", key_ref=f"k/{i}") for i in "abc"],
    )
    d = stream_dialog.StreamPage(col)
    assert reads == ["k/a"]
    d.list.setCurrentRow(1)
    assert reads == ["k/a", "k/b"] and d.key.text() == "bee"
    d.list.setCurrentRow(2)
    d._remove()  # never shown before removing? still deleted from the store
    fake_keyring[("SprigCast", "k/c")] = "sea"
    d2 = stream_dialog.StreamPage(col)
    d2.list.setCurrentRow(2)
    d2.list.setCurrentRow(0)
    d2.outputs.pop(2)
    d2._saved_keys.setdefault("k/c", None)
    d2.keys["k/c"] = ""
    assert d2.apply_to(col) and fake_keyring.get(("SprigCast", "k/c"), "") == ""


def test_sign_out_on_a_place_signs_the_platform_out(fake_keyring, monkeypatch):
    QApplication.instance() or QApplication([])
    from sprigcast.app import accounts_page
    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.output import service_signin, stream_details

    monkeypatch.setattr(service_signin, "available", lambda s: s in ("youtube", "kick"))
    signed_in = {"kick": "slothbloom"}
    monkeypatch.setattr(stream_details, "account", lambda s: signed_in.get(s, ""))
    gone = []

    def sign_out(service):
        gone.append(service)
        signed_in[service] = ""
        return ""

    monkeypatch.setattr(accounts_page, "sign_out", sign_out)
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id="k", type="stream", service="kick", canvas="horizontal", key_ref="sprigcast/k")],
    )
    d = StreamPage(col)
    assert d.signin_state.text() == "Signed in as slothbloom" and d.sign_out_button.isVisibleTo(d)
    d.sign_out_button.click()
    assert gone == ["kick"] and d.signin_state.text() == "Not signed in"
    assert not d.sign_out_button.isVisibleTo(d) and d.sign_in_button.text() == "Sign in"


def test_twitch_place_shows_sign_out_once_connected(fake_keyring, monkeypatch):
    QApplication.instance() or QApplication([])
    from sprigcast.app import accounts_page
    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.output import twitch_auth

    login = {"name": "sloth"}
    monkeypatch.setattr(twitch_auth, "remembered_login", lambda: login["name"])
    monkeypatch.setattr(twitch_auth, "connected_account", lambda: login["name"] or None)
    monkeypatch.setattr(accounts_page, "sign_out", lambda s: login.update(name="") or "")
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id="t", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/t")],
    )
    d = StreamPage(col)
    assert d.twitch_sign_out.isVisibleTo(d) and not d.twitch_button.isVisibleTo(d)
    d.twitch_sign_out.click()
    assert not d.twitch_sign_out.isVisibleTo(d) and d.twitch_button.isVisibleTo(d)


def test_a_login_never_recorded_is_looked_up_off_the_window_thread_and_filled_in(fake_keyring, monkeypatch):
    """Opening the password store while the Stream page was being made held it 16.6 s on a cold build machine."""
    import threading
    import time

    app = QApplication.instance() or QApplication([])
    from sprigcast.app.stream_dialog import StreamPage
    from sprigcast.output import twitch_auth

    asked_on, recorded, gate = [], [], threading.Event()

    def slow_store():
        asked_on.append(threading.current_thread() is threading.main_thread())
        gate.wait(5)
        return "surakage"

    monkeypatch.setattr(twitch_auth, "remembered_login", lambda: recorded[-1] if recorded else None)
    monkeypatch.setattr(twitch_auth, "connected_account", slow_store)
    monkeypatch.setattr(twitch_auth, "_announce", recorded.append)
    col = SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        outputs=[Output(id="out-twitch", type="stream", service="twitch", canvas="horizontal")],
    )
    began = time.monotonic()
    d = StreamPage(col)  # made while the store is still busy
    d._show_service(d.outputs[0])  # shown again meanwhile: one lookup, not two
    assert time.monotonic() - began < 2 and not d.use_account.isVisibleTo(d)
    gate.set()
    end = time.monotonic() + 5
    while not recorded and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert asked_on == [False] and recorded == ["surakage"]
    assert d.twitch_label.text() == "Connected as surakage" and d.use_account.isVisibleTo(d)
    assert not d._twitch_poll.isActive()
