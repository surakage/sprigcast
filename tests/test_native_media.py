"""The engine's own player for video, GIF and sound files (native/core/src/media.cpp, docs/native-engine.md
phase 3), through sprigcore.MediaPlayer and sources/media.py.

The player needs no graphics card, so these run wherever it can be built: against sprigcore.dll on Windows
(SPRIGCAST_CORE_DLL in CI), and on Linux against the player built alone with g++ (a few seconds, once a run).
It plays real files, made here with PyAV, with the same FFmpeg PyAV brought.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from sprigcast import sprigcore

av = pytest.importorskip("av")
ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "native" / "core"
MEDIA_SOURCES = ("base.cpp", "ffmpeg.cpp", "media.cpp", "writer.cpp")  # the parts of the engine without Direct3D


@pytest.fixture(scope="session")
def player_lib(tmp_path_factory):
    """Makes sure sprigcore can reach a media player, building one on Linux."""
    if sys.platform != "win32" and not os.path.isfile(os.environ.get("SPRIGCAST_MEDIA_LIB", "")):
        compiler = shutil.which("g++")
        if compiler is None:
            pytest.skip("no g++ to build the player with")
        out = tmp_path_factory.mktemp("sprigmedia") / "libsprigmedia.so"
        subprocess.run([compiler, "-std=c++20", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-fPIC", "-shared",
                        f"-I{CORE / 'include'}", f"-I{ROOT / 'native' / 'third_party' / 'ffmpeg' / 'include'}",
                        "-o", str(out), *(str(CORE / "src" / name) for name in MEDIA_SOURCES),
                        "-ldl", "-pthread"], check=True)
        os.environ["SPRIGCAST_MEDIA_LIB"] = str(out)
        sprigcore._media_lib.cache_clear()
        sprigcore.media_ready.cache_clear()
    elif sys.platform == "win32" and not sprigcore.available():
        pytest.skip("sprigcore.dll isn't built")
    assert sprigcore.media_ready(), "the engine couldn't load PyAV's FFmpeg"
    return True


@pytest.fixture(scope="session")
def files(tmp_path_factory):
    """A 2 s video with sound, a 1 s GIF, a 1 s sound file and a see-through WebM."""
    folder = tmp_path_factory.mktemp("media")

    def pictures(count, w, h):
        for i in range(count):
            img = np.zeros((h, w, 3), np.uint8)
            img[:, :, 0] = i * 4
            img[:, :max(1, i * w // count), 1] = 255
            img[h // 2:, :, 2] = 200
            yield i, img

    video = folder / "clip.mp4"
    with av.open(str(video), "w") as c:
        v = c.add_stream("libx264", rate=30)
        v.width, v.height, v.pix_fmt = 320, 240, "yuv420p"
        v.options = {"bf": "0"}
        a = c.add_stream("aac", rate=48000)
        a.layout = "stereo"
        for i, img in pictures(60, 320, 240):
            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = i
            c.mux(v.encode(frame))
        tone = (np.sin(2 * np.pi * 440 * np.arange(48000 * 2) / 48000) * 0.5).astype(np.float32)
        for i in range(0, len(tone), 1024):
            chunk = tone[i:i + 1024]
            frame = av.AudioFrame.from_ndarray(np.stack([chunk, chunk]), format="fltp", layout="stereo")
            frame.sample_rate, frame.pts = 48000, i
            c.mux(a.encode(frame))
        c.mux(v.encode())
        c.mux(a.encode())

    gif = folder / "loop.gif"
    with av.open(str(gif), "w") as c:
        v = c.add_stream("gif", rate=10)
        v.width, v.height, v.pix_fmt = 64, 48, "rgb8"
        for i, img in pictures(10, 64, 48):
            frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="rgb8")
            frame.pts = i
            c.mux(v.encode(frame))
        c.mux(v.encode())

    sound = folder / "tone.wav"
    with av.open(str(sound), "w") as c:
        a = c.add_stream("pcm_s16le", rate=44100)
        a.layout = "mono"
        tone = (np.sin(2 * np.pi * 330 * np.arange(44100) / 44100) * 20000).astype(np.int16)
        for i in range(0, len(tone), 1024):
            frame = av.AudioFrame.from_ndarray(tone[None, i:i + 1024], format="s16", layout="mono")
            frame.sample_rate, frame.pts = 44100, i
            c.mux(a.encode(frame))
        c.mux(a.encode())

    found = {"video": str(video), "gif": str(gif), "sound": str(sound), "folder": folder}
    if "libvpx-vp9" in av.codecs_available:
        webm = folder / "alpha.webm"
        with av.open(str(webm), "w") as c:
            v = c.add_stream("libvpx-vp9", rate=30)
            v.width, v.height, v.pix_fmt = 64, 48, "yuva420p"
            for i in range(15):
                img = np.zeros((48, 64, 4), np.uint8)
                img[:, :32] = (255, 0, 0, 255)  # the right half stays see-through
                frame = av.VideoFrame.from_ndarray(img, format="rgba").reformat(format="yuva420p")
                frame.pts = i
                c.mux(v.encode(frame))
            c.mux(v.encode())
        found["webm"] = str(webm)
    return found


def opened(path, video=True, playing=True, **options):
    player = sprigcore.MediaPlayer(playing=playing)
    player.set_options(options.get("loop", False), options.get("clear_on_end", False),
                       options.get("restart_on_show", True), options.get("speed", 1.0))
    player.set_file(path, video, playing)
    player.control(sprigcore.MEDIA_START)
    return player


def pump(player, seconds, until=None, audio=None):
    """Asks for pictures (and sound) the way the renderer and mixer do. Returns the pictures that came."""
    got, end = [], time.monotonic() + seconds
    while time.monotonic() < end:
        frame = player.video_frame()
        if frame is not None:
            got.append(frame)
        block = player.read_audio(240)  # the mixer always reads: a file only ends once its sound is played too
        if audio is not None:
            audio.append(block)
        if until is not None and until(got):
            break
        time.sleep(0.005)
    return got


def test_ffmpeg_libraries_are_pyavs():
    paths = sprigcore.ffmpeg_libraries()
    assert paths is not None and len(paths) == 5
    for name, path in zip(sprigcore.FFMPEG_LIBRARIES, paths):
        assert name in Path(path).name and Path(path).is_file()


def test_a_video_plays_through_with_its_sound(player_lib, files):
    player = opened(files["video"])
    sound = []
    got = pump(player, 6, until=lambda _: player.info().state_name == "ended", audio=sound)
    info = player.info()
    assert info.state_name == "ended" and info.status_text == "ok"
    assert (info.width, info.height) == (320, 240) and info.has_video
    assert abs(info.duration - 2.0) < 0.1 and info.decoded == 60
    assert not info.thread  # played out: the thread closed the file and ended
    pictures = [g for g in got if not isinstance(g, str)]
    assert len(pictures) >= 50  # every picture, give or take a tick of this machine being slow
    pixels, order = pictures[0]
    assert order.startswith("I420/") and pixels.shape == (360, 320) and pixels.dtype == np.uint8
    loud = sum(1 for block in sound if np.abs(block).max() > 0.2)
    assert loud * 240 / 48000 > 1.5  # about two seconds of the tone
    assert all(block.shape == (240, 2) for block in sound)
    player.close()


def test_pictures_match_the_python_players(player_lib, files):
    """The first picture, byte for byte what sources/media.py packs from PyAV's decode of the same file."""
    from sprigcast.sources.media import pack_i420

    with av.open(files["video"]) as c:
        frame = next(c.decode(video=0))
        expected, order = pack_i420(frame, (320, 240))
    player = opened(files["video"])
    got = pump(player, 3, until=lambda g: len(g) >= 1)
    player.close()
    assert got and got[0][1] == order
    assert np.array_equal(got[0][0], expected)


