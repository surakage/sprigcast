"""A place whose server keeps ending tries as they start is said plainly and tried less often (stream review
2026-11-23: Facebook turned away 80 tries every 30 s for 40 minutes)."""
from types import SimpleNamespace

from sprigcast.app import health
from sprigcast.output import stream
from sprigcast.output.stream import (GRACE_RETRY, REFUSED_GRACE, REFUSED_MAX, REFUSED_SAY, RETRY_MAX, note_refusal,
                                     refused_status, retry_plan)

REFUSED = "couldn't connect to live-api-s.facebook.com: the server ended the connection as it was starting"


def _tries(st, n, message=REFUSED, sent=False, delay=2.0):
    waits = []
    for _ in range(n):
        _, delay, after = retry_plan(message, 1000, delay)
        delay, after = note_refusal(st, message, sent, delay, after)
        waits.append(delay)
        delay = after
    return waits


def test_quick_tries_first_then_slower_up_to_five_minutes():
    st = SimpleNamespace(name="Facebook (horizontal)", refused=0)
    waits = _tries(st, 12)
    assert waits[:REFUSED_SAY - 1] == [2.0, 4.0]
    assert all(w >= RETRY_MAX for w in waits[REFUSED_SAY - 1:])
    assert waits[-1] == REFUSED_MAX and max(waits) == REFUSED_MAX
    assert st.refused == 12


def test_a_try_that_sent_or_an_offline_pc_starts_the_count_again():
    st = SimpleNamespace(name="Facebook", refused=0)
    _tries(st, 5)
    _tries(st, 1, sent=True)
    assert st.refused == 0
    _tries(st, 5)
    _tries(st, 1, message="Failed to resolve hostname live-api-s.facebook.com")
    assert st.refused == 0


def test_status_and_live_notice_say_it_plainly():
    st = SimpleNamespace(name="Facebook", refused=0, connected=False, reconnects=0, status="")
    waits = _tries(st, REFUSED_SAY)
    st.reconnects = REFUSED_SAY
    st.status = refused_status(st, REFUSED, waits[-1])
    assert st.status.startswith("disconnected:") and "stream key" in st.status and "30 s" in st.status
    assert "5 min" in refused_status(st, REFUSED, REFUSED_MAX)
    level, text = health.notice([(None, st)])
    assert level == "bad" and "Facebook may have ended this stream" in text and "try " not in text


def test_a_few_refusals_still_read_as_reconnecting():
    st = SimpleNamespace(name="Kick", refused=0, connected=False, reconnects=2, status="disconnected: x")
    _tries(st, 2)
    assert health.notice([(None, st)])[1] == "Reconnecting to Kick… try 2"


def test_a_place_that_was_just_live_is_asked_briskly_while_the_site_closes_the_old_connection(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(stream.time, "monotonic", lambda: now[0])
    st = SimpleNamespace(name="Twitch", refused=0)
    _tries(st, 1, sent=True)  # it was live and dropped
    waits = []
    for _ in range(8):
        waits += _tries(st, 1, delay=waits[-1] * 2 if waits else 2.0)
        now[0] += waits[-1]
    assert max(waits) <= GRACE_RETRY and st.refused == 0
    now[0] += REFUSED_GRACE  # still turned away long after: now it counts, and slows
    assert _tries(st, REFUSED_SAY)[-1] >= RETRY_MAX and st.refused == REFUSED_SAY


def _place(name, status="", connected=False, reconnects=1, **more):
    return (None, SimpleNamespace(name=name, refused=0, connected=connected, reconnects=reconnects, status=status,
                                  **more))


def test_every_place_dropping_at_once_reads_as_the_internet_not_five_reconnects():
    gone = "disconnected: I/O error; retrying in 4 s"
    level, text = health.notice([_place("Twitch", gone), _place("Kick", gone), _place("YouTube", gone)])
    assert level == "bad" and text == "Your internet seems down. Waiting for it to come back, retrying…"
    # One of several is that place's own trouble.
    live = _place("Kick", "live", connected=True, reconnects=0)
    assert health.notice([_place("Twitch", gone, reconnects=2), live])[1] == "Reconnecting to Twitch… try 2"


def test_a_pc_that_cant_look_the_server_up_is_waiting_for_internet():
    _, delay, _ = retry_plan("Failed to resolve hostname live.twitch.tv", 1000, 2.0)
    st = SimpleNamespace(name="Twitch", same_key_as="")
    status = stream.dropped_status(st, "Failed to resolve hostname", retry_plan("Failed to resolve hostname x", 1, 2.0)[0],
                                   delay)
    assert health.OFFLINE_WORDS in status
    assert health.notice([_place("Twitch", status)])[1] == "Waiting for internet, retrying Twitch…"


def test_a_place_that_came_back_says_so_for_a_moment(monkeypatch):
    now = [500.0]
    monkeypatch.setattr(health.time, "monotonic", lambda: now[0])
    back = [_place("Twitch", "live", connected=True, back_live_at=499.0),
            _place("Kick", "live", connected=True, back_live_at=498.0)]
    assert health.notice(back) == ("good", "Back live: Twitch and Kick")
    now[0] += health.BACK_LIVE_S
    assert health.notice(back) == ("", "")


def test_going_live_again_right_after_a_stop_keeps_asking_briskly(monkeypatch):
    """The server still held the old session ("Already publishing"): 3 refusals in 6 s, then no try for a minute
    and a half though it was free seconds later (sim PC, 2026-11-28)."""
    now = [1000.0]
    monkeypatch.setattr(stream.time, "monotonic", lambda: now[0])
    stream._ended_at.clear()
    old = SimpleNamespace(name="Twitch (horizontal)", _url="rtmp://live.example/app/key")
    stream.note_ended(old)  # Stop
    now[0] += 2
    new = SimpleNamespace(name="Twitch (horizontal)", refused=0, _url="rtmp://live.example/app/key")  # Go live again
    waits = _tries(new, 8)
    assert max(waits) <= GRACE_RETRY and new.refused == 0
    now[0] += REFUSED_GRACE  # still turned away long after: now it is said plainly and tried less often
    assert max(_tries(new, 6)) >= RETRY_MAX and new.refused == 6
    other = SimpleNamespace(name="Kick (horizontal)", refused=0, _url="rtmp://kick.example/app/other")
    now[0] = 1001.0
    assert max(_tries(other, 6)) >= RETRY_MAX  # a place that wasn't just stopped is counted as before


def test_a_bare_not_permitted_at_connect_is_said_in_plain_words():
    """Five of six places turned away at once got only "[Errno 1] Operation not permitted" (sim PC 2026-11-28)."""
    import pytest

    with pytest.raises(ConnectionError) as caught:
        with stream.explain_connect("rtmp://live.example/app/key"):
            raise PermissionError(1, "Operation not permitted")
    said = str(caught.value)
    assert "turned the stream away" in said and "still live there" in said and "key" not in said.split(":")[0]
    assert "live.example" in said and "app/key" not in said


def test_refusals_after_a_try_that_was_live_come_briskly():
    """A place reset before it sent anything new was still live a moment ago: the tries after it stay at most
    GRACE_RETRY apart (they grew to 40 s and 60 s with the server free, sim PC 2026-11-28)."""
    st = SimpleNamespace(name="Custom RTMP", refused=0)
    _tries(st, 1, message="Connection reset by peer", sent=True, delay=30.0)  # was live in this try
    waits = _tries(st, 6, delay=30.0)
    assert max(waits) <= GRACE_RETRY and st.refused == 0
