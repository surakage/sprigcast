"""Before going live each place's server is tried, without the key (output/preflight.py)."""

from __future__ import annotations

import socket
import time
from types import SimpleNamespace as NS

from sprigcast.output import preflight


def test_addresses_are_the_host_and_port_only():
    assert preflight.address("rtmp://a.rtmp.youtube.com/live2") == ("a.rtmp.youtube.com", 1935)
    assert preflight.address("rtmps://live-api-s.facebook.com:443/rtmp/") == ("live-api-s.facebook.com", 443)
    assert preflight.address("rtmp://ingest.example:1936/app/SECRET") == ("ingest.example", 1936)
    assert preflight.address("/tmp/out.flv") is None and preflight.address("") is None
    assert preflight.address("srt://host:9000") == ("host", 9000) and preflight.address("srt://host") is None


def test_a_server_that_listens_is_reached_and_nothing_is_sent_to_it():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        assert preflight.reach("127.0.0.1", port, 1.0) == ""
        conn, _ = server.accept()
        conn.settimeout(1.0)
        assert conn.recv(16) == b""  # connected and closed: no handshake, no key
        conn.close()
    finally:
        server.close()
    assert preflight.reach("127.0.0.1", port, 1.0) == "refused the connection"
    assert preflight.reach("no-such-host.invalid", 1935, 1.0) == "can't be found by name"


def test_places_that_would_fail_are_named_without_their_keys_and_the_check_never_takes_long():
    tried = []

    def try_one(host, port, timeout):
        tried.append((host, port))
        if host == "slow.example":
            time.sleep(5)
        return {"down.example": "refused the connection"}.get(host, "")

    targets = [NS(name="YouTube (horizontal)", server="rtmp://down.example/live2", key="SECRET-1"),
               NS(name="YouTube (vertical)", server="rtmp://down.example/live2", key="SECRET-2"),
               NS(name="Kick (horizontal)", server="rtmps://fine.example/app", key="SECRET-3"),
               NS(name="Custom", server="rtmp://slow.example/app", key="SECRET-4"),
               NS(name="Twitch (horizontal)", server="rtmp://live.twitch.tv/app", key="SECRET-5",
                  enhanced=NS(url="rtmps://ingest.example/app/SECRET-6")),
               NS(name="File", server="/tmp/somewhere", key="x"), object()]
    began = time.monotonic()
    said = preflight.check_places(targets, each_s=0.2, all_s=0.5, try_one=try_one)
    assert time.monotonic() - began < 1.5
    assert sorted(tried) == [("down.example", 1935), ("fine.example", 443), ("ingest.example", 443),
                             ("slow.example", 1935)]  # each server once, Twitch at the address it really sends to
    assert said == [("YouTube (horizontal) and YouTube (vertical) would not connect: down.example refused the "
                     "connection."),
                    "Custom would not connect: slow.example didn't answer."]
    assert "SECRET" not in " ".join(said)
    assert preflight.check_places(targets[2:3], try_one=try_one) == [] and preflight.check_places([]) == []
    offline = preflight.check_places(targets[:3], try_one=lambda h, p, t: "can't be found by name")
    assert offline == ["This PC can't reach the internet right now, so no place would connect."]