def test_a_gif_loops_from_memory(player_lib, files):
    player = opened(files["gif"], loop=True)
    got = pump(player, 6, until=lambda _: player.info().passes >= 3)
    info = player.info()
    assert info.passes >= 3 and info.state_name == "playing"
    assert info.replaying and info.decoded == 10  # decoded once; the later passes came from memory
    pixels, order = got[0]
    assert order == "RGBA" and pixels.shape == (48, 64, 4)
    assert len(got) >= 20
    player.close()


def test_controls(player_lib, files):
    player = opened(files["video"])
    pump(player, 3, until=lambda g: len(g) >= 3)
    player.control(sprigcore.MEDIA_PAUSE)
    at = player.info().position
    assert pump(player, 0.3) == [] and player.info().state_name == "paused"
    assert abs(player.info().position - at) < 0.01  # the clock stands still
    player.control(sprigcore.MEDIA_SEEK, 1.5)
    player.control(sprigcore.MEDIA_START, 1)
    got = pump(player, 3, until=lambda g: len(g) >= 1)  # paused: the picture there shows, then it waits
    assert got and player.info().state_name == "paused" and abs(player.info().position - 1.5) < 0.05
    player.control(sprigcore.MEDIA_PLAY)
    pump(player, 3, until=lambda _: player.info().state_name == "ended")
    assert player.info().state_name == "ended"
    player.control(sprigcore.MEDIA_PLAY)  # from the start again
    player.control(sprigcore.MEDIA_START, 1)
    assert pump(player, 3, until=lambda g: len(g) >= 2) and player.info().position < 1.0
    player.control(sprigcore.MEDIA_STOP)
    assert player.video_frame() == "blank" and player.info().state_name == "stopped"
    player.close()


