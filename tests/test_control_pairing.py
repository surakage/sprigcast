"""Pair a device (a short code instead of the password) and the password kept encrypted (PC test 2026-11-15:
the Stream Deck plugin was refused twice for a wrong password, and the password sat in the registry as plain text)."""

import json

from test_control_api import FakeHost, _collection

from sprigcast.control import secret_store, web
from sprigcast.control.native import NativeApi, NativeSession
from sprigcast.control.pairing import CODE_SECONDS, LOCK_SECONDS, MOST_TRIES, TRY_WINDOW, Pairing, matches
from sprigcast.control.protocol import Close, auth_string


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _api():
    return NativeApi(FakeHost(_collection()))


def test_a_code_works_once_for_two_minutes():
    clock, saved = Clock(), []
    pairing = Pairing(clock=clock, on_change=lambda: saved.append(1))
    code = pairing.start()
    assert len(code) == 6 and code.isdigit() and pairing.seconds_left() == CODE_SECONDS
    device = pairing.redeem(f"{code[:3]} {code[3:]}", "  Stream   Deck ")  # spaces are fine
    assert device.name == "Stream Deck" and device.key.startswith("sc_") and saved == [1]
    assert pairing.keys() == [device.key] and pairing.redeem(code, "Again") is None  # used up
    code = pairing.start()
    clock.now += CODE_SECONDS + 1
    assert pairing.redeem(code, "Late") is None and pairing.seconds_left() == 0


def test_wrong_codes_lock_pairing_for_a_minute():
    """Soak 2026-11-16: about 100 guesses a second were answered. Five wrong codes in a minute lock pairing."""
    clock = Clock()
    pairing = Pairing(clock=clock)
    code = pairing.start()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MOST_TRIES - 1):
        assert pairing.redeem(wrong) is None
    assert not pairing.locked_seconds() and pairing.code == code  # four: still fine
    assert pairing.redeem(wrong) is None
    assert pairing.locked_seconds() == LOCK_SECONDS and pairing.code == ""  # five: locked, code gone
    code = pairing.start()
    assert pairing.redeem(code) is None  # even the right code is refused unread while locked
    for _ in range(1000):
        pairing.redeem(wrong)  # hammering doesn't make it longer or count
    assert pairing.locked_seconds() == LOCK_SECONDS
    clock.now += LOCK_SECONDS
    assert not pairing.locked_seconds() and pairing.redeem(code, "Deck") is not None


def test_slow_wrong_tries_and_tries_with_no_code_showing():
    clock = Clock()
    pairing = Pairing(clock=clock)
    for _ in range(MOST_TRIES * 2):  # one a minute: never locks
        assert pairing.redeem("123456") is None
        clock.now += TRY_WINDOW
    assert not pairing.locked_seconds()
    for _ in range(MOST_TRIES):  # no code showing still counts
        pairing.redeem("123456")
    assert pairing.locked_seconds()


def test_devices_save_and_remove():
    pairing = Pairing(clock=Clock())
    pairing.start()
    first = pairing.redeem(pairing.code, "Stream Deck")
    pairing.start()
    second = pairing.redeem(pairing.code, "Streamer.bot")
    again = Pairing(Pairing.devices_from_json(pairing.to_json()))
    assert [d.name for d in again.devices] == ["Stream Deck", "Streamer.bot"]
    assert again.remove(first.id) and again.keys() == [second.key] and not again.remove("nope")
    assert Pairing.devices_from_json("not json") == []
    assert matches(second.key, ["", "x", second.key]) and not matches("sc_guess", [second.key])


def test_the_api_pairs_then_the_key_works_like_the_password():
    pairing = Pairing(clock=Clock())
    code = pairing.start()
    session = NativeSession(api=_api(), password="leafy", pairing=pairing)
    out = list(session.receive(json.dumps({"pair": code, "name": "Stream Deck", "events": "none"})))
    key = json.loads(out[0])["paired"]["key"]
    assert json.loads(out[1])["welcome"]["events"] == [] and session.welcomed and session.key == key
    # Next time: the key answers the challenge just like the password does.
    later = NativeSession(api=_api(), password="leafy", keys=pairing.keys(), pairing=pairing)
    out = list(later.receive(json.dumps({"auth": auth_string(key, later.salt, later.challenge)})))
    assert "welcome" in json.loads(out[0]) and later.key == key
    # A wrong or used code closes the connection.
    out = list(NativeSession(api=_api(), password="leafy", pairing=pairing).receive(json.dumps({"pair": code})))
    assert isinstance(out[0], Close) and out[0].code == 4011
    out = list(NativeSession(api=_api(), password="leafy", keys=[]).receive(
        json.dumps({"auth": auth_string(key, "s", "c")})))
    assert isinstance(out[0], Close) and out[0].code == 4009


