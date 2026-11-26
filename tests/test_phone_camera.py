"""The Phone Camera: a phone on the same Wi-Fi sends its camera to SprigCast through a page SprigCast serves
itself (app/phone_server.py), found by a QR code SprigCast draws itself (core/qr.py), over HTTPS with a
certificate SprigCast makes itself (core/selfsigned.py)."""

import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from sprigcast.app import phone_server
from sprigcast.core import qr, selfsigned


def _read_qr(modules):
    cv2 = pytest.importorskip("cv2")
    img = np.pad(np.where(np.array(modules, dtype=bool), 0, 255).astype(np.uint8), 4, constant_values=255)
    img = cv2.resize(img, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    return cv2.QRCodeDetector().detectAndDecode(img)[0]


@pytest.mark.parametrize("text", ["https://192.168.1.23:4466/phone/AbCdEf123456_-xy", "a", "x" * 60, "y" * 150])
def test_the_qr_code_reads_back(text):
    assert _read_qr(qr.encode(text)) == text


def test_the_certificate_serves_https(tmp_path):
    cert, key = selfsigned.make(["192.168.1.23"])
    (tmp_path / "c.pem").write_text(cert)
    (tmp_path / "k.pem").write_text(key)
    server_side = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_side.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def serve():
        conn, _ = listener.accept()
        with server_side.wrap_socket(conn, server_side=True) as s:
            s.sendall(b"ok")

    threading.Thread(target=serve, daemon=True).start()
    client = ssl.create_default_context()
    client.load_verify_locations(tmp_path / "c.pem")  # its own signature checks out
    client.check_hostname = False
    with client.wrap_socket(socket.create_connection(("127.0.0.1", port), timeout=5)) as s:
        assert s.recv(2) == b"ok"
    listener.close()


def test_only_home_addresses_are_served():
    assert phone_server.is_home("192.168.1.40") and phone_server.is_home("10.0.0.2") and phone_server.is_home("::1")
    assert not phone_server.is_home("8.8.8.8") and not phone_server.is_home("2001:4860::1")


@pytest.fixture
def server(tmp_path):
    srv = phone_server.PhoneServer(tmp_path)
    srv.use(["code1"])
    yield srv
    srv.use([])


def _get(url, data=None, key=None):
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET",
                                     headers={"X-Key": key} if key else {})
    with urllib.request.urlopen(request, context=context if url.startswith("https") else None, timeout=5) as r:
        return r.status, r.read()


def test_the_phone_and_the_leaf_meet_through_the_server(server):
    phone = f"https://127.0.0.1:{server.phone_port}/phone/code1"
    view = server.view_url("code1")
    assert _get(phone)[0] == 200 and b"getUserMedia" in _get(phone)[1]
    assert _get(view)[0] == 200 and b"RTCPeerConnection" in _get(view)[1]
    assert _get(view + "/offer?after=0")[0] == 204
    key = _pair(server)
    assert json.loads(_get(phone + "/offer", b"offer sdp", key)[1]) == {"id": 1}
    assert json.loads(_get(view + "/offer?after=0")[1]) == {"id": 1, "sdp": "offer sdp"}
    assert _get(view + "/offer?after=1")[0] == 204  # nothing newer
    assert _get(phone + "/answer?id=1", key=key)[0] == 204
    _get(view + "/answer?id=1", b"answer sdp")
    assert json.loads(_get(phone + "/answer?id=1", key=key)[1]) == {"sdp": "answer sdp"}
    assert not server.connected("code1")
    _get(phone + "/state", b"live", key)
    assert server.connected("code1")


def _pair(server, code="code1"):
    url = f"https://127.0.0.1:{server.phone_port}/phone/{code}/pair"
    return json.loads(_get(url, server.pin(code).encode())[1])["key"]


def _status(url, data=None, key=None):
    try:
        return _get(url, data, key)[0]
    except urllib.error.HTTPError as err:
        return err.code


