"""Wave Link sync logic, without Wave Link."""

from sprigcast.audio.wavelink import WaveLinkState, channel_of, db_to_level, level_to_db
from sprigcast.core.scene import SourceConfig


def _state():
    state = WaveLinkState()
    state.set_mixes([{"id": "mix-local", "name": "Personal Mix"}, {"id": "mix-stream", "name": "Stream Mix"}])
    state.set_channels([
        {"id": "ch-game", "name": "Game", "mixes": [{"mixId": "mix-stream", "isMuted": False, "level": 1.0},
                                                    {"mixId": "mix-local", "isMuted": True, "level": 0.2}]},
        {"id": "ch-music", "name": "Music", "mixes": [{"mixId": "mix-stream", "isMuted": True, "level": 0.5}]},
    ])
    return state


def _sound(device, **settings):
    return SourceConfig(id=device, kind="audio_output", name=device, settings={"device": device, **settings})


def test_channel_names():
    assert channel_of("Game (Elgato Virtual Audio)") == "game"
    assert channel_of("Speakers (Realtek)") is None
    assert level_to_db(1.0) == 0.0 and level_to_db(0.0) == -100.0
    assert abs(db_to_level(-6.0) - 0.501) < 0.01 and db_to_level(6.0) == 1.0


def test_stream_mix_is_used():
    state = _state()
    assert state.mix_id == "mix-stream"
    assert state.channels["ch-game"]["muted"] is False and state.channels["ch-music"]["muted"] is True


def test_outgoing_only_what_differs():
    state = _state()
    game = _sound("Game (Elgato Virtual Audio)", muted=True, volume_db=0.0)
    other = _sound("Speakers (Realtek)", muted=True)
    assert state.outgoing([game, other]) == [{"id": "ch-game", "mixes": [{"mixId": "mix-stream", "isMuted": True}]}]
    assert state.outgoing([game]) == []  # remembered: not sent twice
    game.settings["volume_db"] = -6.0
    sent = state.outgoing([game])
    assert sent[0]["mixes"][0]["level"] == round(db_to_level(-6.0), 4)
    game.settings["volume_db"] = 3.0  # above Wave Link's top: it stays at full
    assert state.outgoing([game])[0]["mixes"][0]["level"] == 1.0


def test_incoming_changes_matching_sounds():
    state = _state()
    music = _sound("Music (Elgato Virtual Audio)", muted=False, volume_db=0.0)
    game = _sound("Game (Elgato Virtual Audio)")
    changes = state.incoming({"id": "ch-music", "mixes": [{"mixId": "mix-stream", "isMuted": True, "level": 0.5}]},
                             [music, game])
    assert changes == [(music, {"muted": True, "volume_db": level_to_db(0.5)})]
    # a change in the other mix leaves SprigCast alone
    assert state.incoming({"id": "ch-game", "mixes": [{"mixId": "mix-local", "isMuted": False}]}, [game]) == []
    # an echo of what SprigCast already has does nothing
    music.settings.update(muted=True, volume_db=level_to_db(0.5))
    assert state.incoming({"id": "ch-music", "isMuted": True}, [music]) == []
