"""The Phone Camera: a phone on the same Wi-Fi sends its camera to SprigCast through a page SprigCast serves
itself (app/phone_server.py), found by a QR code SprigCast draws itself (core/qr.py), over HTTPS with a
certificate SprigCast makes itself (core/selfsigned.py)."""

import json
import socket
import ssl
import threading
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


def _get(url, data=None):
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(request, context=context if url.startswith("https") else None, timeout=5) as r:
        return r.status, r.read()


def test_the_phone_and_the_leaf_meet_through_the_server(server):
    phone = f"https://127.0.0.1:{server.phone_port}/phone/code1"
    view = server.view_url("code1")
    assert _get(phone)[0] == 200 and b"getUserMedia" in _get(phone)[1]
    assert _get(view)[0] == 200 and b"RTCPeerConnection" in _get(view)[1]
    assert _get(view + "/offer?after=0")[0] == 204
    assert json.loads(_get(phone + "/offer", b"offer sdp")[1]) == {"id": 1}
    assert json.loads(_get(view + "/offer?after=0")[1]) == {"id": 1, "sdp": "offer sdp"}
    assert _get(view + "/offer?after=1")[0] == 204  # nothing newer
    assert _get(phone + "/answer?id=1")[0] == 204
    _get(view + "/answer?id=1", b"answer sdp")
    assert json.loads(_get(phone + "/answer?id=1")[1]) == {"sdp": "answer sdp"}
    assert not server.connected("code1")
    _get(phone + "/state", b"live")
    assert server.connected("code1")


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