def test_a_phone_needs_the_6_digit_code_before_it_sends(server, monkeypatch):
    """User 2026-11-26: after the QR code, the phone enters the code SprigCast shows. Without it the page loads but
    nothing else answers, and a wrong code is turned down."""
    monkeypatch.setattr(phone_server, "WRONG_S", 0.0)
    phone = f"https://127.0.0.1:{server.phone_port}/phone/code1"
    pin = server.pin("code1")
    assert len(pin) == 6 and pin.isdigit() and server.pin("code1") == pin
    assert _status(phone) == 200
    assert _status(phone + "/offer", b"sdp") == 401 and _status(phone + "/answer?id=1") == 401
    assert _status(phone + "/state", b"live") == 401 and not server.connected("code1")
    assert _status(phone + "/offer", b"sdp", "made-up") == 401
    wrong = f"{(int(pin) + 1) % 1_000_000:06d}"
    assert _status(phone + "/pair", wrong.encode()) == 403 and not server.paired("code1")
    key = _pair(server)
    assert server.paired("code1") and _status(phone + "/offer", b"sdp", key) == 200
    assert key not in (server.folder / "paired.json").read_text()  # only its hash is kept
    again = phone_server.PhoneServer(server.folder)  # the next start: the phone stays paired
    with again.lock:
        assert again.knows("code1", key) and not again.knows("code1", "other")
    server.forget("code1")
    assert _status(phone + "/offer", b"sdp", key) == 401


def test_too_many_wrong_codes_make_a_new_one(server, monkeypatch, caplog):
    monkeypatch.setattr(phone_server, "WRONG_S", 0.0)
    phone = f"https://127.0.0.1:{server.phone_port}/phone/code1/pair"
    pin = server.pin("code1")
    wrong = f"{(int(pin) + 1) % 1_000_000:06d}".encode()
    with caplog.at_level(logging.INFO, logger="sprigcast.app.phone_server"):
        for _ in range(phone_server.TRIES):
            assert _status(phone, wrong) == 403
    assert server.pin("code1") != pin
    assert _status(phone, server.pin("code1").encode()) == 429  # paused for a while, even with the new one
    assert "wrong pairing codes" in caplog.text and pin not in caplog.text
    server._paused.clear()
    assert _status(phone, server.pin("code1").encode()) == 200
    assert _status(phone, "١٢٣٤٥٦".encode()) == 403  # digits from other scripts are no code


def test_a_wrong_code_finds_nothing(server):
    with pytest.raises(urllib.error.HTTPError) as err:
        _get(f"https://127.0.0.1:{server.phone_port}/phone/guess")
    assert err.value.code == 404
    with pytest.raises(urllib.error.HTTPError):
        _get(f"http://127.0.0.1:{server.view_port}/phone/code1")  # the view port serves only the view


def test_the_server_runs_only_while_a_leaf_needs_it(tmp_path):
    srv = phone_server.PhoneServer(tmp_path)
    srv.use([])
    assert not srv.running
    srv.use(["a"])
    assert srv.running and (tmp_path / "phone.crt").is_file()
    srv.use([])
    assert not srv.running


def test_a_new_leaf_is_a_page_of_its_own(monkeypatch, tmp_path):
    from sprigcast.app import phone_camera
    from sprigcast.core.scene import SourceConfig

    a, b = phone_camera.new_settings(), phone_camera.new_settings(vertical=True)
    assert a["phone"] != b["phone"] and a["keep_running"] and a["audio_to_mixer"]
    assert (a["width"], a["height"]) == (1280, 720) and (b["width"], b["height"]) == (720, 1280)
    from sprigcast.audio.mixer import is_voice

    assert is_voice(SourceConfig(id="browser-1", kind="browser", name="Phone Camera", settings=a))  # Voice track
    monkeypatch.setattr(phone_camera, "_server", phone_server.PhoneServer(tmp_path))

    class Garden:
        sources = [SourceConfig(id="browser-1", kind="browser", name="Phone Camera",
                                settings={**a, "url": "http://127.0.0.1:1/view/" + a["phone"]})]

    try:
        assert phone_camera.sync(Garden) is True  # pointed at the port the view got
        assert Garden.sources[0].settings["url"] == phone_camera._server.view_url(a["phone"])
        assert phone_camera.sync(Garden) is False
    finally:
        Garden.sources = []
        phone_camera.sync(Garden)
    assert not phone_camera._server.running


