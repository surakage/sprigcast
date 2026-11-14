"""Measure the upload the way a stream uses it: a short stream to Twitch's bandwidth test.

Twitch takes a stream sent with ``?bandwidthtest=true`` like any other but never shows it to viewers,
so it is the closest thing to going live. A test video (moving noise, which can't be compressed, so the
encoder really sends ``ceiling_kbps``) is sent in real time for a few seconds. When the upload is slower
than that, writing to the connection blocks, and bytes sent over time is the upload speed.
"""

from __future__ import annotations

import logging
import time
from fractions import Fraction

log = logging.getLogger(__name__)

TWITCH_SERVER = "rtmp://live.twitch.tv/app"
CEILING_KBPS = 16000  # higher than any site takes for one stream (Twitch 6000, YouTube 20000 is rare)
SECONDS = 8.0
WARMUP_S = 2.0  # connecting and the first burst into the network buffers don't count
WIDTH, HEIGHT, FPS = 1280, 720, 30


def _frames(n: int = 6):
    import av
    import numpy as np

    rng = np.random.default_rng(7)
    return [av.VideoFrame.from_ndarray(rng.integers(0, 256, (HEIGHT, WIDTH, 3), dtype=np.uint8), format="rgb24")
            .reformat(format="yuv420p") for _ in range(n)]


def stream_test(url: str, seconds: float = SECONDS, ceiling_kbps: int = CEILING_KBPS, open_container=None,
                clock=time.monotonic, sleep=time.sleep) -> int | None:
    """Upload in kbps measured by streaming to `url`; capped at `ceiling_kbps`. None when it couldn't connect."""
    import av

    open_container = open_container or (lambda u: av.open(u, "w", format="flv"))
    container = None
    try:
        container = open_container(url)
        video = container.add_stream("libx264", rate=FPS)
        video.width, video.height, video.pix_fmt = WIDTH, HEIGHT, "yuv420p"
        video.bit_rate = ceiling_kbps * 1000
        video.options = {"preset": "ultrafast", "tune": "zerolatency", "maxrate": f"{ceiling_kbps}k",
                         "bufsize": f"{ceiling_kbps // 2}k", "x264-params": "nal-hrd=cbr:filler=1:keyint=60"}
        audio = container.add_stream("aac", rate=48000)
        audio.layout = "stereo"
        pictures = _frames()
        silence = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
        for plane in silence.planes:
            plane.update(bytes(plane.buffer_size))
        silence.sample_rate = 48000
        start = clock()
        sent = counted_from = 0
        counted_at = None
        frame_no = samples = 0
        while (now := clock()) - start < seconds:
            if counted_at is None and now - start >= WARMUP_S:
                counted_at, counted_from = now, sent
            picture = pictures[frame_no % len(pictures)]
            picture.pts, picture.time_base = frame_no, Fraction(1, FPS)
            for packet in video.encode(picture):
                sent += packet.size
                container.mux(packet)  # blocks when the upload can't keep up
            while samples < (frame_no + 1) * 48000 // FPS:
                silence.pts, silence.time_base = samples, Fraction(1, 48000)
                for packet in audio.encode(silence):
                    sent += packet.size
                    container.mux(packet)
                samples += 1024
            frame_no += 1
            wait = start + frame_no / FPS - clock()
            if wait > 0:
                sleep(wait)  # real time, like a stream: never faster than the encoder would send
        end = clock()
        if counted_at is None or end <= counted_at:
            return None
        return int((sent - counted_from) * 8 / 1000 / (end - counted_at))
    except Exception as exc:
        message = str(exc)
        for secret in (url, url.rsplit("/", 1)[-1].split("?")[0]):  # never log the stream key
            message = message.replace(secret, "****") if secret else message
        log.info("upload test stream failed: %s", message)
        return None
    finally:
        if container is not None:
            try:
                container.close()
            except Exception:
                pass


def twitch_key(collection) -> str | None:
    """A Twitch stream key from the outputs: the connected account's, else one saved for a Twitch output."""
    from sprigcast.output.keys import get_key

    for output in collection.outputs:
        if output.type != "stream" or output.service != "twitch":
            continue
        if output.twitch_account:
            try:
                from sprigcast.output.twitch_auth import fetch_stream_key

                return fetch_stream_key()
            except Exception as exc:
                log.info("upload test: couldn't get the Twitch key from the account: %s", exc)
        key = get_key(output.key_ref)
        if key:
            return key
    return None


def twitch_upload(key: str, **kwargs) -> int | None:
    from sprigcast.output.stream import stream_url

    return stream_test(stream_url(TWITCH_SERVER, key, bandwidth_test=True), **kwargs)
