"""Page videos the page engine can't play (H.264/AAC MP4), turned into WebM it can.

The pip PySide6 Qt WebEngine has no H.264/AAC, so a page's MP4 clip (StreamBridge's Random Clip Player plays
Twitch clips from its local cache) fails and the page shows an empty player. Every media request a page makes for
an MP4 (by its name, or any video on this computer's own servers) is sent to a small server here first:

- An MP4 the engine can't play is converted to WebM once (AV1 on the graphics card when it has an encoder, else
  VP8; the sound to Opus) by another program, so SprigCast and its pages never wait on it. The copy is kept
  (CACHE_BYTES at most, least recently played first out, in the local cache folder) and served from then on.
- Anything else (WebM, MP3, an MP4 with VP9/AV1/Opus inside) goes straight back to where the page asked for it.
- If converting fails, the page gets the original back and says the video can't play, as before.

Nothing is installed when the page engine plays H.264 itself (the codec build lays MARKER next to it).
Only the page's own requests can use the server: its address has a secret made at start.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

MARKER = "webengine-codecs.txt"  # in PySide6's folder once the page engine with H.264/AAC is laid over it
CACHE_BYTES = 1536 * 1024 * 1024
CONVERT_TIMEOUT_S = 180.0
MP4_SUFFIXES = (".mp4", ".m4v", ".mov", ".m4a", ".3gp")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")
PLAYABLE_VIDEO = {"vp8", "vp9", "av1", "theora"}
PLAYABLE_AUDIO = {"opus", "vorbis", "flac", "mp3"}
# Converted, played as is, not a video or broken (the page gets the original back), and the converter's crash.
CONVERTED, AS_IS, CANT = 0, 3, 4
MAX_W, MAX_H = 1920, 1080
MIN_RATE, HW_MAX_RATE, SW_MAX_RATE = 1_500_000, 6_000_000, 8_000_000
HW_ENCODERS = (("av1_nvenc", {"preset": "p1", "tune": "hq"}), ("av1_amf", {"usage": "transcoding", "quality": "speed"}),
               ("av1_qsv", {"preset": "veryfast"}))
SW_ENCODER = ("libvpx", {"deadline": "realtime", "cpu-used": "8", "lag-in-frames": "0", "auto-alt-ref": "0"})


def engine_plays_h264() -> bool:
    try:
        import PySide6

        return (Path(PySide6.__file__).parent / MARKER).is_file()
    except Exception:
        return False


def is_mp4_head(head: bytes) -> bool:
    """An ISO media file (MP4, MOV, M4A...): its first box is ftyp (some start with a free/wide/mdat box)."""
    return len(head) >= 8 and head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")


def wanted(url: str, port: int) -> bool:
    """Whether a page's media request goes by the server: MP4s by name anywhere, and anything (sniffed then) on
    this computer's own servers, where overlay apps keep clips under names without a file type."""
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https", "file"):
        return False
    if host in LOCAL_HOSTS and u.port == port:
        return False  # the server itself
    path = urllib.parse.unquote(u.path).lower()
    if path.endswith(MP4_SUFFIXES):
        return True
    if u.scheme == "file":
        return False
    if path.endswith((".webm", ".mp3", ".ogg", ".oga", ".opus", ".wav", ".flac", ".m3u8", ".mpd", ".ts", ".aac")):
        return False
    return host in LOCAL_HOSTS


def file_path(url: str) -> Path | None:
    u = urllib.parse.urlsplit(url)
    if u.scheme != "file":
        return None
    path = urllib.parse.unquote(u.path)
    if sys.platform == "win32" and len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if u.netloc and u.netloc != "localhost":
        path = f"//{u.netloc}{path}"
    return Path(path)


# The converter (another program) -------------------------------------------------------------------------------


def convert(src: str, out: Path, encoders=None, hw_decode: bool = True) -> int:
    """src (a file or URL) to out (WebM): CONVERTED, AS_IS (the engine plays it already) or CANT."""
    if "://" in src and not src.startswith("file:"):
        # Read once into a local file first: decoding straight from a server seeks there for every jump between
        # the picture and the sound (3,702 requests and 50 s for a one-minute clip on the PC, 2026-11-20).
        local = out.with_name(out.name + ".src")
        try:
            started = time.monotonic()
            _download(src, local)
            log.info("page video: read %d MB in %.1f s", local.stat().st_size // (1024 * 1024),
                     time.monotonic() - started)
            return convert(str(local), out, encoders, hw_decode)
        except OSError as e:
            log.info("page video %s: can't read it (%s)", src[:200], e)
            return CANT
        finally:
            local.unlink(missing_ok=True)
    return _convert_file(src, out, encoders, hw_decode)


