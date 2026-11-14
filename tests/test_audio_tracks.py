"""Sound tracks 1-5 (everything, VOD-safe, voice, 4, 5), what recordings keep, and global devices."""

import os
import time

import numpy as np
import pytest

from sprigcast.audio.mixer import Mixer, source_tracks
from sprigcast.core.scene import Canvas, RecordingSettings, SceneCollection, SourceConfig


class Fake:
    def __init__(self, sid, value, kind="audio_output", name="", **settings):
        self.id, self.value, self.kind, self.gain, self.name = sid, value, kind, 1.0, name
        self.settings = settings

    def read_block(self, n):
        return np.full((n, 2), self.value, dtype=np.float32)


def test_vod_safe_track_leaves_out_music_and_tracks_4_and_5_take_only_what_is_put_there():
    game = Fake("game", 0.25, name="Game")
    music = Fake("spotify", 0.5, name="Spotify")  # music by its name until put in a group
    alert = Fake("alert", 0.125, group="Music", track4=True)  # music by its group
    mic = Fake("mic", 0.0625, kind="audio_input", name="Music mic")  # a mic is never music
    assert "vod" not in source_tracks(music) and "vod" not in source_tracks(alert)
    assert "vod" in source_tracks(game) and "vod" in source_tracks(mic)
    assert "vod" in source_tracks(Fake("x", 0, name="Spotify", group="Game"))  # its group wins over the name
    assert "vod" not in source_tracks(Fake("g", 0, name="Game", vod=False))  # kept out by hand
    assert "vod" not in source_tracks(Fake("g", 0, tracks=["stream"]))  # not recorded: not in VOD-safe either
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([game, music, alert, mic])
    got = []
    mixer.add_tap(got.append, ("recording", "vod", "voice", "track4", "track5"))
    mixer.tick()
    everything, vod, voice, four, five = got[0]
    assert everything[0, 0] == pytest.approx(0.9375)
    assert vod[0, 0] == pytest.approx(0.3125)  # game + mic
    assert voice[0, 0] == pytest.approx(0.0625)
    assert four[0, 0] == pytest.approx(0.125)
    assert five[0, 0] == 0.0


def test_without_music_the_vod_track_is_the_same_mix():
    mixer = Mixer(report=lambda levels: None)
    mixer.set_sources([Fake("game", 0.25, name="Game")])
    got = []
    mixer.add_tap(got.append, ("recording", "vod"))
    mixer.tick()
    assert got[0][0] is got[0][1]  # mixed once


def test_recording_tracks_follow_the_old_voice_setting_and_track_1_always_stays():
    assert RecordingSettings().audio_tracks == [1, 3]
    assert RecordingSettings.model_validate({"voice_track": False}).audio_tracks == [1]
    assert RecordingSettings.model_validate({"voice_track": True}).audio_tracks == [1, 3]
    picked = RecordingSettings(audio_tracks=[3, 2, 9, 2])
    assert picked.audio_tracks == [2, 3] and picked.track_ids() == ("vod", "voice")
    assert RecordingSettings(audio_tracks=[]).audio_tracks == [1]
    assert not RecordingSettings(audio_tracks=[1, 2]).voice_track


def _collection(**audio):
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080)],
        sources=[
            SourceConfig(id="desktop", kind="audio_output", name="Desktop audio"),
            SourceConfig(id="mic", kind="audio_input", name="Mic"),
            SourceConfig(id="mic-2", kind="audio_input", name="Mic (Wave)"),
            SourceConfig(id="cam", kind="color", name="Colour"),
        ],
        audio=audio,
    )


def test_global_devices_switched_off_are_not_opened_and_your_own_stay():
    col = _collection()
    assert col.global_device_ids() == ["mic", "desktop"]  # older files: the first of each
    assert [s.id for s in col.sources_in_use()] == ["desktop", "mic", "mic-2", "cam"]
    col.audio.global_devices = False
    assert [s.id for s in col.sources_in_use()] == ["mic-2", "cam"]
    assert col.device_off(col.source("mic")) and not col.device_off(col.source("mic-2"))
    pinned = _collection(global_devices=False, global_sources=["mic-2", "gone"])
    assert pinned.global_device_ids() == ["mic-2"]
    assert [s.id for s in pinned.sources_in_use()] == ["desktop", "mic", "cam"]
    back = SceneCollection.model_validate_json(pinned.model_dump_json())
    assert back.audio.global_sources == ["mic-2", "gone"] and not back.audio.global_devices


def test_track_names_fill_in_blanks():
    col = _collection(track_names=["All", " "])
    assert [col.audio.track_name(n) for n in range(1, 6)] == ["All", "VOD-safe", "Voice", "Track 4", "Track 5"]


def test_recording_keeps_the_chosen_tracks_with_their_names(tmp_path):
    pytest.importorskip("av")
    from test_shared_encoder import CANVASES, TrackEngine, _audio_tracks, _feed

    from sprigcast.output.shared import EncoderPool, SharedRecording

    class Engine(TrackEngine):
        LEVEL = {**TrackEngine.LEVEL, "vod": 0.375 - 0.005}

    engine = Engine()
    pool = EncoderPool(engine)
    pool.recording_tracks = ("recording", "vod", "voice")
    pool.track_titles = {"recording": "All of it", "vod": "VOD-safe"}
    rec = SharedRecording.start(pool, CANVASES[:1], tmp_path, 800, codec="libx264")
    assert pool.encoders["horizontal"].tracks == ("recording", "vod", "voice", "clips")
    _feed(engine, time.perf_counter(), 20, pool=pool)
    rec.stop()
    assert rec.errors == []
    assert _audio_tracks(rec.paths[0]) == [("All of it", 0.5), ("VOD-safe", 0.37), ("Voice", 0.25)]


def test_audio_page_sets_switch_tracks_and_which_sounds_go_where(monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    from sprigcast.app.audio_page import AudioPage, on_track

    QApplication.instance() or QApplication([])
    col = _collection()
    col.sources.append(SourceConfig(id="song", kind="media", name="Song", settings={"group": "Music"}))
    page = AudioPage(col, {"audio_input": ["Wave"]})
    assert set(page.track_ticks) == {"desktop", "mic", "mic-2", "song"}
    assert not page.track_ticks["song"][1].isChecked()  # music starts off the VOD-safe track
    assert page.track_kept[0].isChecked() and not page.track_kept[0].isEnabled()
    page.global_on.setChecked(False)
    page.track_kept[1].setChecked(True)  # recordings keep VOD-safe
    page.track_kept[2].setChecked(False)  # ... but not voice
    page.track_names[3].setText("Game only")
    page.track_ticks["mic-2"][2].setChecked(False)  # Wave mic off the voice track
    page.track_ticks["desktop"][3].setChecked(True)  # desktop on track 4
    page.track_ticks["song"][1].setChecked(True)  # this song is fine for VODs
    page.apply_to(col)
    assert not col.audio.global_devices and col.audio.global_sources == ["mic", "desktop"]
    assert col.recording.audio_tracks == [1, 2] and not col.recording.voice_track
    assert col.audio.track_name(4) == "Game only"
    assert not on_track(col.source("mic-2"), 3) and on_track(col.source("mic-2"), 1)
    assert on_track(col.source("desktop"), 4) and on_track(col.source("song"), 2)
    assert on_track(col.source("mic"), 3)  # untouched
    again = AudioPage(col, {})
    assert set(again.track_ticks) == {"mic-2", "song"}  # switched-off global devices leave the list
    assert not again.global_rows[0].isEnabled()