def test_sleep_and_wake(player_lib, files):
    player = opened(files["video"], restart_on_show=False)
    pump(player, 3, until=lambda _: player.info().position > 0.5)
    player.control(sprigcore.MEDIA_SLEEP)
    deadline = time.monotonic() + 3
    while player.info().thread and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not player.info().thread  # asleep, nothing is kept running
    at = player.info().position
    time.sleep(0.2)
    assert abs(player.info().position - at) < 0.01
    player.control(sprigcore.MEDIA_WAKE)
    got = pump(player, 3, until=lambda g: len(g) >= 2)
    assert got and player.info().position >= at - 0.05  # carried on from where it was
    player.close()


def test_shown_small_is_decoded_small(player_lib, files):
    player = opened(files["video"])
    player.set_shrink(0.5)
    got = pump(player, 3, until=lambda g: any(g_[0].shape == (180, 160) for g_ in g if not isinstance(g_, str)))
    assert any(g[0].shape == (180, 160) for g in got)  # 160 x 120 as I420
    assert (player.info().width, player.info().height) == (320, 240)  # its place in the layout stays
    player.close()


def test_faster_plays_in_less_time(player_lib, files):
    player = opened(files["video"], speed=2.0)
    began = time.monotonic()
    sound = []
    pump(player, 6, until=lambda _: player.info().state_name == "ended", audio=sound)
    assert 0.8 < time.monotonic() - began < 1.8
    assert sum(1 for block in sound if np.abs(block).max() > 0.2) > 60  # its sound came along
    player.close()


def test_a_sound_file_waits_for_play(player_lib, files):
    player = opened(files["sound"], video=False, playing=False)
    deadline = time.monotonic() + 3
    while player.info().status_text != "ok" and time.monotonic() < deadline:
        time.sleep(0.01)
    info = player.info()
    assert info.status_text == "ok" and info.state_name == "stopped" and not info.has_video
    assert abs(info.duration - 1.0) < 0.05 and not info.thread
    assert np.abs(player.read_audio(480)).max() == 0
    player.control(sprigcore.MEDIA_PLAY)
    player.control(sprigcore.MEDIA_START, 1)
    loud, deadline = 0, time.monotonic() + 4
    while player.info().state_name != "ended" and time.monotonic() < deadline:
        loud += int(np.abs(player.read_audio(240)).max() > 0.2)
        time.sleep(0.005)
    assert player.info().state_name == "ended" and loud * 240 / 48000 > 0.7
    player.close()


