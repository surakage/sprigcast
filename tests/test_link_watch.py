"""A quiet stream's server is asked whether it still answers (output/link_watch.py): with a still picture and
no sound, every packet to the server was dropped for 240 s and all six places still said "live"
(sim PC, 2026-11-28)."""
from types import SimpleNamespace

from sprigcast.output.link_watch import (
    FAILS,
    PROBE_S,
    QUIET_KBPS,
    REST_FIRST_S,
    STEADY_S,
    LinkWatch,
    answers,
    server_of,
)


def _link(name="Twitch (horizontal)", url="rtmp://live.example/app/key"):
    link = SimpleNamespace(name=name, _url=url, connected=True, bytes_out=0, lost=[])
    link.link_lost = link.lost.append
    return link


def _rounds(watch, links, clock, n, kbps=20):
    told = []
    for _ in range(n):
        clock[0] += PROBE_S
        for link in links:
            link.bytes_out += int(kbps * 1000 / 8 * PROBE_S)
        told += watch.check(links)
    return told


def test_a_quiet_place_whose_server_goes_silent_is_told_after_a_few_asks():
    clock, up = [100.0], [True]
    asked = []
    watch = LinkWatch(ask=lambda server: (asked.append(server), up[0])[1], clock=lambda: clock[0])
    a, b = _link(), _link("Twitch (vertical)")
    assert _rounds(watch, [a, b], clock, 4) == [] and asked.count(("live.example", 1935)) == 3  # one ask a round
    up[0] = False
    assert _rounds(watch, [a, b], clock, FAILS - 1) == []
    assert _rounds(watch, [a, b], clock, 1) == [a, b]
    assert "stopped answering" in a.lost[0] and "live.example" in a.lost[0] and "key" not in a.lost[0]


def test_a_place_sending_normally_is_never_asked():
    clock = [0.0]
    asked = []
    watch = LinkWatch(ask=lambda server: asked.append(server) or False, clock=lambda: clock[0])
    busy = _link()
    assert _rounds(watch, [busy], clock, 10, kbps=QUIET_KBPS * 5) == [] and asked == [] and busy.lost == []


def test_one_missed_answer_is_not_an_outage_and_offline_places_are_left_alone():
    clock = [0.0]
    replies = iter([True, False, False, True, False, False, True, True, True, True])
    watch = LinkWatch(ask=lambda server: next(replies), clock=lambda: clock[0])
    link = _link()
    assert _rounds(watch, [link], clock, 8) == []  # never FAILS in a row
    link.connected = False
    watch2 = LinkWatch(ask=lambda server: False, clock=lambda: clock[0])
    assert _rounds(watch2, [link], clock, 10) == []  # reconnecting already: its own tries handle it


def test_a_third_outage_is_noticed_as_fast_as_the_first():
    clock, up = [0.0], [True]
    watch = LinkWatch(ask=lambda server: up[0], clock=lambda: clock[0])
    link = _link()
    _rounds(watch, [link], clock, 2)
    for _ in range(3):  # two outages that ended soon used to switch the watch off for the run
        up[0] = False
        assert _rounds(watch, [link], clock, FAILS) == [link]
        up[0] = True
        _rounds(watch, [link], clock, int((REST_FIRST_S + STEADY_S) / PROBE_S) + 4)


def test_a_server_that_never_answers_the_asks_costs_fewer_and_fewer_reconnects():
    clock = [0.0]
    watch = LinkWatch(ask=lambda server: False, clock=lambda: clock[0])
    link = _link()  # stays "connected": the server takes the stream, only the asks get no answer
    told = _rounds(watch, [link], clock, int(1800 / PROBE_S))
    assert 3 <= len(told) <= 8  # one every 15 s would be 120 in that half hour


def test_servers_and_answers():
    assert server_of("rtmp://live.example/app/key") == ("live.example", 1935)
    assert server_of("rtmps://live.example/app/key") == ("live.example", 443)
    assert server_of("rtmp://live.example:1936/app/key") == ("live.example", 1936)
    assert server_of("srt://live.example:9000") is None and server_of("") is None
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    assert answers(("127.0.0.1", port), timeout=2) is True
    listener.close()
    assert answers(("127.0.0.1", port), timeout=2) is True  # refused: the PC is reachable, that is an answer
    assert answers(("no-such-host.invalid", 1935), timeout=2) is False  # the name no longer resolves
