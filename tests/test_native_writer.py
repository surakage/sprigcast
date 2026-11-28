"""The engine's own writer (native/core/src/writer.cpp): NV12 pictures and stereo sound in, an encoded and muxed
file out, all on a thread of the engine's. Built with the media player on Linux (tests/test_native_media.py)."""

from __future__ import annotations

import time

import av
import numpy as np
import pytest

from sprigcast import sprigcore
from test_native_media import player_lib  # noqa: F401 (the fixture that builds or finds the library)

W, H = 64, 48


def nv12(level: int) -> np.ndarray:
    picture = np.full((H * 3 // 2, W), 128, np.uint8)
    picture[:H] = level
    return picture


def finished(writer, seconds=20.0):
    writer.finish()
    assert writer.wait(seconds)
    return writer.stats()


def test_a_recording_has_every_picture_and_its_sound(player_lib, tmp_path):  # noqa: F811
    path = tmp_path / "out.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_kbps=500, keyframe_frames=30,
                              video_options={"preset": "ultrafast"}, audio_tracks=2, queue_frames=120)
    try:
        tone = (0.2 * np.sin(np.arange(1600) / 10.0)).astype(np.float32)
        for n in range(60):
            assert writer.video(nv12(16 + 3 * n), n) == sprigcore.SC_OK
            for track in (0, 1):
                assert writer.audio(np.stack([tone, tone], axis=1), track) == sprigcore.SC_OK
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        assert stats.frames_written == 60 and stats.frames_dropped == 0 and stats.bytes > 0
        assert stats.encode_seconds > 0  # the stream report's "encode ms" (0.0 on every line before, PC 2026-11-20)
    finally:
        writer.close()
    with av.open(str(path)) as f:
        video = f.streams.video[0]
        assert (video.width, video.height) == (W, H)
        assert len(f.streams.audio) == 2
        frames = list(f.decode(video=0))
    assert len(frames) == 60
    lumas = [int(frame.to_ndarray()[:H].mean()) for frame in (frames[0], frames[-1])]  # the Y plane as written
    assert abs(lumas[0] - 16) <= 4 and abs(lumas[1] - (16 + 3 * 59)) <= 4  # the pictures, in order
    with av.open(str(path)) as f:
        seconds = sum(frame.samples for frame in f.decode(audio=0)) / 48000
    assert abs(seconds - 60 * 1600 / 48000) < 0.1  # the sound, all of it


def test_a_full_queue_drops_and_counts_and_a_bad_encoder_fails_without_harm(player_lib, tmp_path):  # noqa: F811
    writer = sprigcore.Writer(str(tmp_path / "slow.mp4"), "libx264", W, H, video_options={"preset": "veryslow"},
                              audio_tracks=0, queue_frames=2)
    try:
        results = [writer.video(nv12(100), n) for n in range(40)]
        assert sprigcore.WRITER_DROPPED in results
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        assert stats.frames_written + stats.frames_dropped == 40 and stats.frames_dropped > 0
    finally:
        writer.close()
    broken = sprigcore.Writer(str(tmp_path / "x.mp4"), "no_such_encoder", W, H)
    try:
        assert broken.wait(10)
        stats = broken.stats()
        assert stats.state_name == "failed" and "no_such_encoder" in stats.error_text
        assert broken.video(nv12(1), 0) == sprigcore.WRITER_REFUSED
    finally:
        broken.close()
    unwritable = sprigcore.Writer(str(tmp_path / "no" / "such" / "folder" / "x.mkv"), "libx264", W, H)
    try:
        assert unwritable.wait(10) and unwritable.stats().state_name == "failed"
    finally:
        unwritable.close()


def test_closing_without_finishing_gives_up_at_once(player_lib, tmp_path):  # noqa: F811
    writer = sprigcore.Writer(str(tmp_path / "gone.mkv"), "libx264", W, H, video_options={"preset": "ultrafast"})
    for n in range(5):
        writer.video(nv12(50), n)
    began = time.monotonic()
    writer.close()
    assert time.monotonic() - began < 6
    with pytest.raises(sprigcore.NativeError):
        writer.video(nv12(50), 6)


def test_track_titles_muxer_options_and_b_frames(player_lib, tmp_path):  # noqa: F811
    """What recordings ask of it: each sound track named, an MP4 in fragments (it survives a crash), B-frames."""
    tone = np.zeros((1600, 2), np.float32)
    for name, options in (("titled.mkv", None),
                          ("fragments.mp4", {"movflags": "+frag_keyframe+empty_moov+default_base_moof"})):
        path = tmp_path / name
        writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                                  audio_tracks=2, queue_frames=120, b_frames=2, track_titles=["Stream", "Mic only"],
                                  container_options=options)
        try:
            for n in range(30):
                assert writer.video(nv12(40 + n), n) == sprigcore.SC_OK
                for track in (0, 1):
                    writer.audio(tone, track)
            stats = finished(writer)
            assert stats.state_name == "finished", stats.error_text
        finally:
            writer.close()
        with av.open(str(path)) as f:
            if name.endswith(".mkv"):
                assert [s.metadata.get("title") for s in f.streams.audio] == ["Stream", "Mic only"]
            frames = list(f.decode(video=0))
        assert len(frames) == 30 and sorted(int(fr.pts) for fr in frames) == [int(fr.pts) for fr in frames]
    data = (tmp_path / "fragments.mp4").read_bytes()
    assert data[4:8] == b"ftyp" and b"moof" in data  # in fragments