def test_a_see_through_webm_keeps_its_alpha(player_lib, files):
    if "webm" not in files:
        pytest.skip("this FFmpeg has no libvpx")
    player = opened(files["webm"])
    got = pump(player, 3, until=lambda g: len(g) >= 1)
    player.close()
    pixels, order = got[0]
    assert order.startswith("I420A/") and pixels.shape == (48 + 24 + 48, 64)
    alpha = pixels[72:]
    assert alpha[:, :28].min() > 200 and alpha[:, 36:].max() < 50


def test_files_that_cant_play_say_so(player_lib, files):
    missing = opened(str(files["folder"] / "nothing.mp4"))
    broken_file = files["folder"] / "broken.mp4"
    broken_file.write_bytes(b"this is not a video" * 100)
    broken = opened(str(broken_file))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not (missing.info().status_text.startswith("can't")
                                               and broken.info().status_text.startswith("can't")):
        time.sleep(0.01)
    assert missing.info().status_text == "can't play: file not found"
    assert broken.info().status_text.startswith("can't play")
    assert missing.video_frame() is None
    missing.close()
    broken.close()


@pytest.mark.parametrize("kind", [1, 2] if sys.platform == "win32" else [1])
def test_crash_gate_a_decoder_fault_ends_the_file_not_the_app(player_lib, files, kind):
    """kind 1 a C++ exception, kind 2 a write through a null pointer (Windows, where /EHa stops it)."""
    player = opened(files["video"], loop=True)
    pump(player, 3, until=lambda g: len(g) >= 2)
    player.debug_fault(kind)
    deadline = time.monotonic() + 3
    while not player.info().faulted and time.monotonic() < deadline:
        player.video_frame()
        time.sleep(0.01)
    info = player.info()
    assert info.faulted and not info.thread and info.status_text.startswith("can't play")
    player.control(sprigcore.MEDIA_PLAY)
    player.control(sprigcore.MEDIA_START, 1)
    assert not player.info().thread  # a faulted player starts nothing again
    player.close()
    other = opened(files["video"])  # and the next file plays
    assert pump(other, 3, until=lambda g: len(g) >= 1)
    other.close()


def test_closing_while_playing_ends_its_thread(player_lib, files):
    for _ in range(5):
        player = opened(files["video"], loop=True)
        pump(player, 1, until=lambda g: len(g) >= 1)
        player.close()
    with pytest.raises(sprigcore.NativeError):
        player.info()


