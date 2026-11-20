"""Page MP4s the page engine can't play, converted to WebM (app/page_video.py)."""

import http.client
import urllib.parse
from pathlib import Path

import pytest

from sprigcast.app import page_video as pv

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")


def _has(codec: str) -> bool:
    try:
        av.codec.Codec(codec, "w")
        return True
    except Exception:
        return False


def _clip(path: Path, video="libx264", audio="aac", frames=30) -> Path:
    with av.open(str(path), "w") as o:
        vs = o.add_stream(video, rate=30)
        vs.width, vs.height, vs.pix_fmt = 320, 180, "yuv420p"
        aus = o.add_stream(audio, rate=48000)
        aus.layout = "stereo"
        for i in range(frames):
            f = av.VideoFrame.from_ndarray(np.full((180, 320, 3), i * 8 % 255, np.uint8), format="rgb24")
            f.pts = i
            for p in vs.encode(f):
                o.mux(p)
        for p in vs.encode(None):
            o.mux(p)
        size = aus.codec_context.frame_size or 1024
        for i in range(48000 * frames // 30 // size):
            a = av.AudioFrame.from_ndarray(np.zeros((2, size), np.float32), format="fltp", layout="stereo")
            a.sample_rate, a.pts = 48000, i * size
            for p in aus.encode(a):
                o.mux(p)
        for p in aus.encode(None):
            o.mux(p)
    return path


def test_which_requests_go_by_the_server():
    assert pv.wanted("http://127.0.0.1:7000/overlay/cache/abc", 9000)  # a local overlay app's clip, no file type
    assert pv.wanted("https://cdn.example.com/clip.MP4?sig=1", 9000)
    assert pv.wanted("file:///C:/clips/a.mp4", 9000)
    assert not pv.wanted("http://127.0.0.1:9000/x/v?u=1", 9000)  # the server itself
    assert not pv.wanted("http://127.0.0.1:7000/alert.mp3", 9000)
    assert not pv.wanted("https://cdn.example.com/stream.webm", 9000)
    assert not pv.wanted("https://cdn.example.com/page", 9000)  # someone else's server, no MP4 name
    assert not pv.wanted("file:///C:/clips/a.webm", 9000)
    assert not pv.wanted("data:video/mp4;base64,AAAA", 9000)


def test_mp4_heads():
    assert pv.is_mp4_head(b"\x00\x00\x00\x20ftypisom")
    assert not pv.is_mp4_head(b"\x1aE\xdf\xa3\x9fB\x86\x81\x01B\xf7\x81")  # WebM
    assert not pv.is_mp4_head(b"ID3\x04\x00\x00\x00\x00\x00\x00\x00\x00")


@pytest.mark.skipif(not (_has("libx264") and _has("libvpx") and _has("libopus")), reason="needs x264, VP8, Opus")
def test_an_h264_clip_becomes_vp8_and_opus_webm(tmp_path):
    src = _clip(tmp_path / "clip.mp4")
    out = tmp_path / "clip.webm"
    assert pv.convert(str(src), out, encoders=[pv.SW_ENCODER]) == pv.CONVERTED
    with av.open(str(out)) as i:
        assert [s.codec_context.name for s in i.streams] == ["vp8", "opus"]
        assert sum(1 for _ in i.decode(video=0)) == 30
    assert not out.with_name(out.name + ".part").exists()
    assert pv.convert(str(out), tmp_path / "again.webm") == pv.AS_IS  # WebM plays as it is


@pytest.mark.skipif(not _has("libx264"), reason="needs x264")
def test_no_encoder_that_opens_means_the_page_gets_the_original(tmp_path):
    src = _clip(tmp_path / "clip.mp4")
    out = tmp_path / "clip.webm"
    assert pv.convert(str(src), out, encoders=[("no_such_encoder", {})]) == pv.CANT
    assert not out.exists() and not out.with_name(out.name + ".part").exists()


def test_not_a_video(tmp_path):
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"not a video at all")
    assert pv.convert(str(junk), tmp_path / "junk.webm") == pv.CANT


