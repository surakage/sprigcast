"""Pair a device (a short code instead of the password) and the password kept encrypted (PC test 2026-11-15:
the Stream Deck plugin was refused twice for a wrong password, and the password sat in the registry as plain text)."""

import json

from test_control_api import FakeHost, _collection

from sprigcast.control import secret_store, web
from sprigcast.control.native import NativeApi, NativeSession
from sprigcast.control.pairing import CODE_SECONDS, MOST_TRIES, Pairing, matches
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


def test_wrong_codes_cancel_it():
    pairing = Pairing(clock=Clock())
    code = pairing.start()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MOST_TRIES):
        assert pairing.redeem(wrong) is None
    assert pairing.redeem(code) is None  # no guessing: the code is gone


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
    status, body = web.handle(api, _request(f"GET /api/app.info HTTP/1.1\r\nAuthorization: Bearer {key}\r\n\r\n"),
                              enabled=True, password="leafy", keys=pairing.keys(), pairing=pairing)
    assert status == 200 and body["ok"]


def test_secrets_are_encrypted_and_old_plain_ones_moved_over(monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import control_server

    # Off Windows they stay as they are; a value encrypted elsewhere can't be read.
    assert secret_store.protect("leafy") == ("leafy" if not secret_store.available() else secret_store.protect("leafy"))
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
