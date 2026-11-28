"""Twitch Enhanced Broadcasting: what its stream counts across reconnects."""


def test_a_reconnect_keeps_the_frames_already_sent_in_the_count():
    from types import SimpleNamespace

    from sprigcast.output.enhanced_stream import _Track

    track = _Track(0, SimpleNamespace(fps=60), SimpleNamespace(fps=60))
    track.frames = 201361
    track.reset()  # what a reconnect does (_drain)
    track.frames = 110
    assert track.frames_before + track.frames == 201471
    track.reset()
    assert (track.frames_before, track.frames) == (201471, 0)