def test_add_phone_camera_puts_in_a_phone_leaf_and_starts_its_page(tmp_path, monkeypatch):
    from sprigcast.app import phone_camera
    from test_add_menu import _window

    monkeypatch.setattr(phone_camera, "_server", phone_server.PhoneServer(tmp_path / "phone"))
    col, w = _window(tmp_path, monkeypatch)
    try:
        w.selected_scene_id = "a"
        w._add_blank("browser:phone", "Phone Camera")
        leaf = next(c for c in col.sources if c.name == "Phone Camera")
        assert leaf.kind == "browser" and leaf.settings["phone"] and w._look_kind(leaf) == "phone"
        assert phone_camera._server.running
        assert leaf.settings["url"] == phone_camera._server.view_url(leaf.settings["phone"])
        for scene in col.scenes:
            scene.items = [i for i in scene.items if i.source != leaf.id]
        col.sources.remove(leaf)
        w._changed()
        assert not phone_camera._server.running
    finally:
        w.close()
        phone_camera._server.use([])


def test_a_browser_stalled_at_the_handshake_holds_up_no_one(server):
    """The secure handshake runs on each request's own thread: a connection that sends nothing (a browser at its
    warning page) didn't stop the next phone getting its page."""
    stalled = socket.create_connection(("127.0.0.1", server.phone_port), timeout=5)
    try:
        assert _get(f"https://127.0.0.1:{server.phone_port}/phone/code1")[0] == 200
    finally:
        stalled.close()


def test_a_failed_request_is_logged(server, monkeypatch, caplog):
    """The app has no console: a request that fails says so in the log instead of closing with nothing said."""
    import logging

    from sprigcast.app import phone_server

    monkeypatch.setattr(phone_server, "PHONE_PAGE", None)  # .encode() fails
    with caplog.at_level(logging.ERROR, logger="sprigcast.app.phone_server"):
        with pytest.raises(Exception):
            _get(f"https://127.0.0.1:{server.phone_port}/phone/code1")
        deadline = time.monotonic() + 3
        while "phone page failed" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
    assert "a request to the phone page failed" in caplog.text
    assert "AttributeError" in caplog.text


def test_the_page_port_is_its_own(server):
    """While the phone page holds its port, nothing else can listen on it."""
    import sys

    other = socket.socket()
    try:
        if sys.platform != "win32":
            other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with pytest.raises(OSError):
            other.bind(("0.0.0.0", server.phone_port))
            other.listen()
    finally:
        other.close()


def test_the_page_serves_with_truststore_standing_in(tmp_path, monkeypatch):
    """On Windows main.py lets truststore stand in for ssl.SSLContext; its contexts check the other side's
    certificate after the handshake and close the connection when there's none, as with every phone (PC
    2026-11-26: the page closed with nothing sent). The phone's page keeps Python's own."""
    original = ssl.SSLContext

    class Truststore(original):  # what truststore.inject_into_ssl() puts in its place
        def wrap_socket(self, sock, *args, **kwargs):
            s = super().wrap_socket(sock, *args, **kwargs)
            s.close()
            raise ssl.SSLCertVerificationError("no certificate from the other side")

    Truststore.__module__ = "truststore._api"

    # In place in the ssl module itself, as inject_into_ssl() does: ssl.py's own property setters then went round
    # and round through it (minimum_version: a stack overflow on every start, PC 2026-11-26).
    monkeypatch.setattr(ssl, "SSLContext", Truststore)
    srv = phone_server.PhoneServer(tmp_path)
    srv.use(["code1"])
    monkeypatch.setattr(ssl, "SSLContext", original)  # the test's own client
    try:
        assert _get(f"https://127.0.0.1:{srv.phone_port}/phone/code1")[0] == 200
    finally:
        srv.use([])


