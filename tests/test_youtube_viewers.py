"""youtube.viewers (StreamBridge's Live count, 2026-11-26): from the last read, never waiting; read at most once a
minute while asked; Dual stream counted once; nothing read while not live on YouTube."""

from __future__ import annotations

import time

import pytest

from sprigcast.output import stream_details as sd
from sprigcast.output import youtube_viewers as yv


@pytest.fixture
def youtube(monkeypatch):
    calls = []
    keys = {"key-h": "secret-h", "key-v": "secret-v"}
    state = {"active": [{"id": "b1", "contentDetails": {"boundStreamId": "s1"}}], "viewers": {"b1": "7"}}

    def yt(token, method, path, body=None):
        calls.append(path.split("?")[0])
        if path.startswith("liveBroadcasts"):
            return {"items": state["active"]}
        if path.startswith("videos"):
            ids = path.split("id=")[1].split(",")
            return {"items": [{"id": i, "liveStreamingDetails": {"concurrentViewers": state["viewers"][i]}}
                              for i in ids if i in state["viewers"]]}
        raise AssertionError(path)

    monkeypatch.setattr(sd, "_yt", yt)
    monkeypatch.setattr(sd, "_youtube_streams_for_keys",
                        lambda token, *k: [{"id": {"secret-h": "s1", "secret-v": "s2"}[x]} for x in k])
    monkeypatch.setattr(sd, "_with_token", lambda service, call: call("token"))
    monkeypatch.setattr("sprigcast.output.keys.get_key", lambda ref: keys[ref])
    yv.forget()
    yield state, calls
    yv.forget()


def _settled(places):
    deadline = time.monotonic() + 3
    while yv._reading and time.monotonic() < deadline:
        time.sleep(0.01)
    return yv.viewers(places)


def test_not_live_on_youtube_reads_nothing(youtube):
    _state, calls = youtube
    assert yv.viewers([]) == {"live": False}
    assert calls == []


def test_the_count_comes_from_the_last_read(youtube):
    state, calls = youtube
    places = [("horizontal", "key-h"), ("vertical", "key-v")]
    first = yv.viewers(places)
    assert first["live"] is True and first["viewers"] is None  # never waits on YouTube
    got = _settled(places)
    # Dual stream: the vertical key's stream is bound to no broadcast of its own, so it's counted once
    assert got["viewers"] == 7 and got["broadcasts"] == [{"canvas": "horizontal", "viewers": 7}] and got["updatedAt"]
    reads = len(calls)
    state["viewers"]["b1"] = "9"
    assert yv.viewers(places)["viewers"] == 7 and len(calls) == reads  # not read again within the minute


def test_two_youtube_streams_add_up(youtube):
    state, _calls = youtube
    state["active"].append({"id": "b2", "contentDetails": {"boundStreamId": "s2"}})
    state["viewers"]["b2"] = "2"
    places = [("horizontal", "key-h"), ("vertical", "key-v")]
    yv.viewers(places)
    got = _settled(places)
    assert got["viewers"] == 9 and {b["canvas"]: b["viewers"] for b in got["broadcasts"]} == {"horizontal": 7, "vertical": 2}
    assert yv.viewers([]) == {"live": False}  # the stream ended


def test_not_signed_in_says_not_live(youtube, monkeypatch):
    def no(service, call):
        raise sd.NeedsSignIn("Sign in to YouTube to change your stream details.")

    monkeypatch.setattr(sd, "_with_token", no)
    places = [("horizontal", "key-h")]
    yv.viewers(places)
    assert _settled(places) == {"live": False}


def test_the_api_asks_only_about_youtube_places_sending_now(monkeypatch):
    from types import SimpleNamespace

    from sprigcast.control.native import NativeApi
    from sprigcast.output import youtube_viewers

    asked = []
    monkeypatch.setattr(youtube_viewers, "viewers", lambda places: asked.append(places) or {"live": False})
    outputs = [SimpleNamespace(id="o1", type="stream", service="youtube", canvas="horizontal", key_ref="k1"),
               SimpleNamespace(id="o2", type="stream", service="twitch", canvas="horizontal", key_ref="k2"),
               SimpleNamespace(id="o3", type="stream", service="youtube", canvas="vertical", key_ref="k3")]
    api = SimpleNamespace(col=SimpleNamespace(outputs=outputs),
                          host=SimpleNamespace(stream_places=lambda: {"o1": {}, "o2": {}}))
    assert NativeApi.youtube_viewers(api, {}) == {"live": False}
    assert asked == [[("horizontal", "k1")]]


# viewers.counts: YouTube, Twitch and Kick in one answer (user, 2026-11-26) --------------------------------


@pytest.fixture
def platforms(monkeypatch, youtube):
    from sprigcast.output import viewer_counts as vc

    reads = {"twitch": 0, "kick": 0}
    numbers = {"twitch": 5, "kick": 11}

    def reader(name):
        def read():
            reads[name] += 1
            if numbers[name] == "signed out":
                raise sd.NeedsSignIn("not signed in")
            return numbers[name]
        return read

    for name in ("twitch", "kick"):
        monkeypatch.setattr(vc._counts[name], "read", reader(name))
        vc._counts[name].forget()
    yield vc, reads, numbers
    for name in ("twitch", "kick"):
        vc._counts[name].forget()


def _counts_settled(vc, live_on):
    deadline = time.monotonic() + 3
    while (yv._reading or any(c.busy for c in vc._counts.values())) and time.monotonic() < deadline:
        time.sleep(0.01)
    return vc.counts(live_on)


def test_counts_for_each_platform_live_now(platforms):
    vc, reads, _numbers = platforms
    live_on = {"youtube": [("horizontal", "key-h")], "twitch": [("horizontal", None)]}
    first = vc.counts(live_on)["platforms"]
    assert first["twitch"] == {"live": True, "viewers": None} and first["kick"] == {"live": False}
    got = _counts_settled(vc, live_on)
    assert got["platforms"]["youtube"]["viewers"] == 7 and got["platforms"]["twitch"] == {"live": True, "viewers": 5}
    assert got["platforms"]["kick"] == {"live": False} and reads["kick"] == 0 and got["updatedAt"]
    vc.counts(live_on)
    assert reads["twitch"] == 1  # not again within the minute


def test_a_platform_without_a_sign_in_says_so(platforms):
    vc, _reads, numbers = platforms
    numbers["kick"] = "signed out"
    live_on = {"kick": [("vertical", None)]}
    vc.counts(live_on)
    assert _counts_settled(vc, live_on)["platforms"]["kick"] == {"live": True, "viewers": None, "signedIn": False}


def test_the_api_groups_places_sending_now_by_platform(monkeypatch):
    from types import SimpleNamespace

    from sprigcast.control.native import NativeApi
    from sprigcast.output import viewer_counts

    asked = []
    monkeypatch.setattr(viewer_counts, "counts", lambda live_on: asked.append(live_on) or {"platforms": {}})
    outputs = [SimpleNamespace(id="o1", type="stream", service="youtube", canvas="horizontal", key_ref="k1"),
               SimpleNamespace(id="o2", type="stream", service="twitch", canvas="horizontal", key_ref="k2"),
               SimpleNamespace(id="o3", type="stream", service="kick", canvas="vertical", key_ref="k3")]
    api = SimpleNamespace(col=SimpleNamespace(outputs=outputs),
                          host=SimpleNamespace(stream_places=lambda: {"o1": {}, "o2": {}}))
    NativeApi.viewers_counts(api, {})
    assert asked == [{"youtube": [("horizontal", "k1")], "twitch": [("horizontal", "k2")]}]