def _download(url: str, to: Path) -> None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if host in LOCAL_HOSTS \
        else urllib.request.build_opener()
    with opener.open(urllib.request.Request(url), timeout=20) as r, open(to, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def _open(src: str, hw_decode: bool):
    """The file, decoded on the graphics card when it can (D3D11, any card): the pictures come back as NV12,
    which the card's encoder takes as they are. (container, decoded on the card)"""
    import av

    if hw_decode:
        try:
            from av.codec.hwaccel import HWAccel, hwdevices_available

            kind = next((k for k in ("d3d11va", "cuda", "vaapi", "videotoolbox") if k in hwdevices_available()), None)
            if kind:
                return av.open(src, hwaccel=HWAccel(kind, allow_software_fallback=True)), True
        except Exception as e:  # noqa: BLE001  (no card, no driver: decoded by the processor)
            log.info("page video: no decoding on the card (%s)", e)
    return av.open(src), False


def _convert_file(src: str, out: Path, encoders, hw_decode: bool) -> int:
    import av

    try:
        inp, on_card = _open(src, hw_decode)
    except Exception as e:  # noqa: BLE001  (not a media file at all)
        log.info("page video %s: can't open (%s)", src[:200], e)
        return CANT
    with inp:
        v = next(iter(inp.streams.video), None)
        a = next(iter(inp.streams.audio), None)
        vname = v.codec_context.name if v is not None else None
        aname = a.codec_context.name if a is not None else None
        if v is None and a is None:
            return CANT
        v_ok = v is None or vname in PLAYABLE_VIDEO
        a_ok = a is None or aname in PLAYABLE_AUDIO or (aname or "").startswith("pcm_")
        if v_ok and a_ok:
            return AS_IS
        part = out.with_name(out.name + ".part")
        started, frames = time.monotonic(), 0
        try:
            with av.open(str(part), "w", format="webm") as dst:
                source_rate = (v.bit_rate or inp.bit_rate or 0) if v is not None else 0
                vout, venc = (_video_out(dst, v, encoders, "nv12" if on_card else (v.codec_context.pix_fmt or ""),
                                         source_rate)
                              if v is not None and not v_ok else (None, None))
                vcopy = dst.add_stream_from_template(v) if v is not None and v_ok else None
                sound = _Sound(dst) if a is not None and not a_ok else None
                acopy = dst.add_stream_from_template(a) if a is not None and a_ok else None
                if v is not None and venc is None and vcopy is None:
                    return CANT
                if v is not None:
                    v.thread_type = "AUTO"
                for packet in inp.demux(*[s for s in (v, a) if s is not None]):
                    if packet.stream is v:
                        if vcopy is not None:
                            if packet.dts is not None:
                                packet.stream = vcopy
                                dst.mux(packet)
                            continue
                        for frame in packet.decode():
                            _encode_video(dst, vout, venc, frame)
                            frames += 1
                        if packet.size == 0:
                            _encode_video(dst, vout, venc, None)
                    elif packet.stream is a:
                        if acopy is not None:
                            if packet.dts is not None:
                                packet.stream = acopy
                                dst.mux(packet)
                            continue
                        for frame in packet.decode():
                            sound.add(frame)
                        if packet.size == 0:
                            sound.add(None)
        except Exception:
            part.unlink(missing_ok=True)
            if on_card:  # the card's decoder gave up part way: once more on the processor
                log.info("page video %s: decoding on the card failed, again on the processor", src[:200])
                return _convert_file(src, out, encoders, False)
            log.exception("page video %s: converting failed", src[:200])
            return CANT
        seconds = time.monotonic() - started
        log.info("page video: %d pictures in %.1f s (%.0f a second), decoded on the %s", frames, seconds,
                 frames / max(seconds, 1e-3), "card" if on_card else "processor")
        part.replace(out)
        return CONVERTED


def _even(n: int) -> int:
    return max(2, n - n % 2)


def _video_out(dst, v, encoders, frames_format: str = "", source_rate: int = 0):
    import av

    w, h = v.codec_context.width, v.codec_context.height
    if not w or not h:
        return None, None
    scale = min(1.0, MAX_W / w, MAX_H / h)
    w, h = _even(round(w * scale)), _even(round(h * scale))
    rate = v.average_rate or v.guessed_rate or Fraction(30)
    for name, options in (encoders if encoders is not None else (*HW_ENCODERS, SW_ENCODER)):
        opts = dict(options)
        if name == "libvpx":
            opts["threads"] = str(max(1, min(8, (os.cpu_count() or 2) - 1)))
        # About the clip's own size (AV1 needs less than H.264 for the same look): a page buffers a big file
        # slowly, and the copy stays small. 12 Mbps made a 47 MB clip 87 MB (2026-11-20).
        guess = w * h * float(rate) * 0.05
        bit_rate = int(min(SW_MAX_RATE if name == "libvpx" else HW_MAX_RATE,
                           max(MIN_RATE, (source_rate * (1.0 if name == "libvpx" else 0.8)) or guess)))
        opts.update(maxrate=str(bit_rate * 3 // 2), bufsize=str(bit_rate * 2))
        try:
            formats = [f.name for f in (av.Codec(name, "w").video_formats or [])]
        except Exception:  # noqa: BLE001  (no such encoder in this FFmpeg)
            continue
        # The pictures' own format when the encoder takes it: nothing to convert per picture.
        pix_fmt = next((f for f in (frames_format, "nv12", "yuv420p") if f and f in formats), None)
        if pix_fmt is None:
            continue
        settings = {"width": w, "height": h, "pix_fmt": pix_fmt,
                    "time_base": v.time_base or Fraction(1, 1000), "bit_rate": bit_rate,
                    "gop_size": max(1, round(float(rate) * 2))}
        try:  # tried on its own first: a stream added to the file stays there even if its encoder won't open
            trial = av.CodecContext.create(name, "w")
            for key, value in settings.items():
                setattr(trial, key, value)
            trial.framerate = rate
            trial.options = opts
            trial.open()
            del trial
        except Exception as e:  # no such encoder, no such card, or its encoder is busy
            log.info("page video: %s won't open (%s)", name, e)
            continue
        out = dst.add_stream(name, rate=rate)
        for key, value in settings.items():
            setattr(out.codec_context, key, value)
        out.codec_context.options = opts
        log.info("page video: %s, %dx%d %s, %d kbps", name, w, h, pix_fmt, bit_rate // 1000)
        return out, out.codec_context
    return None, None


def _encode_video(dst, out, cc, frame) -> None:
    if frame is not None:
        if (frame.format.name, frame.width, frame.height) != (cc.pix_fmt, cc.width, cc.height):
            pts, tb = frame.pts, frame.time_base
            frame = frame.reformat(width=cc.width, height=cc.height, format=cc.pix_fmt)
            frame.pts, frame.time_base = pts, tb
        try:
            frame.pict_type = type(frame.pict_type).NONE  # the encoder picks its own key frames
        except Exception:
            pass
    for packet in out.encode(frame):
        dst.mux(packet)


class _Sound:
    """The page video's sound to Opus (48 kHz stereo), in whole Opus frames, timed by the samples written."""

    def __init__(self, dst):
        import av

        self.out = dst.add_stream("libopus", rate=48000)
        cc = self.out.codec_context
        cc.layout = "stereo"
        names = [f.name for f in (cc.codec.audio_formats or [])]
        cc.format = "flt" if "flt" in names or not names else names[0]
        cc.bit_rate = 160_000
        self.dst = dst
        self.fifo = av.AudioFifo()
        self.resampler = av.AudioResampler(cc.format.name, "stereo", 48000)
        self.samples = 0

    def _send(self, frame) -> None:
        if frame is not None:
            frame.pts = self.samples
            frame.time_base = Fraction(1, 48000)
            self.samples += frame.samples
        for packet in self.out.encode(frame):
            self.dst.mux(packet)

    def add(self, frame) -> None:
        size = self.out.codec_context.frame_size or 960
        if frame is not None:
            frame.pts = None
        for f in self.resampler.resample(frame):
            self.fifo.write(f)
        while self.fifo.samples >= size:
            self._send(self.fifo.read(size))
        if frame is None:
            if self.fifo.samples:
                self._send(self.fifo.read())
            self._send(None)


def converter_command(src: str, out: Path) -> list[str]:
    code = ("import sys; sys.path[:0] = [p for p in sys.argv[3:] if p]; "
            "from sprigcast.app.page_video import convert_main; sys.exit(convert_main(sys.argv[1], sys.argv[2]))")
    return [sys.executable, "-I", "-c", code, src, str(out), *[p for p in sys.path if p and Path(p).is_dir()]]


def convert_main(src: str, out: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    try:
        return convert(src, Path(out))
    except Exception:
        log.exception("page video: converter")
        return CANT


# The server ---------------------------------------------------------------------------------------------------


class PageVideos:
    """The server the page's media requests go by (see the module's notes). Its threads only wait: the work is
    in the converter, one clip at a time."""

    def __init__(self, folder: Path, run=None):
        self.folder = Path(folder)
        self.secret = secrets.token_urlsafe(16)
        self.run = run or self._run_converter
        self._lock = threading.Lock()
        self._as_is: set[str] = set()  # URLs the engine plays: the interceptor leaves them be
        self._jobs: dict[str, threading.Event] = {}
        self._results: dict[str, int] = {}
        self._one = threading.Semaphore(1)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # quiet
                pass

            def do_GET(self):
                owner._answer(self, body=True)

            def do_HEAD(self):
                owner._answer(self, body=False)

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # The page engine drops a request it no longer needs (a seek, a clip that changed): no traceback.
                if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
                    log.exception("page video: a request")

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="page-videos", daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # What the interceptor asks

    def route(self, url: str) -> str | None:
        """The server's address for a page's media request, or None to leave it be."""
        with self._lock:
            if url in self._as_is:
                return None
        if not wanted(url, self.port):
            return None
        path = file_path(url)
        if path is not None:  # a file the page names: only an MP4 goes by the server (a file never redirects back)
            try:
                with open(path, "rb") as f:
                    if not is_mp4_head(f.read(12)):
                        return None
            except OSError:
                return None
        return f"http://127.0.0.1:{self.port}/{self.secret}/v?u={urllib.parse.quote(url, safe='')}"

    # Answers

    def _answer(self, req: BaseHTTPRequestHandler, body: bool) -> None:
        try:
            parts = urllib.parse.urlsplit(req.path)
            if parts.path != f"/{self.secret}/v":
                req.send_error(404)
                return
            url = urllib.parse.parse_qs(parts.query).get("u", [""])[0]
            if not url or not wanted(url, -1):
                req.send_error(400)
                return
            path = self.playable(url)
            if path is None:
                self._send_back(req, url)
            else:
                self._send_file(req, path, body)
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception:
            log.exception("page video: answering %s", req.path[:200])

    def playable(self, url: str) -> Path | None:
        """The file to give the page for url (converted, or the page's own file played as is), or None: the page
        gets the original from where it asked."""
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
        out = self.folder / f"{key}.webm"
        local = file_path(url)
        if out.is_file():
            return out
        with self._lock:
            if url in self._as_is:
                return local
            job = self._jobs.get(key)
            mine = job is None
            if mine:
                job = self._jobs[key] = threading.Event()
        if mine:
            result = CANT
            try:
                result = self._sniff_and_convert(url, local, out)
            finally:
                with self._lock:
                    self._results[key] = result
                    self._jobs.pop(key, None)
                    if result == AS_IS:
                        self._as_is.add(url)
                job.set()
        else:
            job.wait(CONVERT_TIMEOUT_S + 10)
        result = self._results.get(key, CANT)
        if result == CONVERTED and out.is_file():
            return out
        return local if result == AS_IS else None

    def _sniff_and_convert(self, url: str, local: Path | None, out: Path) -> int:
        head = b""
        try:
            if local is not None:
                with open(local, "rb") as f:
                    head = f.read(12)
            else:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) \
                    if (urllib.parse.urlsplit(url).hostname or "") in LOCAL_HOSTS else urllib.request.build_opener()
                with opener.open(urllib.request.Request(url, headers={"Range": "bytes=0-11"}), timeout=10) as r:
                    head = r.read(12)
        except Exception as e:
            log.info("page video %s: can't look at it (%s)", url[:200], e)
            return CANT
        if not is_mp4_head(head):
            return AS_IS  # WebM, MP3, a stream...: the engine's own business
        self.folder.mkdir(parents=True, exist_ok=True)
        with self._one:
            if out.is_file():
                return CONVERTED
            started = time.monotonic()
            result = self.run(str(local) if local is not None else url, out)
            log.info("page video %s: %s in %.1f s", url[:200],
                     {CONVERTED: "converted to WebM", AS_IS: "plays as it is"}.get(result, "can't be converted"),
                     time.monotonic() - started)
        if result == CONVERTED:
            self._trim()
        return result

    def _run_converter(self, src: str, out: Path) -> int:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
        try:
            proc = subprocess.Popen(converter_command(src, out), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, creationflags=flags)
        except OSError as e:
            log.warning("page video: the converter didn't start (%s)", e)
            return CANT
        try:
            _, err = proc.communicate(timeout=CONVERT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            log.warning("page video %s: converting took over %d s", src[:200], CONVERT_TIMEOUT_S)
            return CANT
        for line in (err or b"").decode("utf-8", "replace").splitlines()[-8:]:
            if line.strip():
                log.info("page video converter: %s", line.strip()[:300])
        return proc.returncode if proc.returncode in (CONVERTED, AS_IS) else CANT

    def _trim(self) -> None:
        try:
            files = sorted((p for p in self.folder.glob("*.webm") if p.is_file()), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            for p in files[:-1]:
                if total <= CACHE_BYTES:
                    break
                total -= p.stat().st_size
                p.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _send_back(req: BaseHTTPRequestHandler, url: str) -> None:
        req.send_response(307)
        req.send_header("Location", url)
        req.send_header("Access-Control-Allow-Origin", "*")
        req.send_header("Cache-Control", "no-store")
        req.send_header("Content-Length", "0")
        req.end_headers()

    @staticmethod
    def _send_file(req: BaseHTTPRequestHandler, path: Path, body: bool) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        ranged = req.headers.get("Range", "")
        if ranged.startswith("bytes="):
            first, _, last = ranged[6:].split(",")[0].strip().partition("-")
            try:
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            if start >= size or start > end:
                req.send_response(416)
                req.send_header("Content-Range", f"bytes */{size}")
                req.send_header("Content-Length", "0")
                req.end_headers()
                return
        kind = "video/webm" if path.suffix == ".webm" else (mimetypes.guess_type(path.name)[0] or "video/mp4")
        req.send_response(206 if ranged else 200)
        req.send_header("Content-Type", kind)
        req.send_header("Accept-Ranges", "bytes")
        req.send_header("Access-Control-Allow-Origin", "*")
        req.send_header("Content-Length", str(end - start + 1))
        if ranged:
            req.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        req.end_headers()
        if not body:
            return
        try:
            os.utime(path)  # played: last out of the cache
        except OSError:
            pass
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                req.wfile.write(chunk)
                left -= len(chunk)


# The page engine side -----------------------------------------------------------------------------------------

_videos: PageVideos | None = None
_interceptors: list = []


def install(profile, base: str | None) -> bool:
    """Send the profile's media requests by the server (once per profile). False when it isn't needed (the page
    engine plays H.264) or can't be (no folder, the server didn't start)."""
    global _videos
    if not base or engine_plays_h264() or os.environ.get("SPRIGCAST_PAGE_VIDEO_OFF"):
        return False
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtWebEngineCore import (
            QWebEngineUrlRequestInfo,
            QWebEngineUrlRequestInterceptor,
        )

        if _videos is None:
            from PySide6.QtCore import QStandardPaths

            # The local cache folder (%LOCALAPPDATA%), not the roaming profile: up to CACHE_BYTES of clips.
            cache = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation) or base
            _videos = PageVideos(Path(cache) / "page-videos")
        videos = _videos
        media = QWebEngineUrlRequestInfo.ResourceType.ResourceTypeMedia

        class Interceptor(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):  # on the window's thread: a lookup, nothing that waits
                try:
                    if info.resourceType() != media:
                        return
                    target = videos.route(info.requestUrl().toString(QUrl.ComponentFormattingOption.FullyEncoded))
                    if target:
                        info.redirect(QUrl(target))
                except Exception:
                    log.exception("page video: a request")

        interceptor = Interceptor()
        profile.setUrlRequestInterceptor(interceptor)
        _interceptors.append(interceptor)  # the profile doesn't own it
        log.info("page videos: MP4s pages can't play are converted (port %d)", videos.port)
        return True
    except Exception:
        log.exception("page videos: not installed")
        return False