def test_each_taker_gets_the_picture_on_screen_once(player_lib, files):
    """The renderer and the engine's compositor each remember what they took: a second taker gets the picture
    that is on screen at once, and nobody gets the same one twice."""
    player = opened(files["video"])
    try:
        first = pump(player, 3, until=lambda got: got)
        assert first
        player.control(sprigcore.MEDIA_PAUSE)
        pump(player, 0.1)
        assert player.video_frame() is None  # paused: nothing new for the one who has it
        player._seen.value = 0  # a newcomer (the engine's compositor, or Python's when it draws again)
        again = player.video_frame()
        assert again is not None and again[0].shape == first[-1][0].shape
        assert player.video_frame() is None
    finally:
        player.close()


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_the_engines_compositor_takes_pictures_straight_from_the_player(player_lib, files):
    """A video attached to the engine shows on its canvas with Python sending no picture at all, goes on showing
    after the player's handle is closed (the engine holds it), and forgetting the source lets it go."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render import native_bridge

    col = SceneCollection(
        canvases=[Canvas(id="h", width=320, height=240)],
        sources=[SourceConfig(id="clip", kind="media", name="Clip", settings={"path": files["video"]})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="clip", x=0, y=0, align="top_left")])],
        active_scenes={"h": "a"},
    )
    player = opened(files["video"], loop=True)
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        assert bridge.attach_media("clip", player)
        assert bridge.attach_media("clip", player)  # told once, asked every tick
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (320, 240), 0)

        def shown(seconds=5.0):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                player.read_audio(240) if player._handle else None
                got = bridge.take("h")
                if got is not None:
                    img = np.frombuffer(got[0], np.uint8).reshape(240, 320, 4)
                    if img[..., 3].min() == 255:
                        return img
                time.sleep(0.005)
            return None

        one = shown()
        assert one is not None and not bridge.failed  # the video fills the canvas, and Python sent nothing
        two, deadline = one, time.monotonic() + 5
        while (two == one).all() and time.monotonic() < deadline:
            two = shown()
        assert not (two == one).all()  # and it moves
        assert not bridge.attach_media("clip", None) and bridge.attach_media("clip", player)
        player.close()  # the source closed its player with the engine still attached
        time.sleep(0.2)
        assert not bridge.failed
        bridge.forget_gone([])
        time.sleep(0.2)
        assert not bridge.failed and "clip" not in bridge._media
    finally:
        player.close()
        bridge.close()


def test_a_stinger_clip_plays_through_the_engine(player_lib, files, monkeypatch, tmp_path):
    """render/stinger.py with SPRIGCAST_ENGINE=native: RGBA pictures with the clip's alpha, shrunk to the limit,
    sound from the first frame_at on, and a file that can't play gives an error instead of a picture."""
    from sprigcast.core.retire import RETIRER
    from sprigcast.render import stinger

    assert isinstance(stinger.open_stinger(files["video"]), stinger.StingerPlayer)  # the switch is off
    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    player = stinger.open_stinger(files["video"], (160, 160), 1.0)
    assert isinstance(player, stinger.NativeStinger)
    player.start()
    deadline = time.monotonic() + 5
    while not player.ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert player.ready.is_set() and player.error is None
    assert abs(player.duration - 2.0) < 0.1 and player.size == (160, 120)
    assert not np.abs(player.read_block(240)).any()  # silent until the transition shows its first frame
    frames, sound, began = [], 0, time.monotonic()
    while time.monotonic() - began < 1.0:
        frame = player.frame_at(time.monotonic() - began)
        if frame is not None:
            frames.append(frame)
        sound += int(np.abs(player.read_block(240)).max() > 0.2)
        time.sleep(0.005)
    assert 20 <= len(frames) <= 32 and sound > 50  # 30 pictures a second, by its own clock
    assert frames[0].shape == (120, 160, 4) and player.channel_order == "RGBA" and (frames[0][..., 3] == 255).all()
    player.close()
    player.close()
    assert player.frame_at(2.0) is None and not np.abs(player.read_block(240)).any()

    if "webm" in files:  # a see-through clip keeps its alpha
        clear = stinger.open_stinger(files["webm"])
        clear.start()
        deadline = time.monotonic() + 5
        while not clear.ready.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        frame = clear.frame_at(0.0)
        assert frame is not None and frame.shape == (48, 64, 4)
        assert frame[10, 10, 3] > 250 and frame[10, 50, 3] < 5 and frame[10, 10, 0] > 200
        clear.close()

    bad = tmp_path / "bad.webm"
    bad.write_bytes(b"not a video at all")
    broken = stinger.open_stinger(str(bad))
    broken.start()
    deadline = time.monotonic() + 5
    while not broken.ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert broken.ready.is_set() and broken.error
    broken.close()
    assert RETIRER.settle()


