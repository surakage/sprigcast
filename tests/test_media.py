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


def _grey(picture):
    """The top-left level of a picture as RGBA would show it (videos now arrive as packed YUV planes)."""
    if picture.ndim == 2:  # pack_i420: Y rows first, TV range
        return round((int(picture[0, 0]) - 16) * 255 / 219)
    return int(picture[0, 0, 0])


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
        assert source.size == (64, 36) and frames[0].shape == (36 + 18, 64)  # Y, then U|V rows
        assert source.channel_order == "I420/601/tv"
        assert len(frames) >= 20  # most of the 30 frames, in order
        levels = [_grey(f) for f in frames]
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
        assert len(frames) == 1 and _grey(frames[0]) >= 200
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


def _webm(path, seconds=0.6, fps=30):
    """A VP9 .webm (what animations and alerts use), decoded with libvpx to keep see-through parts."""
    with av.open(str(path), "w") as out:
        v = out.add_stream("libvpx-vp9", rate=fps)
        v.width, v.height, v.pix_fmt = 64, 36, "yuv420p"
        for n in range(int(seconds * fps)):
            frame = av.VideoFrame.from_ndarray(np.full((36, 64, 3), min(255, n * 8), dtype=np.uint8), format="rgb24")
            frame.pts = n
            frame.time_base = Fraction(1, fps)
            for packet in v.encode(frame):
                out.mux(packet)
        for packet in v.encode():
            out.mux(packet)
    return str(path)


def test_a_webm_ends_without_looping_or_errors(tmp_path):
    if "libvpx-vp9" not in av.codecs_available:
        pytest.skip("no libvpx")
    source = MediaSource("m", {"path": _webm(tmp_path / "2.webm"), "loop": False})
    source.start()
    try:
        frames = []
        _play(source, 1.2, frames)
        assert source.state == "ended" and source.status == "ok", source.status
        frames.clear()
        _play(source, 6.0, frames)  # well past the old 5 s retry: nothing plays again
        assert not frames and source.state == "ended" and source.status == "ok"
    finally:
        source.stop()


def test_a_looping_webm_loops_without_errors(tmp_path, caplog):
    # Each pass used to end in "End of file: avcodec_send_packet()" (the decoder was flushed twice),
    # then a pause and a race through the file: Busy/Struggling on the user's PC.
    if "libvpx-vp9" not in av.codecs_available:
        pytest.skip("no libvpx")
    source = MediaSource("m", {"path": _webm(tmp_path / "loop.webm", seconds=0.5), "loop": True})
    source.start()
    try:
        source.play()
        frames = []
        _play(source, 1.7, frames)
        assert source.status == "ok" and "could not be played" not in caplog.text, source.status
        assert len(frames) > 30  # three passes of 15 frames, at their own pace
    finally:
        source.stop()


def test_hiding_its_eye_pauses_and_showing_starts_over(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4", seconds=0.6)})
    source.start()
    try:
        _play(source, 0.9)
        assert source.state == "ended"
        source.set_hidden(True)
        source.set_hidden(False)  # Start over when shown: plays again, even after it ended
        assert source.state == "playing" and source.position < 0.1
        assert _play(source, 0.3) > 5
        source.set_hidden(True)
        assert source.state == "paused"
        source.update({**source.settings, "restart_on_show": False})
        at = source.position
        source.set_hidden(False)  # carries on where it was
        assert source.state == "playing" and source.position == pytest.approx(at, abs=0.05)
    finally:
        source.stop()


def test_speed_runs_the_clock_and_sound_faster(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4", seconds=2.0), "speed": 2.0})
    source.start()
    try:
        heard = _play(source, 1.3)
        assert source.state == "ended"  # 2 s of video in about 1 s
        assert 70 <= heard <= 115  # its sound squeezed into the same second
        source.update({**source.settings, "speed": 0.5})
        source.restart()
        _play(source, 0.6)
        assert source.position == pytest.approx(0.3, abs=0.12)
    finally:
        source.stop()


def test_hardware_decoding_falls_back_to_the_cpu(tmp_path):
    source = MediaSource("m", {"path": _video(tmp_path / "clip.mp4"), "hw_decode": True})
    source.start()
    try:
        frames = []
        _play(source, 0.5, frames)
        assert frames and source.status == "ok"
    finally:
        source.stop()


def test_the_engine_tells_a_video_when_its_eye_goes_off_and_on(tmp_path):
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render.engine import Engine

    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36)],
        sources=[SourceConfig(id="m", kind="media", name="Clip", settings={"path": _video(tmp_path / "c.mp4")}),
                 SourceConfig(id="p", kind="scene", name="BRB", settings={"scene": "brb"})],
        scenes=[Scene(id="show", name="Show", canvas="h", items=[SceneItem(source="p")]),
                Scene(id="brb", name="BRB", canvas="h", kind="part", items=[SceneItem(source="m")])],
        active_scenes={"h": "show"},
    )
    engine = Engine(col)
    told = []
    try:
        engine._sync_sources(type("C", (), {"forget": lambda *_: None, "targets": {}, "parts": {},
                                            "downscale": {}, "effects": {}})())
        engine._sources["m"].set_hidden = told.append
        engine._show_hidden()
        assert told == [False]  # inside a sprig on air: showing
        col = engine._collection  # the engine's own copy, as Build's edits reach it
        col.scenes[1].items[0].visible = False
        engine._show_hidden()
        engine._show_hidden()
        col.scenes[1].items[0].visible = True
        engine._show_hidden()
        col.scenes[0].items[0].visible = False  # the sprig itself hidden hides what's in it
        engine._show_hidden()
        assert told == [False, True, False, True]
    finally:
        for source in engine._sources.values():
            source.stop()