def _get(port: int, path: str, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_the_server_converts_once_serves_ranges_and_sends_other_files_back(tmp_path):
    calls = []

    def run(src, out):
        calls.append(src)
        out.write_bytes(b"W" * 1000)
        return pv.CONVERTED

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 100)
    sound = tmp_path / "ding.mp3"
    sound.write_bytes(b"ID3\x04" + b"\x00" * 100)
    videos = pv.PageVideos(tmp_path / "cache", run=run)
    try:
        url = clip.as_uri()
        target = videos.route(url)
        assert target and target.startswith(f"http://127.0.0.1:{videos.port}/{videos.secret}/v?u=")
        path = urllib.parse.urlsplit(target)
        path = f"{path.path}?{path.query}"
        status, headers, body = _get(videos.port, path)
        assert status == 200 and headers["Content-Type"] == "video/webm" and body == b"W" * 1000
        status, headers, body = _get(videos.port, path, {"Range": "bytes=100-199"})
        assert status == 206 and headers["Content-Range"] == "bytes 100-199/1000" and len(body) == 100
        assert calls == [str(clip)]  # converted once, the copy kept
        assert videos.route(sound.as_uri()) is None  # not an MP4: a file is never sent by the server
        assert _get(videos.port, f"/wrong/v?u={urllib.parse.quote(url, safe='')}")[0] == 404
    finally:
        videos.close()


def test_a_playable_file_on_a_local_server_goes_back_to_it(tmp_path):
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    (tmp_path / "clip").write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 100)  # WebM under a name without a file type
    site = ThreadingHTTPServer(("127.0.0.1", 0),
                               functools.partial(SimpleHTTPRequestHandler, directory=str(tmp_path)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    videos = pv.PageVideos(tmp_path / "cache", run=lambda src, out: pytest.fail("nothing to convert"))
    try:
        url = f"http://127.0.0.1:{site.server_address[1]}/clip"
        target = urllib.parse.urlsplit(videos.route(url))
        status, headers, _ = _get(videos.port, f"{target.path}?{target.query}")
        assert status == 307 and headers["Location"] == url
        assert videos.route(url) is None  # from now on the page asks its server directly
    finally:
        videos.close()
        site.shutdown()


def test_nothing_is_installed_when_the_engine_plays_h264(monkeypatch):
    monkeypatch.setattr(pv, "engine_plays_h264", lambda: True)
    assert pv.install(object(), "/tmp") is False


def test_a_dropped_request_leaves_no_traceback(tmp_path, caplog):
    import socket

    videos = pv.PageVideos(tmp_path / "cache", run=lambda src, out: pv.CANT)
    try:
        s = socket.create_connection(("127.0.0.1", videos.port))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        s.close()  # reset, as the page engine does with a request it no longer needs
        assert _get(videos.port, "/nope")[0] == 404  # still answering
    finally:
        videos.close()
    assert "page video: a request" not in caplog.text


@pytest.mark.skipif(not (_has("libx264") and _has("libvpx") and _has("libopus")), reason="needs x264, VP8, Opus")
def test_a_clip_on_a_server_is_read_once_then_converted(tmp_path):
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    _clip(tmp_path / "clip.mp4")
    asked = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            asked.append(self.path)

    site = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(tmp_path)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    try:
        out = tmp_path / "out.webm"
        url = f"http://127.0.0.1:{site.server_address[1]}/clip.mp4"
        assert pv.convert(url, out, encoders=[pv.SW_ENCODER], hw_decode=False) == pv.CONVERTED
        assert len(asked) == 1  # one read, no jumping about on the server
        assert not out.with_name(out.name + ".src").exists()
        with av.open(str(out)) as i:
            assert [s.codec_context.name for s in i.streams] == ["vp8", "opus"]
    finally:
        site.shutdown()



def test_pages_say_browser_h264_only_with_the_codec_engine(monkeypatch):
    from sprigcast.control import native

    monkeypatch.setattr(pv, "engine_plays_h264", lambda: False)
    assert "browser.h264" not in native.features()
    monkeypatch.setattr(pv, "engine_plays_h264", lambda: True)
    assert "browser.h264" in native.features()
