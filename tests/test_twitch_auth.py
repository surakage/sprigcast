import json
import time

import pytest

from sprigcast.output import twitch_auth as t


@pytest.fixture
def store(monkeypatch):
    import keyring

    data = {}
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: data.get((svc, ref)))
    monkeypatch.setattr(keyring, "set_password", lambda svc, ref, v: data.__setitem__((svc, ref), v))
    monkeypatch.setattr(keyring, "delete_password", lambda svc, ref: data.pop((svc, ref), None))
    return data


class FakeTwitch:
    """Answers the few Twitch endpoints SprigCast uses."""

    def __init__(self):
        self.approved = False
        self.issued = 0
        self.valid = set()
        self.calls = []

    def post(self, url, fields):
        self.calls.append((url, dict(fields)))
        if url == t.DEVICE_URL:
            assert fields["scopes"] == "channel:read:stream_key"
            return {"device_code": "dev", "user_code": "ABCDEFGH", "verification_uri": "https://www.twitch.tv/activate?device-code=ABCDEFGH", "interval": 5, "expires_in": 1800}
        if fields.get("grant_type", "").endswith("device_code"):
            if not self.approved:
                return {"error": {"status": 400, "message": "authorization_pending"}}
            return self._tokens()
        if fields.get("grant_type") == "refresh_token":
            assert "client_secret" not in fields  # public client
            return self._tokens()
        raise AssertionError(url)

    def _tokens(self):
        self.issued += 1
        token = f"tok{self.issued}"
        self.valid = {token}
        return {"access_token": token, "refresh_token": f"ref{self.issued}", "expires_in": 14000}

    def get(self, path, token, cid):
        if token not in self.valid:
            raise PermissionError("token rejected")
        if path == "/users":
            return {"data": [{"id": "1234", "login": "surakage"}]}
        assert path == "/streams/key?broadcaster_id=1234"
        return {"data": [{"stream_key": "live_1234_secret"}]}


@pytest.fixture
def twitch(monkeypatch, store):
    fake = FakeTwitch()
    monkeypatch.setattr(t, "_post", fake.post)
    monkeypatch.setattr(t, "_get", fake.get)
    return fake


def test_device_sign_in_then_fetch_key(twitch, store):
    code = t.start_device_flow("cid")
    assert code.user_code == "ABCDEFGH" and code.interval == 5
    with pytest.raises(t.Pending):
        t.poll_device_flow(code, "cid")
    twitch.approved = True
    account = t.poll_device_flow(code, "cid")
    assert account["login"] == "surakage" and t.connected_account() == "surakage"
    saved = json.loads(store[("SprigCast", t.TOKEN_REF)])
    assert saved["user_id"] == "1234" and "live_1234_secret" not in json.dumps(saved)
    assert t.fetch_stream_key("cid") == "live_1234_secret"


def test_expired_or_revoked_token_is_refreshed(twitch, store):
    twitch.approved = True
    t.poll_device_flow(t.start_device_flow("cid"), "cid")
    tokens = json.loads(store[("SprigCast", t.TOKEN_REF)])
    tokens["expires_at"] = time.time() - 1  # expired
    store[("SprigCast", t.TOKEN_REF)] = json.dumps(tokens)
    assert t.fetch_stream_key("cid") == "live_1234_secret"
    assert json.loads(store[("SprigCast", t.TOKEN_REF)])["refresh_token"] == "ref2"
    twitch.valid = set()  # revoked server-side before its expiry
    assert t.fetch_stream_key("cid") == "live_1234_secret"
    assert twitch.issued == 3


def test_disconnect_and_errors(twitch, store):
    twitch.approved = True
    t.poll_device_flow(t.start_device_flow("cid"), "cid")
    t.disconnect()
    assert t.connected_account() is None
    with pytest.raises(t.TwitchAuthError, match="No Twitch account"):
        t.fetch_stream_key("cid")
    code = t.start_device_flow("cid")
    code.expires_at = time.time() - 1
    with pytest.raises(t.TwitchAuthError, match="expired"):
        t.poll_device_flow(code, "cid")


def test_shipped_client_id():
    assert len(t.DEFAULT_CLIENT_ID) == 30