def test_waking_and_sleeping_a_looping_alpha_video_leaves_no_threads_behind(tmp_path):
    import threading

    import psutil

    source = MediaSource("brb", {"path": _webm(tmp_path / "brb.webm", seconds=0.5), "loop": True})
    source.start()
    time.sleep(0.3)
    process = psutil.Process()
    before = process.num_threads()
    for _ in range(30):  # the soak's Be right back bloom, on and off screen quickly
        source.sleep()
        began = time.perf_counter()
        source.wake()
        assert time.perf_counter() - began < 0.05  # never waits on the old decoder thread
        time.sleep(0.03)
    time.sleep(0.5)
    deciding = [t for t in threading.enumerate() if t.name == "media-brb"]
    assert len(deciding) == 1  # one decoder thread, never two decoding the same file
    assert process.num_threads() <= before + 3
    source.stop()


def _gif(path, frames=6):
    from PIL import Image

    pictures = [Image.new("RGB", (64, 36), (n * 40, n * 40, n * 40)) for n in range(frames)]
    pictures[0].save(str(path), save_all=True, append_images=pictures[1:], duration=100, loop=0)
    return str(path)


def test_pictures_handed_over_own_their_memory(tmp_path):
    """A picture the render thread holds never points into FFmpeg's frame (and so its decoder)."""
    source = MediaSource("g", {"path": _gif(tmp_path / "a.gif"), "loop": True})
    source.start()
    try:
        frames = []
        _play(source, 0.4, frames)
        assert frames
        for picture in frames:
            assert picture.flags.owndata or picture.base is None or isinstance(picture.base, np.ndarray)
    finally:
        source.stop()


def test_rapid_wake_sleep_loop_and_controls_from_many_threads(tmp_path):
    """Stress: a GIF and a see-through webm looping fast while sleep/wake, seek, restart, hide and speed come
    from other threads and the render and mixer threads keep taking frames. No errors, one decoder thread
    each at the end, and the thread count back where it started."""
    import random
    import threading

    import psutil

    sources = [MediaSource("gif", {"path": _gif(tmp_path / "loop.gif", frames=3), "loop": True}),
               MediaSource("brb", {"path": _webm(tmp_path / "brb.webm", seconds=0.2), "loop": True})]
    for source in sources:
        source.start()
    time.sleep(0.3)
    process = psutil.Process()
    before = process.num_threads()
    errors: list[BaseException] = []
    stop = threading.Event()

    def guarded(job):
        def run():
            try:
                job()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        return threading.Thread(target=run, daemon=True)

    def render():  # the render and mixer threads
        while not stop.is_set():
            for source in sources:
                picture = source.video_frame()
                if picture is not None:
                    picture.sum()  # read every byte: a freed buffer would show up here
                source.read_block(480)
            time.sleep(0.002)

    def flip(source, seed):  # the engine: blooms on and off screen, eyes on and off
        rng = random.Random(seed)
        while not stop.is_set():
            action = rng.choice(("sleep", "wake", "wake", "hide", "show", "seek", "restart", "speed"))
            if action == "sleep":
                source.sleep()
            elif action == "wake":
                source.wake()
            elif action in ("hide", "show"):
                source.set_hidden(action == "hide")
            elif action == "seek":
                source.seek(rng.random() * 0.2)
            elif action == "restart":
                source.restart()
            else:
                source.update({**source.settings, "speed": rng.choice((0.5, 1.0, 2.0))})
            time.sleep(rng.random() * 0.01)

    workers = [guarded(render)] + [guarded(lambda s=s, i=i: flip(s, i)) for i, s in enumerate(sources)]
    for worker in workers:
        worker.start()
    time.sleep(4.0)
    stop.set()
    for worker in workers:
        worker.join(timeout=5)
    assert not errors, errors
    for source in sources:
        source.wake()
    assert _until(lambda: all(s.status == "ok" for s in sources))
    time.sleep(0.5)
    for source in sources:
        assert len([t for t in threading.enumerate() if t.name == f"media-{source.id}" and t.is_alive()]) == 1
    assert _until(lambda: process.num_threads() <= before + 3, timeout=5.0)
    for source in sources:
        source.stop()