def _request(text):
    return web.parse(text.encode())


def test_web_links_pair_even_while_off_and_take_the_key():
    api, pairing = _api(), Pairing(clock=Clock())
    code = pairing.start()
    status, body = web.handle(api, _request(f"GET /api/pair?code={code}&name=Streamer.bot HTTP/1.1\r\n\r\n"),
                              enabled=False, password="leafy", pairing=pairing)
    assert status == 200 and body["result"]["name"] == "Streamer.bot"
    key = body["result"]["key"]
    status, _ = web.handle(api, _request(f"GET /api/pair?code={code} HTTP/1.1\r\n\r\n"), enabled=False,
                           password="leafy", pairing=pairing)
    assert status == 401  # once only
    for _ in range(MOST_TRIES):
        status, body = web.handle(api, _request("GET /api/pair?code=000000 HTTP/1.1\r\n\r\n"), enabled=False,
                                  password="leafy", pairing=pairing)
    assert status == 429 and "try again" in body["error"]["message"]
    status, body = web.handle(api, _request(f"GET /api/app.info HTTP/1.1\r\nAuthorization: Bearer {key}\r\n\r\n"),
                              enabled=True, password="leafy", keys=pairing.keys(), pairing=pairing)
    assert status == 200 and body["ok"]


def test_secrets_are_encrypted_and_old_plain_ones_moved_over(monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import control_server

    # Off Windows they stay as they are; a value encrypted elsewhere can't be read.
    if secret_store.available():  # Windows: encrypted (differently each time, DPAPI salts it) and read back
        saved = secret_store.protect("leafy")
        assert saved.startswith("dpapi:") and "leafy" not in saved and secret_store.unprotect(saved) == "leafy"
    else:
        assert secret_store.protect("leafy") == "leafy"
    if not secret_store.available():
        assert secret_store.unprotect("dpapi:AAAA") == "" and secret_store.unprotect("leafy") == "leafy"
    # As on Windows: a plain password from an older SprigCast is encrypted on first read.
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "protect", lambda v: "dpapi:" + v[::-1])
    monkeypatch.setattr(secret_store, "unprotect", lambda v: v[6:][::-1] if v.startswith("dpapi:") else v)
    key = "control/test_secret"
    q = QSettings()
    q.setValue(key, "leafy")
    try:
        assert control_server._secret(key) == "leafy"
        assert q.value(key) == "dpapi:yfael"  # moved over
        assert control_server._secret(key) == "leafy"
        q.remove(key)
        made = control_server._secret(key, lambda: "fresh")
        assert made == "fresh" and q.value(key) == "dpapi:hserf"
    finally:
        q.remove(key)


