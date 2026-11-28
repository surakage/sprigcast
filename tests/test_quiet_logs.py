"""Things that happen over and over are logged now and then; libraries' request lines not at all."""

import logging

from sprigcast.core.quiet import Every, quiet_libraries


def test_every_lets_one_through_per_period_and_counts_the_rest():
    clock = [0.0]
    every = Every(60, clock=lambda: clock[0])
    assert every.allow("a") and every.skipped("a") == ""
    assert not every.allow("a") and not every.allow("a")
    assert every.allow("b")  # its own key
    clock[0] = 61
    assert every.allow("a") and every.skipped("a") == " (and 2 more like it since)"


def test_web_request_lines_are_quiet():
    quiet_libraries()
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_chat_waiting_is_logged_now_and_then(caplog, monkeypatch):
    from sprigcast.chat import hub

    monkeypatch.setattr(hub, "_CHAT_LOG", Every(1800))
    fake = type("H", (), {})()
    fake.readers = {"youtube": type("R", (), {"channel": "me"})()}
    fake.states = {}
    fake.changed = type("S", (), {"emit": lambda self: None})()
    fake._extra = lambda platform: None  # extra emotes, when this PC has them switched on
    caplog.set_level(logging.INFO, logger="sprigcast.chat.hub")
    for _ in range(50):
        hub.ChatHub._state(fake, "youtube", "connecting")
        hub.ChatHub._state(fake, "youtube", "waiting: not live yet")
    hub.ChatHub._state(fake, "youtube", "live")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 3 and lines[-1] == "chat youtube: live"  # no channel names in the log
