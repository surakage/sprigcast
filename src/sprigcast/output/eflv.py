"""The Enhanced RTMP form Twitch Enhanced Broadcasting asks for, on top of FFmpeg's FLV muxer.

Amazon IVS's multitrack guide (what Twitch's Enhanced Broadcasting follows): "The primary, highest quality video
track must be packaged and sent as enhanced RTMP single-track video packets" (FourCC avc1: SequenceStart,
CodedFrames, CodedFramesX...), and the other qualities as Multitrack / OneTrack packets. FFmpeg 8's FLV muxer
writes the other qualities that way, but the first H.264 track as classic FLV video ("AVC NALU", codec id 7),
and Twitch dropped the connection a second or two after it went live, every time (PC, 2026-11-21).

So the muxer writes into memory, each classic H.264 video tag is rewritten as its Enhanced RTMP single-track
form (same frame, same timestamps), and the bytes go to the server through FFmpeg's RTMP protocol, which takes
an FLV byte stream and sends each tag as an RTMP message (the "data" muxer passes the bytes through as they are).
The connect command lists avc1 among the Enhanced RTMP codecs (fourCcList), as an Enhanced RTMP client does.
"""

from __future__ import annotations

import struct

FLV_HEADER = 13  # "FLV", version, flags, header size, then the first previous-tag-size (0)
TAG_HEADER = 11
VIDEO, AUDIO, SCRIPT = 9, 8, 18
AVC = 7  # classic FLV codec id for H.264
EX_HEADER = 0x80
SEQUENCE_START, CODED_FRAMES, SEQUENCE_END, CODED_FRAMES_X = 0, 1, 2, 3
FOURCC = b"avc1"


def enhanced_video_body(body: bytes) -> bytes:
    """A classic FLV H.264 video tag's body in its Enhanced RTMP single-track form; anything else as it was."""
    if len(body) < 5 or body[0] & EX_HEADER or body[0] & 0x0F != AVC:
        return body
    frame_type = body[0] & 0x70
    kind, cts, data = body[1], body[2:5], body[5:]
    if kind == 0:  # AVC sequence header: the avcC record
        return bytes([EX_HEADER | frame_type | SEQUENCE_START]) + FOURCC + data
    if kind == 2:  # end of sequence
        return bytes([EX_HEADER | frame_type | SEQUENCE_END]) + FOURCC
    if cts == b"\x00\x00\x00":  # no composition time offset: the shorter form, as FFmpeg does for other tracks
        return bytes([EX_HEADER | frame_type | CODED_FRAMES_X]) + FOURCC + data
    return bytes([EX_HEADER | frame_type | CODED_FRAMES]) + FOURCC + cts + data


class Rewriter:
    """Takes an FLV byte stream in any pieces; gives back the same stream with classic H.264 tags made Enhanced."""

    def __init__(self):
        self._pending = bytearray()
        self._header_done = False

    def feed(self, data: bytes) -> bytes:
        self._pending += data
        out = bytearray()
        if not self._header_done:
            if len(self._pending) < FLV_HEADER:
                return bytes(out)
            out += self._pending[:FLV_HEADER]
            del self._pending[:FLV_HEADER]
            self._header_done = True
        while len(self._pending) >= TAG_HEADER:
            size = int.from_bytes(self._pending[1:4], "big")
            whole = TAG_HEADER + size + 4
            if len(self._pending) < whole:
                break
            kind = self._pending[0] & 0x1F
            head = bytes(self._pending[:TAG_HEADER])
            body = bytes(self._pending[TAG_HEADER:TAG_HEADER + size])
            del self._pending[:whole]
            if kind == VIDEO:
                body = enhanced_video_body(body)
                head = head[:1] + len(body).to_bytes(3, "big") + head[4:]
            out += head + body + struct.pack(">I", TAG_HEADER + len(body))
        return bytes(out)


class _Memory:
    """Where FFmpeg's FLV muxer writes: kept until the stream takes it. Not seekable, so the muxer writes as it
    would to a network connection."""

    def __init__(self):
        self.data = bytearray()

    def write(self, data) -> int:
        self.data += bytes(data)
        return len(data)

    def take(self) -> bytes:
        data, self.data = bytes(self.data), bytearray()
        return data


class EnhancedIngest:
    """Stands in for the FLV container of an Enhanced Broadcasting stream: add_stream, add_stream_from_template,
    mux and close as on a PyAV container; what goes out is the Enhanced RTMP form above."""

    def __init__(self, url: str, timeout=None, open_out=None):
        import av

        self._memory = _Memory()
        self._rewriter = Rewriter()
        self._flv = av.open(self._memory, "w", format="flv",
                            options={"flvflags": "no_duration_filesize", "flush_packets": "1"})
        options = {"rtmp_enhanced_codecs": "avc1"} if url.startswith("rtmp") else {}
        self._out = (open_out or av.open)(url, "w", format="data", options=options, timeout=timeout)
        self._data = self._out.add_data_stream()
        self.name = url

    @property
    def streams(self):
        return self._flv.streams

    def add_stream(self, *args, **kwargs):
        return self._flv.add_stream(*args, **kwargs)

    def add_stream_from_template(self, *args, **kwargs):
        return self._flv.add_stream_from_template(*args, **kwargs)

    def mux(self, packets) -> None:
        self._flv.mux(packets)
        self._send()

    def _send(self) -> None:
        import av

        data = self._rewriter.feed(self._memory.take())
        if data:
            packet = av.Packet(data)
            packet.stream = self._data
            self._out.mux(packet)

    def close(self) -> None:
        try:
            self._flv.close()
            self._send()
        finally:
            self._out.close()