def test_paired_devices_survive_a_restart(monkeypatch):
    """Pair, close SprigCast, open it again: the same key still gets in, and the device is still listed
    (PC test 2026-11-16: the key got "Wrong password" after a restart)."""
    from PySide6.QtCore import QSettings

    from sprigcast.app import control_server
    from test_control_api import _api as _obs_api, _qt

    _qt()
    monkeypatch.setattr(secret_store, "available", lambda: True)  # as on Windows: saved encrypted
    monkeypatch.setattr(secret_store, "protect", lambda v: "dpapi:" + v[::-1] if v else v)
    monkeypatch.setattr(secret_store, "unprotect", lambda v: v[6:][::-1] if v.startswith("dpapi:") else v)
    q = QSettings()
    before = {k: q.value(k) for k in (control_server.DEVICES, control_server.DEVICES + "_unreadable")}
    q.remove(control_server.DEVICES)
    try:
        first = control_server.ControlServer(_obs_api()[0])
        code = first.pairing.start()
        session = NativeSession(api=first.native, password="leafy", keys=first.keys(), pairing=first.pairing)
        key = json.loads(list(session.receive(json.dumps({"pair": code, "name": "Stream Deck"})))[0])["paired"]["key"]
        assert str(QSettings().value(control_server.DEVICES)).startswith("dpapi:")  # on disk now, encrypted
        first.deleteLater()
        again = control_server.ControlServer(_obs_api()[0])  # SprigCast opened again
        assert [d.name for d in again.pairing.devices] == ["Stream Deck"] and key in again.keys()
        later = NativeSession(api=again.native, password="leafy", keys=again.keys(), pairing=again.pairing)
        out = list(later.receive(json.dumps({"auth": auth_string(key, later.salt, later.challenge)})))
        assert "welcome" in json.loads(out[0]) and later.key == key
        # A saved list this PC can't read is kept aside, never written over by the next pairing.
        QSettings().setValue(control_server.DEVICES, "dpapi-unreadable")
        monkeypatch.setattr(secret_store, "unprotect", lambda v: "" if v == "dpapi-unreadable" else v)
        monkeypatch.setattr(secret_store, "is_protected", lambda v: v.startswith("dpapi"))
        third = control_server.ControlServer(_obs_api()[0])
        assert third.pairing.devices == []
        assert QSettings().value(control_server.DEVICES + "_unreadable") == "dpapi-unreadable"
    finally:
        for k, v in before.items():
            if v is None:
                q.remove(k)
            else:
                q.setValue(k, v)


def _talk(port: int, first: dict | None, answer, app) -> list[dict]:
    """Connect to SprigCast's API over a real socket, answer its hello with answer(hello), and collect what it
    says (and how it closed) until it closes or 3 seconds pass."""
    from PySide6.QtCore import QEventLoop, QTimer, QUrl
    from PySide6.QtWebSockets import QWebSocket

    said: list[dict] = []
    loop = QEventLoop()
    ws = QWebSocket()

    def got(text):
        message = json.loads(text)
        said.append(message)
        if "hello" in message:
            ws.sendTextMessage(json.dumps(first if first is not None else answer(message["hello"])))
        elif "welcome" in message:
            loop.quit()

    ws.textMessageReceived.connect(got)
    ws.disconnected.connect(lambda: (said.append({"closed": ws.closeCode().value}), loop.quit()))
    ws.open(QUrl(f"ws://127.0.0.1:{port}/sprigcast"))
    QTimer.singleShot(3000, loop.quit)
    loop.exec()
    ws.close()
    return said


def test_a_paired_key_gets_in_over_the_network_after_a_restart(monkeypatch, caplog):
    """The whole way, as on the PC: pair over a real socket, stop SprigCast's API and make a new one from what was
    saved (a restart), then connect with the key. Closing those connections leaves no Qt warning (PC log: "wildcard
    call disconnects from destroyed signal of QTcpSocket" a minute after each start)."""
    from PySide6.QtCore import QEventLoop, QSettings, QTimer, qInstallMessageHandler
    from PySide6.QtNetwork import QHostAddress, QTcpServer

    from sprigcast.app import control_server
    from test_control_api import _api as _obs_api, _qt

    app = _qt()
    caplog.set_level("INFO", logger="sprigcast")
    monkeypatch.setattr(secret_store, "available", lambda: True)
    monkeypatch.setattr(secret_store, "protect", lambda v: "dpapi:" + v[::-1] if v else v)
    monkeypatch.setattr(secret_store, "unprotect", lambda v: v[6:][::-1] if v.startswith("dpapi:") else v)
    q = QSettings()
    before = q.value(control_server.DEVICES)
    q.remove(control_server.DEVICES)
    qt_said: list[str] = []
    qInstallMessageHandler(lambda _kind, _where, text: qt_said.append(text))
    probe = QTcpServer()
    probe.listen(QHostAddress.SpecialAddress.LocalHost, 0)
    port = probe.serverPort()
    probe.close()
    try:
        first = control_server.ControlServer(_obs_api()[0])
        assert first.start(port, "leafy")
        said = _talk(port, {"pair": first.pairing.start(), "name": "Stream Deck"}, None, app)
        key = next(m["paired"]["key"] for m in said if "paired" in m)
        first.stop()
        first.deleteLater()
        again = control_server.ControlServer(_obs_api()[0])  # SprigCast opened again
        assert again.start(port, "leafy")
        said = _talk(port, None, lambda h: {"auth": auth_string(key, h["auth"]["salt"], h["auth"]["challenge"])}, app)
        assert any("welcome" in m for m in said), said
        wrong = _talk(port, None, lambda h: {"auth": auth_string("nope", h["auth"]["salt"], h["auth"]["challenge"])}, app)
        assert {"closed": 4009} in wrong
        refused = [r.getMessage() for r in caplog.records if "Wrong password" in r.getMessage()]
        assert refused and "SprigCast API app" in refused[-1], refused  # every refusal says which door and app
        wait = QEventLoop()
        QTimer.singleShot(300, wait.quit)  # the closed connections let go of their sockets
        wait.exec()
        again.stop()
        assert not [m for m in qt_said if "wildcard" in m], qt_said
    finally:
        qInstallMessageHandler(None)
        if before is None:
            q.remove(control_server.DEVICES)
        else:
            q.setValue(control_server.DEVICES, before)