def test_the_leaf_page_says_what_it_receives(server, caplog):
    """On the PC the leaf stayed black with no word why (2026-11-26): its page now reports what arrives (once
    per change, nothing personal), and asks for VP8 first, decoded the same way in every page engine."""
    import logging

    view = server.view_url("code1")
    assert b"setCodecPreferences" in _get(view)[1]
    report = {"connection": "connected", "codec": "video/VP8", "frames": 12, "width": 1280, "height": 720,
              "audio": "arriving", "player": "playing-4"}
    with caplog.at_level(logging.INFO, logger="sprigcast.app.phone_server"):
        _get(view + "/report", json.dumps(report).encode())
        _get(view + "/report", json.dumps(report).encode())  # the same again: not logged twice
        _get(view + "/report", b"not json")
    lines = [r.getMessage() for r in caplog.records if "on this PC" in r.getMessage()]
    assert lines == ["phone camera: on this PC: connection connected, video video/VP8, pictures arriving (1280x720), "
                     "sound arriving, player playing-4"]
    assert phone_server._report_line(json.dumps({"codec": "<script>x</script>", "frames": "lots"})).startswith(
        "connection -, video scriptx/script, no pictures yet")


def test_the_pop_up_shows_the_code_and_what_sprigcast_shows(tmp_path, monkeypatch):
    """User 2026-11-26: the code to enter, big, and the leaf's picture live, so what's sent is what's shown."""
    from PySide6.QtWidgets import QApplication

    from sprigcast.app import phone_camera

    app = QApplication.instance() or QApplication([])
    srv = phone_server.PhoneServer(tmp_path)
    monkeypatch.setattr(phone_camera, "_server", srv)
    monkeypatch.setattr(phone_server, "lan_address", lambda: "192.168.1.20")
    srv.use(["code1"])
    asked, stopped = [], []

    class Grab:
        def __call__(self, source_id, back, raw, limit):
            asked.append((source_id, raw))
            picture = np.zeros((90, 160, 4), np.uint8)
            picture[..., 1] = picture[..., 3] = 255
            back(picture)

        def stop(self):
            stopped.append(True)

    try:
        dialog = phone_camera.PhoneDialog({"phone": "code1", "width": 1280, "height": 720}, None,
                                          source_id="browser-1", grab=Grab())
        dialog.show()
        assert dialog.pin.text().replace(" ", "") == srv.pin("code1")
        assert not dialog.forget.isVisible()
        deadline = time.monotonic() + 3
        while dialog.view.image is None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        assert asked and asked[0] == ("browser-1", False)
        assert dialog.view.image is not None and dialog.view.image.pixelColor(80, 45).green() == 255
        pin = srv.pin("code1")
        with srv.lock:
            srv.pair("code1", pin)
        dialog._look()
        assert dialog.forget.isVisible()
        dialog.forget.click()
        assert not srv.paired("code1") and not dialog.forget.isVisible()
        dialog.accept()
        assert stopped
    finally:
        srv.use([])