def test_a_hole_is_filled_with_one_picture_and_frames_never_go_back(player_lib, tmp_path):  # noqa: F811
    path = tmp_path / "span.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=8)
    try:
        assert writer.stats().last_pts == -1
        assert writer.video(nv12(20), 0) == sprigcore.SC_OK
        assert writer.video_span(nv12(200), 1, 9) == sprigcore.SC_OK  # one queue place for nine frames
        assert writer.video(nv12(60), 5) == sprigcore.WRITER_LATE  # two feeds met: never back in time
        assert writer.video_span(nv12(90), 8, 12) == sprigcore.SC_OK  # overlaps: only 10 .. 12 are new
        stats = writer.stats()
        assert stats.last_pts == 12 and stats.frames_late == 1 and stats.frames_dropped == 0
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        assert stats.frames_written == 13 and stats.frames_repeated == 8 + 2
    finally:
        writer.close()
    with av.open(str(path)) as f:
        frames = list(f.decode(video=0))
    assert len(frames) == 13
    lumas = [int(frame.to_ndarray()[:H].mean()) for frame in frames]
    assert abs(lumas[0] - 20) <= 4 and all(abs(v - 200) <= 4 for v in lumas[1:10])
    assert all(abs(v - 90) <= 4 for v in lumas[10:])


