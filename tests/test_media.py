"""Media source: video and sound files, played on their own clock with picture and sound together."""

import time
from fractions import Fraction

import numpy as np
import pytest

from sprigcast.sources.media import MediaSource, is_sound_file

av = pytest.importorskip("av")


def _video(path, seconds=1.0, fps=30, sound=True):
    """A small clip: frame n is filled with grey level n (so we can tell frames apart), plus a tone."""
    with av.open(str(path), "w") as out:
        v = out.add_stream("mpeg4", rate=fps)
        v.width, v.height, v.pix_fmt = 64, 36, "yuv420p"
        a = out.add_stream("aac", rate=48000) if sound else None
        if a is not None:
            a.layout = "stereo"
        for n in range(int(seconds * fps)):
            img = np.full((36, 64, 3), min(255, n * 8), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = n
            frame.time_base = Fraction(1, fps)
            for packet in v.encode(frame):
                out.mux(packet)
        for packet in v.encode():
            out.mux(packet)
        if a is not None:
            _tone(out, a, seconds)
    return str(path)


def _tone(out, stream, seconds):
    t = np.arange(int(48000 * seconds)) / 48000
    wave = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    samples = np.stack([wave, wave])  # planar stereo
    step = 1024
    for i in range(0, samples.shape[1] - step + 1, step):
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(samples[:, i:i + step]), format="fltp", layout="stereo")
        frame.sample_rate = 48000
        frame.pts = i
        for packet in stream.encode(frame):
            out.mux(packet)
    for packet in stream.encode():
        out.mux(packet)


def _sound(path, seconds=0.5):
    with av.open(str(path), "w") as out:
        a = out.add_stream("pcm_s16le", rate=48000)
        a.layout = "stereo"
        _tone(out, a, seconds)
    return str(path)


def _play(source, seconds, frames=None):
    """Be the render thread (pictures) and the mixer (10 ms of sound) for a while."""
    heard = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        picture = source.video_frame()
        if picture is not None and frames is not None:
            frames.append(picture)
        heard += int(np.abs(source.read_block(480)).max() > 0.01)
        time.sleep(0.01)
    return heard


