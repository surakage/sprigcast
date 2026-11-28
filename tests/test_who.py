"""Who asked for a change: an app over the Control API, or SprigCast's own window (a place switched off while
live read the same whoever did it: the user's stream, 2026-11-27)."""
from sprigcast.control.who import asker, asking


def test_the_app_asking_is_named_and_the_window_otherwise():
    assert asker("the places list") == "the places list"
    with asking("Streamer.bot"):
        assert asker("the places list") == "Streamer.bot"
    assert asker() == ""