def _next_output_frame(eng, within: float) -> bool:
    """Whether output slot 0 of canvas 0 hands over a frame within `within` seconds."""
    end = time.monotonic() + within
    while time.monotonic() < end:
        if eng.take_output(0, 0, 64, 36) is not None:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_an_output_linked_to_a_writer_is_written_without_python(player_lib, tmp_path):  # noqa: F811
    """Phase 4: the frame thread hands each output frame to the engine's own writer itself, numbered by when it was
    drawn; nothing comes back to Python unless asked (keep), and a writer let go never harms the engine."""
    assert sprigcore.media_ready()
    path = tmp_path / "linked.mkv"
    writer = sprigcore.Writer(str(path), "libx264", 64, 36, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=30)
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        eng.set_canvas(0, 64, 36)
        eng.set_output(0, 0, 64, 36)
        eng.set_output(0, 1, 32, 18)
        with pytest.raises(sprigcore.NativeError):
            eng.link_output(0, 1, writer, time.perf_counter(), 30)  # not the writer's size
        start = time.perf_counter()
        eng.link_output(0, 0, writer, start, 30, fill_seconds=5.0)
        time.sleep(1.5)
        assert eng.take_output(0, 0, 64, 36) is None  # the writer had them all
        eng.link_output(0, 0, writer, start, 30, keep=True, fill_seconds=5.0)  # the same writer: numbering goes on
        assert _next_output_frame(eng, 5.0)
        last, linked = eng.unlink_output(0, 0)
        assert linked and last >= 25
        assert eng.unlink_output(0, 0) == (-1, False)
        writer.finish()
        assert writer.wait(20)
        stats = writer.stats()
        assert stats.state_name == "finished", stats.error_text
        assert stats.last_pts == last and last - 2 <= stats.frames_written <= last + 1  # from frame 0 or 1, no holes
        # A writer let go while linked: the link ends by itself, the engine carries on.
        other = sprigcore.Writer(str(tmp_path / "gone.mkv"), "libx264", 64, 36, fps=30, audio_tracks=0,
                                 video_options={"preset": "ultrafast"})
        eng.link_output(0, 0, other, time.perf_counter(), 30)
        time.sleep(0.3)
        other.close()
        time.sleep(0.5)
        assert _next_output_frame(eng, 5.0)  # the link ended: frames come back again
        assert eng.unlink_output(0, 0)[1] is False
        assert not eng.stats()["faulted"]
    writer.close()
    with av.open(str(path)) as f:
        assert len(list(f.decode(video=0))) == stats.frames_written


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_an_output_linked_to_the_virtual_camera_is_written_into_its_block():
    """Phase 4: the frame thread writes the camera's frames into its shared block itself, in the middle of the
    camera's frame (black around), with the count even when done; once unlinked it writes nothing more."""
    import ctypes
    import sys

    from sprigcast.output.virtualcam import BLOCK_SIZE, HEADER, MAGIC, SharedBlock

    if sys.platform != "win32":
        pytest.skip("Windows' shared memory")
    k32 = ctypes.windll.kernel32
    k32.CreateFileMappingW.restype = ctypes.c_void_p
    k32.CreateFileMappingW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                       ctypes.c_uint32, ctypes.c_wchar_p]
    k32.MapViewOfFile.restype = ctypes.c_void_p
    k32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t]
    k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = f"Local\\SprigCastCameraTest{time.monotonic_ns()}"
    handle = k32.CreateFileMappingW(ctypes.c_void_p(-1), None, 0x04, 0, BLOCK_SIZE, name)  # PAGE_READWRITE
    assert handle
    view = k32.MapViewOfFile(handle, 0x0002 | 0x0004, 0, 0, 0)
    assert view
    block = SharedBlock(view, BLOCK_SIZE, name=name)
    ctypes.c_uint32.from_address(view).value = MAGIC
    try:
        with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
            eng.set_canvas(0, 64, 36)
            eng.set_output(0, 0, 64, 36)
            with pytest.raises(sprigcore.NativeError):
                eng.link_camera(0, 0, name, 32, 18)  # smaller than the output
            with pytest.raises(sprigcore.NativeError):
                eng.link_camera(0, 0, "Local\\NoSuchSprigCastCamera", 64, 64)
            eng.link_camera(0, 0, name, 64, 64)
            end = time.monotonic() + 5
            while eng.camera_frames(0, 0) < 5 and time.monotonic() < end:
                time.sleep(0.02)
            assert eng.camera_frames(0, 0) >= 5
            assert eng.take_output(0, 0, 64, 36) is None  # the camera had them all
            _last, linked = eng.unlink_output(0, 0)
            assert linked and eng.camera_frames(0, 0) == 0
            count = block._u64(24).value
            assert count % 2 == 0 and count >= 10 and block._u32(16).value == 64 and block._u32(20).value == 64
            time.sleep(0.3)
            assert block._u64(24).value == count  # nothing written once unlinked
            picture = ctypes.string_at(view + HEADER, 64 * 64 * 3 // 2)
            assert picture[0] == 16 and picture[64 * 13] == 16  # the black bar above the picture (rows 0-13)
            assert picture[64 * 64] == 128  # its colour
            assert not eng.stats()["faulted"]
    finally:
        block.closed = True
        k32.UnmapViewOfFile(view)
        k32.CloseHandle(handle)


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_frames_on_the_card_go_only_to_a_writer_on_the_engines_device(player_lib, tmp_path):  # noqa: F811
    """Phase 4 step 4: an output whose frames stay on the card links only to a writer whose encoder is on the
    engine's device; one whose encoder can't take them there fails to open cleanly (the recording then goes
    through PyAV), and the engine carries on."""
    assert sprigcore.media_ready()
    plain = sprigcore.Writer(str(tmp_path / "plain.mkv"), "libx264", 64, 36, fps=30, audio_tracks=0,
                             video_options={"preset": "ultrafast"})
    with sprigcore.Engine(warp=True, fps=(30, 1)) as eng:
        eng.set_canvas(0, 64, 36)
        eng.set_output(0, 0, 64, 36, share=True)
        with pytest.raises(sprigcore.NativeError):
            eng.link_output(0, 0, plain, time.perf_counter(), 30)  # its encoder takes pictures in memory
        device, _rebuilds = eng.device()
        assert device
        try:
            card = sprigcore.Writer(str(tmp_path / "card.mkv"), "libx264", 64, 36, fps=30, audio_tracks=0,
                                    device=device)
        finally:
            sprigcore.com_release(device)
        assert card.on_card
        deadline = time.monotonic() + 10
        while card.stats().state_name == "opening" and time.monotonic() < deadline:
            time.sleep(0.01)
        # WARP has no video device for FFmpeg's D3D11 context, and x264 takes no frames on the card anyway.
        assert card.stats().state_name == "failed"
        assert "card" in card.stats().error_text
        with pytest.raises(sprigcore.NativeError):
            eng.link_output(0, 0, card, time.perf_counter(), 30, keep=True)  # others would share them
        card.close()
        assert not eng.stats()["faulted"]
    plain.close()


def test_a_video_encoder_that_fails_once_starts_again_and_the_file_goes_on(player_lib, tmp_path, monkeypatch):  # noqa: F811
    """A graphics driver reset breaks the encoder mid-recording: the old one is let go of entirely and a new one
    carries on from memory into the same file, nothing lost. Once only: a second failure ends the file cleanly."""
    monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", "20")
    path = tmp_path / "goes_on.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=1, queue_frames=120)
    try:
        for n in range(60):
            assert writer.video(nv12(16 + 3 * n), n) == sprigcore.SC_OK
            writer.audio(np.zeros((1600, 2), np.float32))
        stats = finished(writer)
    finally:
        writer.close()
    assert stats.state_name == "finished", stats.error_text
    assert stats.recoveries == 1 and stats.on_card == 0 and stats.frames_written == 60
    with av.open(str(path)) as f:
        lumas = [int(frame.to_ndarray()[:H].mean()) for frame in f.decode(video=0)]
    assert len(lumas) >= 59 and abs(lumas[-1] - (16 + 3 * 59)) <= 4  # the pictures after the failure are there

    monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", "10,30")
    path = tmp_path / "twice.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=120)
    try:
        for n in range(60):
            writer.video(nv12(100), n)
        stats = finished(writer)
    finally:
        writer.close()
    assert stats.state_name == "failed" and "deliberate fault" in stats.error_text and stats.recoveries == 1
    with av.open(str(path)) as f:  # ended cleanly: what was written before plays
        assert 25 <= sum(1 for _ in f.decode(video=0)) <= 31


@pytest.mark.parametrize("fault", ["", "40"])
def test_packets_mode_encodes_and_hands_the_packets_over(player_lib, tmp_path, monkeypatch, fault):  # noqa: F811
    """Packets mode: the engine encodes, the caller muxes (a stream, clips, a recording from the same packets). The
    header file's streams carry the codec headers; a keyframe asked for comes next; the bitrate can change. An
    encoder started again after a failure (fault) puts its headers in its keyframes: the packets still play."""
    if fault:
        monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", fault)
    header = tmp_path / "streams.nut"
    writer = sprigcore.Writer(str(header), "libx264", W, H, fps=30,
                              video_options={"preset": "ultrafast", "forced-idr": "1"},
                              keyframe_frames=300, audio_tracks=2, queue_frames=120, packets=True)
    packets = []
    try:
        for n in range(60):
            if n == 30:
                writer.keyframe()
                writer.set_bitrate(300, 300, 600)
            assert writer.video(nv12(16 + 3 * n), n) == sprigcore.SC_OK
            for track in (0, 1):
                writer.audio(np.zeros((1600, 2), np.float32), track)
            while (p := writer.take_packet()) is not None:
                packets.append(p)
        stats = finished(writer)
        while (p := writer.take_packet()) is not None:
            packets.append(p)
    finally:
        writer.close()
    assert stats.state_name == "finished", stats.error_text
    assert stats.recoveries == (1 if fault else 0)
    video = [p for p in packets if p[0] == 0]
    assert len(video) == 60 and {p[0] for p in packets} == {0, 1, 2}
    keys = [p[2] for p in video if p[1]]
    assert keys[0] == 0 and 30 in keys  # the first picture, and the one asked for
    if fault:
        assert 40 in keys  # the new encoder starts on a keyframe
    assert stats.bytes == sum(len(p[5]) for p in packets)
    from fractions import Fraction

    path = tmp_path / "muxed.mkv"
    with av.open(str(header)) as probe, av.open(str(path), "w") as out:
        assert probe.streams.video[0].codec_context.extradata and len(probe.streams.audio) == 2
        streams = [out.add_stream_from_template(s) for s in (probe.streams.video[0], *probe.streams.audio)]
        for track, key, pts, dts, tb, data in packets:
            packet = av.Packet(data)
            packet.pts, packet.dts, packet.time_base = pts, dts, Fraction(*tb)
            packet.is_keyframe = key
            packet.stream = streams[track]
            out.mux(packet)
    with av.open(str(path)) as f:
        lumas = [int(frame.to_ndarray()[:H].mean()) for frame in f.decode(video=0)]
    assert len(lumas) == 60 and abs(lumas[59] - (16 + 3 * 59)) <= 4


def test_frames_keep_coming_while_the_encoder_opens_again(player_lib, tmp_path, monkeypatch, caplog):  # noqa: F811
    """A graphics reset: the encoder takes about a second to open again (PC 2026-11-18: 1.03 s missing from both
    files). The queue fills meanwhile; the newest picture stands for the frames that come, so none is lost."""
    monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", "10")
    monkeypatch.setenv("SPRIGCAST_WRITER_REOPEN_MS", "700")
    caplog.set_level("INFO")
    path = tmp_path / "held.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=8)
    try:
        for n in range(60):
            assert writer.video(nv12(16 + 3 * n), n) == sprigcore.SC_OK  # never SC_WRITER_DROPPED
            time.sleep(1 / 30)
        stats = finished(writer)
    finally:
        writer.close()
    assert stats.state_name == "finished", stats.error_text
    # While it opens, more pictures than queue_frames wait (PC 2026-11-18: a 0.4 s still before): every frame is
    # its own picture, none a repeat.
    assert stats.recoveries == 1 and stats.frames_dropped == 0 and stats.frames_repeated == 0
    assert stats.frames_written == 60
    with av.open(str(path)) as f:
        lumas = [int(frame.to_ndarray()[:H].mean()) for frame in f.decode(video=0)]
    assert len(lumas) >= 59 and all(b > a for a, b in zip(lumas[12:], lumas[13:]))  # moving through the reopen
    sprigcore.drain_log()
    assert any("goes on from memory (opened again in" in r.getMessage() and "while the old one finished in"
               in r.getMessage() for r in caplog.records)


def test_a_very_long_reopen_still_holds_the_newest_picture(player_lib, tmp_path, monkeypatch):  # noqa: F811
    """Past the reopen room (90 pictures at most), the newest picture stands for the frames that come, as before:
    a short still, never a hole."""
    monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", "5")
    monkeypatch.setenv("SPRIGCAST_WRITER_REOPEN_MS", "2000")
    path = tmp_path / "long.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=60, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=8)
    try:
        for n in range(150):
            assert writer.video(nv12(16 + n), n) == sprigcore.SC_OK
            time.sleep(1 / 60)
        stats = finished(writer)
    finally:
        writer.close()
    assert stats.state_name == "finished", stats.error_text
    assert stats.recoveries == 1 and stats.frames_dropped == 0 and stats.frames_repeated > 0
    assert stats.frames_written == 150


def test_what_waited_is_written_without_dropping_new_frames(player_lib, tmp_path, monkeypatch):  # noqa: F811
    """After the reopen the encoder writes the pictures that waited while new ones keep coming: the extra room stays
    until it has caught up (PC 2026-11-18: 25 frames dropped right after a 0.9 s reopen)."""
    monkeypatch.setenv("SPRIGCAST_WRITER_FAULT", "10")
    monkeypatch.setenv("SPRIGCAST_WRITER_REOPEN_MS", "700")
    monkeypatch.setenv("SPRIGCAST_WRITER_SLOW_MS", "20")  # 50 a second against 30 coming: catches up in about 1 s
    path = tmp_path / "caught.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_options={"preset": "ultrafast"},
                              audio_tracks=0, queue_frames=8)
    try:
        for n in range(90):
            assert writer.video(nv12(16 + 2 * n), n) == sprigcore.SC_OK
            time.sleep(1 / 30)
        stats = finished(writer)
    finally:
        writer.close()
    assert stats.state_name == "finished", stats.error_text
    assert stats.recoveries == 1 and stats.frames_dropped == 0 and stats.frames_repeated == 0
    assert stats.frames_written == 90