def _until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_a_video_plays_picture_and_sound_then_ends(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4")})
    source.start()
    try:
        frames = []
        heard = _play(source, 1.4, frames)
        info = source.info()
        assert info["state"] == "ended" and info["video"] and info["duration"] == pytest.approx(1.0, abs=0.1)
        assert source.size == (64, 36) and frames[0].shape == (36, 64, 4)
        assert len(frames) >= 20  # most of the 30 frames, in order
        levels = [int(f[0, 0, 0]) for f in frames]
        assert levels == sorted(levels)
        assert 70 <= heard <= 110  # about a second of sound in 10 ms blocks
        assert source.read_block(480).max() == 0  # nothing after the end
    finally:
        source.stop()


def test_pause_seek_restart_and_stop(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4", seconds=2.0)})
    source.start()
    try:
        _play(source, 0.3)
        source.pause()
        held = source.position
        assert _play(source, 0.2) == 0 and source.position == held and source.state == "paused"
        source.seek(1.5)
        assert source.state == "paused" and source.position == pytest.approx(1.5)
        frames = []
        _play(source, 0.1, frames)  # paused: shows the picture at 1.5 s, once
        assert len(frames) == 1 and int(frames[0][0, 0, 0]) >= 200
        source.play()
        _play(source, 0.8)
        assert source.state == "ended"
        source.play()  # ended: starts again from the top
        assert source.state == "playing" and source.position < 0.1
        source.stop_playing()
        frames = []
        _play(source, 0.1, frames)
        assert source.state == "stopped" and frames and frames[-1].max() == 0  # shows nothing
    finally:
        source.stop()


def test_loop_keeps_going(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4", seconds=0.5), "loop": True})
    source.start()
    try:
        heard = _play(source, 1.6)
        assert source.state == "playing" and 0 <= source.position < 0.5
        assert heard > 120  # sound all the way through three passes
    finally:
        source.stop()


def test_a_sound_file_waits_for_play(tmp_path):
    path = _sound(tmp_path / "ding.wav")
    assert is_sound_file(path)
    source = MediaSource("s", {"path": path})
    assert not source.sleeps and source.size == (0, 0)
    source.start()
    try:
        assert _until(lambda: source.info()["duration"] is not None)
        assert source.state == "stopped" and _play(source, 0.2) == 0
        source.play()
        assert 35 <= _play(source, 0.8) <= 60 and source.state == "ended"
    finally:
        source.stop()
    autoplay = MediaSource("s2", {"path": path, "play_on_start": True})
    autoplay.start()
    try:
        assert _play(autoplay, 0.3) > 10
    finally:
        autoplay.stop()


def test_a_video_sleeps_off_screen_and_starts_over(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4", seconds=2.0)})
    assert source.sleeps
    source.start()
    try:
        _play(source, 0.5)
        source.asleep = True
        source.sleep()
        assert source.read_block(480).max() == 0
        source.asleep = False
        source.wake()
        assert source.position == 0.0 and _play(source, 0.2) > 5
        source.update({**source.settings, "restart_on_show": False})
        _play(source, 0.3)
        at = source.position
        source.sleep()
        source.wake()
        assert source.position == pytest.approx(at, abs=0.05)  # carries on where it was
    finally:
        source.stop()


def test_a_missing_file_says_so(tmp_path):
    source = MediaSource("m", {"path": str(tmp_path / "nope.mp4")})
    source.start()
    try:
        assert _until(lambda: source.status.startswith("can't play"))
        assert source.video_frame() is None and source.read_block(480).max() == 0
    finally:
        source.stop()


def test_the_engine_mixes_media_sound(tmp_path):
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render.engine import Engine

    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id="m", kind="media", name="Clip", settings={"path": _video(tmp_path / "c.mp4")}),
                 SourceConfig(id="s", kind="media", name="Ding", settings={"path": _sound(tmp_path / "d.wav")})],
        scenes=[Scene(id="show", name="Show", canvas="h", items=[SceneItem(source="m")])],
        active_scenes={"h": "show"},
    )
    engine = Engine(col)

    class Compositor:
        targets, parts, downscale, effects = {}, {}, {}, {}

        def forget(self, _):
            pass

    engine._sync_sources(Compositor())
    try:
        mixed = {s.id for s in engine.mixer._sources}
        assert mixed == {"m", "s"}  # both reach the stream's sound
        assert not engine._sources["m"].asleep  # on air
    finally:
        for source in engine._sources.values():
            source.stop()


def test_a_video_draws_in_its_bloom(tmp_path):
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render.compositor import Compositor
    from sprigcast.render.engine import Engine

    try:
        compositor = Compositor()
    except Exception as exc:
        pytest.skip(f"no OpenGL context: {exc}")
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id="m", kind="media", name="Clip", settings={"path": _video(tmp_path / "c.mp4")})],
        scenes=[Scene(id="show", name="Show", canvas="h", items=[SceneItem(source="m")])],
        active_scenes={"h": "show"},
    )
    engine = Engine(col)
    try:
        engine._sync_sources(compositor)
        got = []
        engine.add_frame_tap("h", got.append)
        for n in range(40):
            engine._tick(compositor, 1.0 + n / 30)
            time.sleep(1 / 30)
        pictures = [np.frombuffer(f.data, dtype=np.uint8).reshape(36, 64, 4) for f in got]
        shown = [p for p in pictures if p[18, 32, 3] == 255]  # the first tick or two: not decoded yet
        assert len(shown) >= len(pictures) - 5  # then the video covers the canvas
        assert int(shown[-1][18, 32, 0]) > int(shown[0][18, 32, 0])  # and it moves on
    finally:
        for source in engine._sources.values():
            source.stop()
        compositor.release()


def test_main_window_adds_and_controls_media(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QFileDialog

    from sprigcast.app import main_window as mw
    from sprigcast.app.hotkeys import actions
    from sprigcast.app.main import load_collection

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("MediaTest")
    QSettings().clear()
    monkeypatch.setattr(mw, "ask_name", lambda parent, short, original: short)
    clip, ding = _video(tmp_path / "intro clip.mp4"), _sound(tmp_path / "ding.wav")
    col = load_collection(tmp_path / "scenes.json")
    w = mw.MainWindow(col, tmp_path / "scenes.json")
    try:
        for path in (clip, ding):
            monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, p=path, **k: (p, "")))
            w._add_media()
        video, sound = [s for s in col.sources if s.kind == "media"]
        assert video.name == "intro clip" and sound.name == "ding"
        assert any(i.source == video.id for i in w._scene().items)  # the video is placed in the bloom
        assert not any(i.source == sound.id for s in col.scenes for i in s.items)  # the sound file is in Sound
        assert w._has_sound(video) and w._has_sound(sound)
        assert (f"media:{sound.id}:toggle", "ding: play / pause") in actions(col)
        assert _until(lambda: w.media_source(sound.id) is not None, 5)
        assert _until(lambda: w.media_source(sound.id).info()["duration"] is not None, 5)
        assert w.media_control(sound.id, "play")["state"] == "playing"
        w._on_hotkey(f"media:{sound.id}:stop")
        assert w.media_source(sound.id).state == "stopped"
        app.processEvents()
    finally:
        w.close()
