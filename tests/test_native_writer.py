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