def _fed(writer, frames, start=0, pace=0.0):
    for n in range(start, start + frames):
        assert writer.video(nv12(16 + (3 * n) % 200), n) == sprigcore.SC_OK
        writer.audio(np.zeros((1600, 2), np.float32), 0)
        while writer.take_packet() is not None:
            pass
        if pace:
            time.sleep(pace)


def _until(test, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not test():
        assert time.monotonic() < deadline, "it never happened"
        time.sleep(0.01)


def test_a_link_sends_the_writers_packets_without_python(player_lib, tmp_path):  # noqa: F811
    """A stream sent by the engine (sc_link): it connects by itself, asks for a keyframe and starts there with times
    from zero; sound from before that keyframe is left out; finish() sends what waits and closes properly."""
    writer = sprigcore.Writer(str(tmp_path / "streams.nut"), "libx264", W, H, fps=30,
                              video_options={"preset": "ultrafast", "forced-idr": "1"}, video_kbps=300,
                              keyframe_frames=300, audio_tracks=2, queue_frames=120, packets=True)
    sent = tmp_path / "sent.flv"
    link = None
    try:
        _fed(writer, 20)  # before anyone is connected: never sent
        _until(lambda: writer.stats().frames_written >= 20)
        link = writer.link(str(sent), 300)
        _until(lambda: link.stats().state_name == "ready")
        _fed(writer, 40, start=20)
        _until(lambda: link.stats().frames >= 39)  # the encoder may still hold the newest picture
        stats = link.stats()
        assert stats.state_name == "live" and stats.dropped == 0 and stats.bytes > 0
        assert abs(stats.first_seconds - 20 / 30) < 1e-6  # the keyframe it asked for was the next picture
        link.finish()
        _until(lambda: link.stats().state_name == "finished")
        assert writer.stats().state_name == "running"  # the encoder goes on (clips, other places)
    finally:
        if link is not None:
            link.close()
        writer.close()
    with av.open(str(sent)) as f:
        assert len(f.streams.audio) == 1
        video = list(f.demux(video=0))
        video = [p for p in video if p.size]
        assert len(video) >= 39 and video[0].is_keyframe and video[0].pts == 0
        sound = [p for p in f.demux(audio=0) if p.size] if f.seek(0) is None else []
        assert sound and min(p.pts for p in sound) >= 0
    with av.open(str(sent)) as f:
        lumas = [int(frame.to_ndarray()[:H].mean()) for frame in f.decode(video=0)]
    assert len(lumas) >= 39 and abs(lumas[0] - (16 + 60)) <= 4


def test_a_link_that_cant_connect_or_loses_its_writer_fails_and_says_why(player_lib, tmp_path):  # noqa: F811
    writer = sprigcore.Writer(str(tmp_path / "streams.nut"), "libx264", W, H, fps=30,
                              video_options={"preset": "ultrafast"}, audio_tracks=1, queue_frames=120, packets=True)
    try:
        nowhere = writer.link(str(tmp_path / "no-such-folder" / "sent.flv"), 300)
        _until(lambda: nowhere.stats().state_name == "failed")
        assert "couldn't connect" in nowhere.stats().error_text
        assert "no-such-folder" not in nowhere.stats().error_text  # an address holds the stream key
        nowhere.close()
        link = writer.link(str(tmp_path / "sent.flv"), 300)
        _until(lambda: link.stats().state_name == "ready")
        _fed(writer, 10)
    finally:
        writer.close()
    _until(lambda: link.stats().state_name == "failed")
    assert "encoder stopped" in link.stats().error_text
    link.close()
    assert link.stats().state_name == "failed"  # closed: still safe to ask


def test_a_link_behind_skips_until_a_keyframe_and_a_delay_holds_packets(player_lib, tmp_path, monkeypatch):  # noqa: F811
    """A stream delay holds every packet that long; a link with more than max_backlog waiting skips pictures (and,
    alone on its writer, asks for a keyframe to come back at)."""
    writer = sprigcore.Writer(str(tmp_path / "streams.nut"), "libx264", W, H, fps=30,
                              video_options={"preset": "ultrafast", "forced-idr": "1"}, video_kbps=300,
                              keyframe_frames=3000, audio_tracks=1, queue_frames=120, packets=True)
    delayed = late = None
    try:
        delayed = writer.link(str(tmp_path / "delayed.flv"), 300, delay_s=1.0)
        _until(lambda: delayed.stats().state_name == "ready")
        began = time.monotonic()
        _fed(writer, 30)
        _until(lambda: delayed.stats().delayed > 0)
        assert delayed.stats().frames == 0 and delayed.stats().backlog_bytes == 0
        _until(lambda: delayed.stats().frames >= 29)
        assert time.monotonic() - began >= 0.95
        delayed.close()
        delayed = None
        # kbps 1: any packet waiting is "seconds" of backlog, and each takes 40 ms to send (a slow upload), so
        # packets offered meanwhile find it behind
        monkeypatch.setenv("SPRIGCAST_LINK_SLOW_MS", "40")
        late = writer.link(str(tmp_path / "late.flv"), 1, delay_s=0, max_backlog_s=0.001, resume_backlog_s=1e9)
        _until(lambda: late.stats().state_name == "ready")
        _fed(writer, 60, start=30, pace=0.005)
        _until(lambda: writer.stats().frames_written >= 90)
        _until(lambda: late.stats().backlog_bytes == 0)
        stats = late.stats()
        assert stats.dropped > 0 and stats.frames >= 1 and stats.frames + stats.dropped <= 60
    finally:
        for link in (delayed, late):
            if link is not None:
                link.close()
        writer.close()


def _packets_writer(tmp_path, tracks=1, keyframes=3000):
    return sprigcore.Writer(str(tmp_path / "streams.nut"), "libx264", W, H, fps=30,
                            video_options={"preset": "ultrafast", "forced-idr": "1"}, video_kbps=300,
                            keyframe_frames=keyframes, audio_tracks=tracks, queue_frames=120, packets=True,
                            track_titles=["Game", "Voice", "Clips"][:tracks] if tracks > 1 else None)


def _fed_tracks(writer, frames, start, tracks):
    for n in range(start, start + frames):
        assert writer.video(nv12(16 + (3 * n) % 200), n) == sprigcore.SC_OK
        for t in range(tracks):
            writer.audio(np.zeros((1600, 2), np.float32), t)
        while writer.take_packet() is not None:
            pass


def _lumas(path):
    with av.open(str(path)) as f:
        return [int(frame.to_ndarray()[:H].mean()) for frame in f.decode(video=0)]


def test_the_engine_writes_a_recording_from_the_writers_packets(player_lib, tmp_path):  # noqa: F811
    """sc_file: the file starts at a keyframe it asks for (times from zero), keeps the tracks asked for with their
    titles, pauses and resumes as if no time passed, gets its chapters, and ends just before a keyframe."""
    writer = _packets_writer(tmp_path, tracks=3)
    path = tmp_path / "rec.mkv"
    file = None
    try:
        _fed_tracks(writer, 20, 0, 3)  # before the recording: not in the file
        _until(lambda: writer.stats().frames_written >= 20)
        file = writer.file(path, "matroska", tracks=(0, 1))
        assert file.stats().state_name == "waiting" and file.chapter("too early") is None
        _fed_tracks(writer, 30, 20, 3)
        _until(lambda: file.stats().seconds >= 28 / 30)
        assert file.stats().state_name == "writing" and file.stats().parts == 1
        assert abs(file.chapter("Boss fight") - file.stats().seconds) < 1e-9
        assert file.pause() and not file.pause() and file.stats().state_name == "paused"
        _fed_tracks(writer, 30, 50, 3)  # paused: left out
        _until(lambda: writer.stats().frames_written >= 79)
        assert file.resume() and not file.resume()
        _fed_tracks(writer, 30, 80, 3)
        _until(lambda: file.stats().seconds >= 55 / 30)
        file.finish()
        _fed_tracks(writer, 5, 110, 3)  # the keyframe it ends before
        _until(lambda: file.stats().state_name == "finished")
        parts = file.parts()
        file.close()
        assert file.parts() == parts and file.stats().state_name == "finished"  # closed: still safe to ask
    finally:
        if file is not None:
            file.close()
        writer.close()
    assert [p[0] for p in parts] == [str(path)]
    assert [c["metadata"]["title"] for c in parts[0][2]] == ["Start", "Boss fight"]
    with av.open(str(path)) as f:
        assert [s.metadata.get("title") for s in f.streams.audio] == ["Game", "Voice"]
        assert [c["metadata"]["title"] for c in f.chapters()] == ["Start", "Boss fight"]
        stamps = [p.pts * p.time_base for p in f.demux(video=0) if p.size]
    assert stamps[0] == 0 and 56 <= len(stamps) <= 60
    gaps = [float(b - a) for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 0.05  # the pause left no hole
    lumas = _lumas(path)
    assert abs(lumas[0] - (16 + 60)) <= 4 and abs(lumas[30] - (16 + (3 * 80) % 200)) <= 4


def test_a_recording_splits_into_parts_and_closes_what_there_is(player_lib, tmp_path):  # noqa: F811
    writer = _packets_writer(tmp_path, keyframes=15)
    path = tmp_path / "long take.mp4"
    file = None
    try:
        file = writer.file(path, "mp4", {"movflags": "frag_keyframe+empty_moov+default_base_moof"},
                           split_seconds=1.0)
        _fed_tracks(writer, 75, 0, 1)
        _until(lambda: file.stats().parts == 3)
        file.close()  # no keyframe waited for: what there is
        parts = file.parts()
    finally:
        if file is not None:
            file.close()
        writer.close()
    assert [p[0] for p in parts] == [str(path), str(tmp_path / "long take part 2.mp4"),
                                     str(tmp_path / "long take part 3.mp4")]
    counts = [len(_lumas(p[0])) for p in parts]
    assert counts[:2] == [30, 30] and 10 <= counts[2] <= 15 and abs(parts[0][1] - 1.0) < 0.01


def test_a_recording_that_cant_be_written_fails_alone_and_one_outlives_its_writer(player_lib, tmp_path):  # noqa: F811
    writer = _packets_writer(tmp_path)
    kept = None
    try:
        lost = writer.file(tmp_path / "no-such-folder" / "rec.mkv", "matroska")
        kept = writer.file(tmp_path / "kept.mkv", "matroska")
        _fed_tracks(writer, 30, 0, 1)
        _until(lambda: lost.stats().state_name == "failed")
        assert "opening the file" in lost.stats().error_text
        lost.close()
        _until(lambda: kept.stats().seconds >= 0.9)
        assert writer.stats().state_name == "running"
        finished(writer)
    finally:
        writer.close()
    _until(lambda: kept.stats().state_name == "finished")  # the writer ended: the file is closed properly
    kept.close()
    assert len(_lumas(tmp_path / "kept.mkv")) == 30


def test_the_engine_keeps_the_last_seconds_and_saves_a_clip(player_lib, tmp_path):  # noqa: F811
    """sc_writer_keep: the last seconds stay (from a keyframe, in a ring file), a clip taken now is written later
    from them with times from zero, and with nobody taking packets none are queued."""
    writer = _packets_writer(tmp_path, tracks=2, keyframes=15)
    ring = tmp_path / "ring.bin"
    try:
        with pytest.raises(sprigcore.NativeError, match="nothing buffered"):
            writer.clip_snapshot()
        writer.keep(1.0, ring)
        writer.hand_out(False)
        for n in range(120):
            assert writer.video(nv12(16 + (3 * n) % 200), n) == sprigcore.SC_OK
            for t in range(2):
                writer.audio(np.zeros((1600, 2), np.float32), t)
        _until(lambda: writer.stats().frames_written >= 119)
        assert writer.take_packet() is None and writer.stats().state_name == "running"
        seconds, packets = writer.kept()
        assert 1.0 <= seconds <= 1.5 + 1 / 30 and packets < 250 and ring.exists()
        clip = writer.clip_snapshot()
        for n in range(120, 150):  # the clip was taken: what comes after isn't in it
            writer.video(nv12(16), n)
        clip.write(tmp_path / "clip.mkv", "matroska", tracks=(1,))
        clip.write(tmp_path / "again.mp4", "mp4")
        with pytest.raises(sprigcore.NativeError, match="opening the file"):
            clip.write(tmp_path / "no-such-folder" / "clip.mkv", "matroska")
        clip.close()
        writer.keep(0)
        writer.video(nv12(16), 150)
        _until(lambda: writer.kept() == (0.0, 0))
    finally:
        writer.close()
    assert not ring.exists()  # removed with the writer
    lumas = _lumas(tmp_path / "clip.mkv")
    assert 30 <= len(lumas) <= 46 and abs(len(_lumas(tmp_path / "again.mp4")) - len(lumas)) <= 1
    first = 119 - len(lumas)  # the newest picture may still have been inside the encoder
    assert any(abs(lumas[0] - (16 + (3 * n) % 200)) <= 4 for n in (first, first + 1))
    with av.open(str(tmp_path / "clip.mkv")) as f:
        assert len(f.streams.audio) == 1 and next(p.pts for p in f.demux(video=0)) == 0


def test_clips_stay_in_memory_when_the_ring_cant_be_made(player_lib, tmp_path):  # noqa: F811
    writer = _packets_writer(tmp_path, keyframes=15)
    try:
        writer.keep(1.0, tmp_path / "no-such-folder" / "ring.bin")
        _fed_tracks(writer, 60, 0, 1)
        _until(lambda: writer.kept()[0] >= 1.0)
        writer.clip_snapshot().write(tmp_path / "clip.mkv", "matroska")
    finally:
        writer.close()
    assert len(_lumas(tmp_path / "clip.mkv")) >= 30


def _loud_chunks(path):
    """Per 1024 samples of the file's first sound track: is it loud?"""
    with av.open(str(path)) as f:
        sound = np.concatenate([frame.to_ndarray().reshape(frame.to_ndarray().shape[0], -1).mean(axis=0)
                                if frame.format.is_planar else
                                frame.to_ndarray().reshape(-1, 2).mean(axis=1) for frame in f.decode(audio=0)])
    chunks = [sound[i:i + 1024] for i in range(0, len(sound) - 1023, 1024)]
    return [float(np.sqrt(np.mean(c * c))) > 0.1 for c in chunks]


def test_the_mixer_feeds_a_writers_sound_itself_in_order_and_a_pause_leaves_sound_out(player_lib, tmp_path):  # noqa: F811
    path = tmp_path / "heard.mkv"
    writer = sprigcore.Writer(str(path), "libx264", W, H, fps=30, video_kbps=500, keyframe_frames=30,
                              video_options={"preset": "ultrafast"}, audio_tracks=2, queue_frames=120)
    mixer = sprigcore.SoundMixer(clocked=False)
    block = sprigcore.MIXER_BLOCK
    wave = (0.5 * np.sin(np.arange(block) * 2 * np.pi * 750 / 48000)).astype(np.float32)  # whole cycles a block
    loud, quiet = np.stack([wave, wave], axis=1), np.zeros((block, 2), np.float32)
    try:
        source = mixer.add_source(gain=1.0, tracks=("stream",))
        tap = mixer.add_tap([0])  # one mix: into both of the writer's tracks

        def play(what, blocks):
            for _ in range(blocks):
                mixer.push(source, what)
                mixer.tick(1)

        for n in range(60):
            assert writer.video(nv12(100), n) == sprigcore.SC_OK
        play(quiet, 10)  # taken and written by the caller, as Python did
        while (got := mixer.take(tap, 1)) is not None:
            for track in (0, 1):
                assert writer.audio(got[1][0], track) == sprigcore.SC_OK
        play(loud, 1)  # mixed between the last take and the switch: still the tap's
        mixer.tap_writer(tap, writer)
        assert mixer.take(tap, 1) is None  # the writer's from here
        play(loud, 9)  # waits: the caller may still be writing what it took
        mixer.tap_go(tap)
        play(quiet, 10)  # straight into the writer
        mixer.tap_pause(tap, True)
        play(loud, 10)  # a paused recording: left out
        mixer.tap_pause(tap, False)
        play(quiet, 10)
        stats = finished(writer)
        assert stats.state_name == "finished", stats.error_text
        play(loud, 3)  # a finished writer refuses: no harm
    finally:
        writer.close()
        play(loud, 2)  # and one closed under the tap is only refused (the tap keeps its encoder alive)
        mixer.remove_tap(tap)
        mixer.tap_pause(tap, True)  # an output stopping after its tap went: nothing to do, and nothing logged
        mixer.close()
    chunks = _loud_chunks(path)
    assert 38 <= len(chunks) <= 42, len(chunks)  # 40 blocks: the paused ones never arrived
    first = chunks.index(True)
    run = sum(chunks)
    assert 9 <= first <= 12 and 9 <= run <= 11, chunks  # pushed sound plays a block later
    assert all(chunks[first:first + run]), chunks  # one run: nothing changed places
    both = []
    for i in (0, 1):
        with av.open(str(path)) as f:
            both.append(sum(frame.samples for frame in f.decode(audio=i)))
    assert both[0] == both[1]


@pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")
def test_a_canvas_is_drawn_only_as_often_as_its_places_take_frames(player_lib, tmp_path):  # noqa: F811
    """CRGPlays 2026-11-27: the vertical canvas at 60 fps fed only TikTok at 30, and was drawn 60 times a second
    anyway, on a laptop whose graphics card was at 100%. A writer at 30 on a 60 fps canvas: drawn about 30 times a
    second, with every one of its frames still there."""
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig
    from sprigcast.render import native_bridge

    assert sprigcore.media_ready()
    col = SceneCollection(
        canvases=[Canvas(id="h", width=64, height=36, fps=60)],
        sources=[SourceConfig(id="pic", kind="color", name="Picture")],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="pic")])],
        active_scenes={"h": "a"},
    )
    bridge = native_bridge.NativeCanvases(col, warp=True)
    frame = np.zeros((36, 64, 4), np.uint8)
    writer = sprigcore.Writer(str(tmp_path / "half.mkv"), "libx264", 64, 36, fps=30,
                              video_options={"preset": "ultrafast"}, audio_tracks=0, queue_frames=60)

    def span(seconds):  # a new picture every few ms, as a camera sends them: nothing is kept as it was
        before = bridge.engine.stats()
        end = time.perf_counter() + seconds
        n = 0
        while time.perf_counter() < end:
            n += 1
            frame[:, :, 0] = n % 255
            bridge.upload("pic", frame, "RGBA", (64, 36))
            time.sleep(0.004)
        after = bridge.engine.stats()
        return after["ticks"] - before["ticks"], after["canvas_draws"] - before["canvas_draws"]

    try:
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.upload("pic", frame, "RGBA", (64, 36))
        bridge.set_outputs("h", [((64, 36), False)])
        time.sleep(0.3)
        ticks, draws = span(1.0)
        assert ticks > 20 and draws >= ticks - 3  # frames Python takes: every one, as before
        start = time.perf_counter()
        bridge.engine.link_output(0, 0, writer, start, 30, fill_seconds=1.0)
        time.sleep(0.3)
        ticks, draws = span(2.0)
        assert ticks > 40 and draws <= ticks * 0.65  # about half: the writer takes 30 a second
        last, linked = bridge.engine.unlink_output(0, 0)
        assert linked and not bridge.failed
        writer.finish()
        assert writer.wait(20)
        stats = writer.stats()
        assert stats.state_name == "finished", stats.error_text
        assert last > 50 and stats.frames_repeated <= max(3, last // 10)  # its frames all drawn: no holes filled
    finally:
        writer.close()
        bridge.close()
