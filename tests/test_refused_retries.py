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
    assert level == "bad" and "Facebook keeps turning the stream away" in text and "try" not in text


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