def test_a_leaf_that_follows_the_phone_turns_with_it(server, monkeypatch):
    """User 2026-11-26: turned landscape, the leaf stayed 720x1280. The leaf's page reports the picture as it
    arrives; a leaf that follows the phone takes that shape and its places turn (the phone stays connected)."""
    from sprigcast.app import phone_camera
    from sprigcast.core.scene import SourceConfig

    monkeypatch.setattr(phone_camera, "_server", server)
    view = server.view_url("code1")
    leaf = SourceConfig(id="browser-1", kind="browser", name="Phone Camera",
                        settings={**phone_camera.new_settings(vertical=True), "phone": "code1"})
    fixed = SourceConfig(id="browser-2", kind="browser", name="Phone Camera",
                         settings={**phone_camera.new_settings(vertical=True), "phone": "code1",
                                   phone_camera.FOLLOW: False})

    class Garden:
        sources = [leaf, fixed]

    assert phone_camera.turned(Garden) == []  # nothing reported yet
    _get(view + "/report", json.dumps({"width": 720, "height": 1280}).encode())
    assert phone_camera.turned(Garden) == []  # already that shape
    _get(view + "/report", json.dumps({"width": 1280, "height": 720}).encode())
    assert server.video_size("code1") == (1280, 720)
    assert phone_camera.turned(Garden) == [(leaf, (720, 1280), (1280, 720))]
    assert (leaf.settings["width"], leaf.settings["height"]) == (1280, 720)
    assert (fixed.settings["width"], fixed.settings["height"]) == (720, 1280)  # set to Portrait: stays
    assert phone_camera.turned(Garden) == []
    _get(view + "/report", json.dumps({"width": "x"}).encode())  # nonsense changes nothing
    assert server.video_size("code1") == (1280, 720)


def test_the_phone_page_sends_the_mic_unless_turned_off():
    page = phone_server.PHONE_PAGE
    assert page.index('<option value="on">On</option>') < page.index('<option value="off">Off</option>')
    assert "sprigcast-mic" in page


def test_the_phones_mic_is_heard_from_its_stream():
    """PC 2026-11-26: the phone's mic arrived at the leaf's page but the mixer heard silence: Web Audio hears a
    WebRTC stream only through createMediaStreamSource (checked end to end in Chromium with the page sound script:
    0 blocks before, sound after)."""
    page = phone_server.VIEW_PAGE
    assert "createMediaStreamSource" in page and "v.__sprigcastPlain = true" in page and "v.muted = true" in page


def test_a_turned_phone_reshapes_without_waiting_for_the_source_report():
    from types import SimpleNamespace

    from sprigcast.app.main_window import MainWindow

    calls = []
    win = SimpleNamespace(_turn_phone_cameras=lambda: True, _changed=lambda: calls.append("changed"))
    MainWindow._phone_turn_tick(win)
    assert calls == ["changed"]
    win._turn_phone_cameras = lambda: False
    MainWindow._phone_turn_tick(win)
    assert calls == ["changed"]


def test_the_view_goes_blank_when_the_phone_stops_sending():
    """26r (2026-11-26): with the phone's screen off or its page closed, the leaf kept the last picture frozen.
    No new picture for STALE_MS hides it (checked in Chromium: hidden 3 s after a stream stopped)."""
    page = phone_server.VIEW_PAGE
    assert "STALE_MS = 2000" in page and 'v.style.visibility = stale ? "hidden" : "visible"' in page


def test_an_older_phone_camera_is_a_voice_too():
    """26r (2026-11-26): a Phone Camera added before new ones were marked a voice left the recording's voice track
    (named "Mic") silent."""
    from types import SimpleNamespace

    from sprigcast.app.ducking_dialog import is_voice as shown_voice
    from sprigcast.audio.mixer import is_voice, source_tracks

    old = SimpleNamespace(kind="browser", name="Phone Camera", settings={"phone": "abc", "audio_to_mixer": True})
    page = SimpleNamespace(kind="browser", name="Alerts", settings={})
    assert is_voice(old) and "voice" in source_tracks(old) and shown_voice(old.settings, old.kind)
    assert not is_voice(page) and "voice" not in source_tracks(page)
    old.settings["duck_voice"] = False  # switched off by hand: stays off
    assert not is_voice(old)


def test_a_cleared_picture_is_said_in_the_log():
    """26s (2026-11-26): the blank leaf worked, but nothing in the log said when, so the 2 s couldn't be timed."""
    import json

    assert "cleared" in phone_server._report_line(json.dumps({"cleared": True, "frames": 10}))
    assert "pictures arriving" in phone_server._report_line(json.dumps({"cleared": False, "frames": 10}))
    assert "if (stale !== cleared) { cleared = stale; report(); }" in phone_server.VIEW_PAGE