def test_an_instant_replay_plays_through_the_engine(player_lib, files, monkeypatch, tmp_path):
    """store/instant_replay.py with SPRIGCAST_ENGINE=native: B, G, R, A pictures fitted to the box (letterboxed),
    from skip_to on, at half speed with its sound slowed, ended once it has played out."""
    from sprigcast.core.retire import RETIRER
    from sprigcast.store import instant_replay as ir

    assert type(ir.open_player(Path(files["video"]), (200, 200))) is ir.Player  # the switch is off
    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    player = ir.open_player(Path(files["video"]), (200, 200), speed=0.5, skip_to=1.0).start()
    assert isinstance(player, ir.NativePlayer)
    assert player.wait_first(5) and player.error is None
    assert player.done and abs(player.length - 1.0) < 0.1  # all but the first second
    assert not np.abs(player.read(240)).any() and not player.finished()  # waits for the show's first picture
    pictures, sound, began = [], 0, time.monotonic()
    while not player.finished() and time.monotonic() - began < 6:
        picture = player.take((time.monotonic() - began) * 0.5)
        if picture is not None:
            pictures.append(picture)
        sound += int(np.abs(player.read(240)).max() > 0.05)
        time.sleep(0.005)
    spent = time.monotonic() - began
    assert player.finished() and 1.7 < spent < 3.0  # one second of replay at half speed
    assert 24 <= len(pictures) <= 31 and pictures[0].shape == (200, 200, 4) and pictures[0].flags.writeable
    assert tuple(pictures[0][0, 0]) == (ir.DEEP[2], ir.DEEP[1], ir.DEEP[0], 255)  # letterbox above the picture
    assert pictures[0][100, 100, 3] == 255 and sound > 150
    player.stop()
    player.stop()
    assert player.take(9.0) is None and player.finished()

    quiet = ir.open_player(Path(files["video"]), (320, 240), sound=False, skip_to=1.5).start()
    assert quiet.wait_first(5)
    began, shapes = time.monotonic(), set()
    while not quiet.finished() and time.monotonic() - began < 4:  # nobody reads its sound: it still ends
        picture = quiet.take(time.monotonic() - began)
        if picture is not None:
            shapes.add(picture.shape)
        time.sleep(0.005)
    assert quiet.finished() and shapes == {(240, 320, 4)}
    quiet.stop()

    bad = tmp_path / "bad.mkv"
    bad.write_bytes(b"not a video at all")
    broken = ir.open_player(bad, (200, 200)).start()
    assert not broken.wait_first(5) and broken.error
    broken.stop()
    assert RETIRER.settle()


def test_media_source_plays_through_the_engine(player_lib, files, monkeypatch):
    """sources/media.py with SPRIGCAST_ENGINE=native: the same class, the engine's player underneath."""
    from sprigcast.sources.media import MediaSource

    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    source = MediaSource("m1", {**MediaSource.defaults, "path": files["video"]})
    assert source._native is not None
    source.start()
    frames, sound, deadline = [], 0, time.monotonic() + 5
    while source.state != "ended" and time.monotonic() < deadline:
        frame = source.video_frame()
        if frame is not None:
            frames.append((frame.shape, source.channel_order))
        sound += int(np.abs(source.read_block(240)).max() > 0.2)
        time.sleep(0.005)
    assert source.state == "ended" and len(frames) >= 50 and sound > 100
    assert frames[0] == ((360, 320), frames[0][1]) and frames[0][1].startswith("I420/")
    assert source.size == (320, 240) and abs(source.duration - 2.0) < 0.1 and source.status == "ok"
    info = source.info()
    assert info["state"] == "ended" and info["video"] and abs(info["duration"] - 2.0) < 0.1
    source.seek(1.0)
    assert source.state == "playing"
    source.pause()
    assert source.state == "paused"
    source.stop_playing()
    blank = source.video_frame()
    assert blank is not None and blank.shape == (2, 2, 4) and not blank.any() and source.channel_order == "RGBA"
    source.update({**source.settings, "path": files["gif"], "loop": True})
    deadline = time.monotonic() + 5
    shapes = set()
    while time.monotonic() < deadline and len(shapes) < 1:
        frame = source.video_frame()
        if frame is not None and frame.shape != (2, 2, 4):
            shapes.add(frame.shape)
        time.sleep(0.005)
    assert shapes == {(48, 64, 4)} and source.size == (64, 48)
    source.sleep()
    source.asleep = True
    deadline = time.monotonic() + 3
    while source._native.info().thread and time.monotonic() < deadline:
        time.sleep(0.01)
    source.play()  # asleep: play starts no thread
    assert not source._native.info().thread
    source.asleep = False
    source.wake()
    source.stop()