def test_a_bloom_of_seven_media_waking_and_sleeping_reuses_files_and_decoders(tmp_path, monkeypatch):
    """The PC's Heavy test bloom: 4 GIFs and 3 see-through webms on and off screen 10 times, with Python's cycle
    collector off. Each file is opened once and its decoder kept (no new FFmpeg threads per wake), and the
    thread count ends where it started."""
    import gc
    import threading

    import psutil

    opened = []
    real_open = MediaSource._open
    monkeypatch.setattr(MediaSource, "_open", lambda self, path: opened.append(path) or real_open(self, path))
    files = [_gif(tmp_path / f"g{i}.gif", frames=3) for i in range(4)] + \
        [_webm(tmp_path / f"w{i}.webm", seconds=0.2) for i in range(3)]
    sources = [MediaSource(f"m{i}", {"path": f, "loop": True}) for i, f in enumerate(files)]
    stop = threading.Event()

    def render():
        while not stop.is_set():
            for source in sources:
                source.video_frame()
                source.read_block(480)
            time.sleep(0.005)

    drawer = threading.Thread(target=render, daemon=True)
    drawer.start()
    for source in sources:
        source.start()
    process = psutil.Process()
    gc.disable()
    try:
        assert _until(lambda: all(s.status == "ok" for s in sources))
        time.sleep(0.3)
        before = process.num_threads()
        for _ in range(10):
            for source in sources:
                source.sleep()
            time.sleep(0.1)
            for source in sources:
                source.wake()
            time.sleep(0.2)
        assert abs(process.num_threads() - before) <= 10
        assert sorted(opened) == sorted(files)  # once each, never again on a wake
    finally:
        gc.enable()
        stop.set()
        drawer.join(timeout=2)
        for source in sources:
            source.stop()


def test_asleep_long_enough_the_thread_closes_its_file_and_ends(tmp_path, monkeypatch):
    import threading

    from sprigcast.sources import media

    monkeypatch.setattr(media, "PARK_S", 0.3)
    released = []
    real_release = media._Held.release
    monkeypatch.setattr(media._Held, "release",
                        lambda self: released.append(self.container is not None) or real_release(self))
    source = MediaSource("brb", {"path": _webm(tmp_path / "brb.webm", seconds=0.3), "loop": True})
    source.start()
    assert _until(lambda: source.status == "ok")
    source.sleep()
    time.sleep(0.15)
    assert [t for t in threading.enumerate() if t.name == "media-brb"]  # still parked with the file open
    assert _until(lambda: not [t for t in threading.enumerate() if t.name == "media-brb"])
    assert True in released  # it closed its open file on its own thread
    source.wake()  # and a later wake starts again
    frames = []
    _play(source, 0.4, frames)
    assert frames
    source.stop()


def _webm_with_sound(path, seconds=0.4, fps=30):
    with av.open(str(path), "w") as out:
        v = out.add_stream("libvpx-vp9", rate=fps)
        v.width, v.height, v.pix_fmt = 64, 36, "yuv420p"
        a = out.add_stream("libopus", rate=48000)
        a.layout = "stereo"
        for n in range(int(seconds * fps)):
            frame = av.VideoFrame.from_ndarray(np.full((36, 64, 3), n * 8, dtype=np.uint8), format="rgb24")
            frame.pts, frame.time_base = n, Fraction(1, fps)
            for packet in v.encode(frame):
                out.mux(packet)
        for packet in v.encode():
            out.mux(packet)
        _tone(out, a, seconds)
    return str(path)


def test_sound_resamplers_and_audio_filters_start_no_thread_pools(tmp_path):
    """FFmpeg's filter graphs (inside every AudioResampler, and each audio filter) start a pool of threads that
    only goes when Python's cycle collector runs. 10 restarts of a webm with sound and 10 Equalizers added and
    changed, with the collector off: the thread count barely moves."""
    import gc
    import threading

    import psutil

    from sprigcast.core.filters import AudioChain
    from sprigcast.core.scene import Filter

    process = psutil.Process()
    source = MediaSource("clip", {"path": _webm_with_sound(tmp_path / "clip.webm"), "loop": True})
    source.start()
    gc.disable()
    try:
        assert _until(lambda: source.status == "ok")
        _play(source, 0.3)
        before = process.num_threads()
        for _ in range(10):
            source.restart()
            assert _play(source, 0.15) > 0  # sound plays each time
        block = (np.random.default_rng(1).standard_normal((480, 2)) * 0.1).astype(np.float32)
        for n in range(10):
            chain = AudioChain([Filter(id=f"eq{n}", kind="eq3", name="eq", settings={"low_db": 3.0})])
            chain.process(block)
            chain = AudioChain([Filter(id=f"eq{n}", kind="eq3", name="eq", settings={"low_db": 6.0})])
            chain.process(block)
            del chain
        time.sleep(0.3)
        assert abs(process.num_threads() - before) < 5
        assert len([t for t in threading.enumerate() if t.name == "media-clip"]) == 1
    finally:
        gc.enable()
        source.stop()