def _key_works(server, key) -> bool:
    session = NativeSession(api=server.native, password="leafy", keys=server.keys(), pairing=server.pairing)
    out = list(session.receive(json.dumps({"auth": auth_string(key, session.salt, session.challenge)})))
    return "welcome" in json.loads(out[0]) if isinstance(out[0], str) else False


def _pair(server, name) -> str:
    code = server.pairing.start()
    session = NativeSession(api=server.native, password="leafy", keys=server.keys(), pairing=server.pairing)
    return json.loads(list(session.receive(json.dumps({"pair": code, "name": name})))[0])["paired"]["key"]


def test_two_copies_with_the_same_settings_keep_each_others_pairings(monkeypatch):
    """PC test 2026-11-16: StreamBridge showed as paired, yet after the update only the test harness's device loaded,
    and its key was refused. Two SprigCasts on the same settings (the app and a test tool) each saved their whole
    own list on every change, so the last one to save wiped the other's devices. Now each merges with the saved
    list: devices paired in either survive a restart, and a removal in one reaches the other."""
    import base64

    from PySide6.QtCore import QCoreApplication, QSettings

    from sprigcast.app import control_server
    from test_control_api import _api as _obs_api, _qt

    _qt()
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("TwoCopies")
    QSettings().clear()
    monkeypatch.setattr(secret_store, "available", lambda: True)  # as on Windows: "dpapi:<base64>"
    monkeypatch.setattr(secret_store, "protect", lambda v: "dpapi:" + base64.b64encode(v.encode()).decode() if v else v)
    monkeypatch.setattr(secret_store, "unprotect",
                        lambda v: base64.b64decode(v[6:]).decode() if v.startswith("dpapi:") else v)
    try:
        app_copy = control_server.ControlServer(_obs_api()[0])
        tool_copy = control_server.ControlServer(_obs_api()[0])  # started at the same time, same settings
        harness = _pair(tool_copy, "Soak test harness")
        bridge = _pair(app_copy, "THSV StreamBridge")  # this save used to drop the harness
        deck = _pair(app_copy, "Stream Deck")
        assert _key_works(tool_copy, bridge) and _key_works(app_copy, harness)  # each picks up the other's
        app_copy.keep_devices()  # Settings → Save
        tool_copy.stop()
        app_copy.stop()  # closing (the update): neither writes the other's devices away
        again = control_server.ControlServer(_obs_api()[0])
        assert sorted(d.name for d in again.pairing.devices) == ["Soak test harness", "Stream Deck", "THSV StreamBridge"]
        assert all(_key_works(again, key) for key in (harness, bridge, deck))
        # Removed in one copy: the other copy lets it go too, and doesn't save it back.
        other = control_server.ControlServer(_obs_api()[0])
        gone = next(d.id for d in again.pairing.devices if d.name == "Stream Deck")
        assert again.pairing.remove(gone)
        assert not _key_works(other, deck)
        other.keep_devices()
        assert sorted(d.name for d in control_server.ControlServer(_obs_api()[0]).pairing.devices) == [
            "Soak test harness", "THSV StreamBridge"]
    finally:
        QSettings().clear()


def test_not_inside_a_store_app_package_off_windows():
    """main.inside_package: "" unless Windows runs SprigCast inside another app's package (then its saves vanish)."""
    from sprigcast.app.main import inside_package

    assert inside_package() == ""