def test_media_source_keeps_the_python_player_otherwise(player_lib, files, monkeypatch):
    from sprigcast.sources.media import MediaSource

    monkeypatch.delenv("SPRIGCAST_ENGINE", raising=False)
    assert MediaSource("m2", {**MediaSource.defaults, "path": files["video"]})._native is None


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_a_video_decoded_on_the_card_reaches_the_engine_and_python_alike(player_lib, files):
    """hw_decode with the engine attached: on a real card the pictures stay on the engine's device (media_card);
    on WARP (CI) they come in memory. Either way the canvas shows the video and it moves, and Python asking for
    pictures too (a projector) gets them, in memory."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render import native_bridge

    col = SceneCollection(
        canvases=[Canvas(id="h", width=320, height=240)],
        sources=[SourceConfig(id="clip", kind="media", name="Clip", settings={"path": files["video"]})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="clip", x=0, y=0, align="top_left")])],
        active_scenes={"h": "a"},
    )
    bridge = native_bridge.NativeCanvases(col, warp=True)
    player = sprigcore.MediaPlayer(playing=True)
    try:
        player.set_decode(True)
        player.set_options(True, False, True, 1.0)
        player.set_file(files["video"], True, True)
        player.control(sprigcore.MEDIA_START)
        assert bridge.attach_media("clip", player)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (320, 240), 0)

        def shown(seconds=5.0):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                player.read_audio(240)
                got = bridge.take("h")
                if got is not None:
                    img = np.frombuffer(got[0], np.uint8).reshape(240, 320, 4)
                    if img[..., 3].min() == 255:
                        return img
                time.sleep(0.005)
            return None

        one = shown()
        assert one is not None and not bridge.failed
        two, deadline = one, time.monotonic() + 5
        while (two == one).all() and time.monotonic() < deadline:
            two = shown()
        assert not (two == one).all() and not bridge.failed
        got, deadline = None, time.monotonic() + 5
        while got is None and time.monotonic() < deadline:
            got = player.video_frame()
            player.read_audio(240)
            time.sleep(0.005)
        assert got is not None and not bridge.failed  # Python's own reads still get pictures, in memory
    finally:
        player.close()
        bridge.close()


def test_decoding_on_the_graphics_card_is_the_engines_too(player_lib, files, monkeypatch):
    """hw_decode: the engine's player asks for the graphics card (Direct3D 11 on Windows) and falls back to the CPU
    where there is none (this container): the video plays either way, and turning it on or off reopens the file
    with the other decoder, keeping the same player."""
    from sprigcast.sources.media import MediaSource

    monkeypatch.setenv("SPRIGCAST_ENGINE", "native")
    on_card = MediaSource("m3", {**MediaSource.defaults, "path": files["video"], "hw_decode": True})
    assert on_card._native is not None
    on_card.start()
    frames, deadline = [], time.monotonic() + 5
    while on_card.state != "ended" and time.monotonic() < deadline:
        frame = on_card.video_frame()
        if frame is not None:
            frames.append(frame)
        on_card.read_block(240)
        time.sleep(0.005)
    assert on_card.state == "ended" and len(frames) >= 50 and on_card.status == "ok"
    hardware = on_card._native.info().hardware
    assert hardware in (0, 1) and frames[0].shape in ((360, 320), (240, 320, 4))
    on_card.stop()
    switched = MediaSource("m4", {**MediaSource.defaults, "path": files["video"]})
    player = switched._native
    assert player is not None and not player.info().hardware
    switched.update({**switched.settings, "hw_decode": True})
    assert switched._native is player
    switched.start()
    got, deadline = None, time.monotonic() + 5
    while got is None and time.monotonic() < deadline:
        got = switched.video_frame()
        switched.read_block(240)
        time.sleep(0.005)
    assert got is not None
    switched.stop()