def test_paired_devices_and_secrets_are_read_only_from_sprigcasts_own_settings(monkeypatch):
    """Qt also reads HKLM and Slothbloom's OrganizationDefaults for anything missing; an old device list or
    password left there must never come back (PC 2026-11-18)."""
    from pathlib import Path

    from sprigcast.app import control_server

    q = control_server._own_settings()
    assert not q.fallbacksEnabled()
    src = Path(control_server.__file__).read_text(encoding="utf-8")
    assert "QSettings().value(DEVICES" not in src


def test_last_seen_is_kept_when_a_key_signs_in_or_leaves_without_writing_each_time():
    """Settings shows "Last seen 2 min ago" (2026-11-19): each device keeps when its key last signed in or left.
    Saving happens at most once a minute per device, since web links sign in on every button press."""
    from sprigcast.control.pairing import SEEN_SAVE_SECONDS, Device

    clock, wall, saves = Clock(), [1_763_566_190.0], []
    pairing = Pairing(clock=clock, wall=lambda: wall[0])
    pairing.on_seen = lambda: saves.append(1)
    device = pairing.redeem(pairing.start(), "StreamBridge")
    assert device.last_seen == "2025-11-19T15:29:50Z"  # paired counts as seen
    wall[0] += 30
    clock.now += 30
    assert pairing.seen(device.key) is device and device.last_seen == "2025-11-19T15:30:20Z"
    assert saves == []  # paired 30 s ago: that save is recent enough
    clock.now += SEEN_SAVE_SECONDS
    wall[0] += SEEN_SAVE_SECONDS
    pairing.seen(device.key)
    pairing.seen(device.key)
    assert saves == [1] and device.last_seen == "2025-11-19T15:31:20Z"
    assert pairing.seen("leafy") is None and pairing.seen("") is None  # the password isn't a device
    # Saved and read back; lists saved before last_seen read as never seen.
    again = Pairing.devices_from_json(pairing.to_json())
    assert again[0].last_seen == "2025-11-19T15:31:20Z"
    assert Pairing.devices_from_json(json.dumps([{"id": "a", "name": "Old", "key": "k", "added": ""}]))[0].last_seen == ""
    # A merge with another copy's list keeps the newer time, whichever had it.
    newer = Device(id=device.id, name="StreamBridge", key=device.key, added=device.added, last_seen="2025-11-19T16:00:00Z")
    assert pairing.merged([newer], {device.id})[0].last_seen == "2025-11-19T16:00:00Z"


def test_web_links_and_both_doors_mark_the_device_seen(monkeypatch):
    from sprigcast.app import control_server
    from sprigcast.control.protocol import Session
    from test_control_api import _api as _obs_api, _qt

    api, pairing = _api(), Pairing(clock=Clock(), wall=lambda: 1_763_566_190.0)
    device = pairing.redeem(pairing.start(), "Streamer.bot")
    device.last_seen = ""
    status, _ = web.handle(api, _request(f"GET /api/app.info HTTP/1.1\r\nAuthorization: Bearer {device.key}\r\n\r\n"),
                           enabled=True, password="leafy", keys=pairing.keys(), pairing=pairing)
    assert status == 200 and device.last_seen == "2025-11-19T15:29:50Z"

    _qt()
    server = control_server.ControlServer(_obs_api()[0])
    monkeypatch.setattr(server, "keep_devices", lambda: None)
    marks = []
    monkeypatch.setattr(server.pairing, "seen", lambda key: marks.append(key))
    native = NativeSession(api=server.native, password="leafy", keys=[device.key])
    list(native.receive(json.dumps({"auth": auth_string(device.key, native.salt, native.challenge)})))
    server._mark_seen(native)
    server._mark_seen(native)  # later messages on the same connection don't count again
    obs = Session(handler=lambda *a: None, password="leafy", keys=[device.key])
    list(obs.receive(json.dumps({"op": 1, "d": {"rpcVersion": 1,
                                                 "authentication": auth_string(device.key, obs.salt, obs.challenge)}})))
    assert obs.identified and obs.key == device.key
    server._mark_seen(obs)
    server._mark_seen(native, leaving=True)
    by_password = NativeSession(api=server.native, password="leafy")
    list(by_password.receive(json.dumps({"auth": auth_string("leafy", by_password.salt, by_password.challenge)})))
    server._mark_seen(by_password)
    assert marks == [device.key] * 3
    server.deleteLater()
