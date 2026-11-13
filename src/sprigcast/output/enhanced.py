"""Twitch Enhanced Broadcasting (multitrack video).

How it works, following Amazon IVS's public "multitrack video broadcast software integration"
guide and OBS Studio's implementation (both GPL-compatible references; this is a fresh Python
version for SprigCast):

1. Before going live, POST a description of this PC (GPU, CPU, memory, Windows version) and the
   canvases to Twitch's GetClientConfiguration endpoint, authenticated with the stream key.
2. Twitch answers with the qualities ("encoder configurations") to send: size, frame rate,
   bitrate and encoder settings for each, which canvas each comes from (index 0 = horizontal,
   1 = vertical for dual-format streams), the audio settings and the ingest URL.
3. Encode every quality at once with keyframes at the same instants, and send them over one
   Enhanced RTMP connection: the first quality as a classic FLV video track, the rest as
   multitrack packets with a track id. FFmpeg's FLV muxer (FFmpeg 8+) writes exactly that layout.
4. Before every keyframe on every track, add "broadcast performance metrics" (BPM): three small
   SEI messages with timestamps and frame counters. Twitch disconnects streams without them.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TWITCH_CONFIG_URL = "https://ingest.twitch.tv/api/v3/GetClientConfiguration"
SCHEMA_VERSION = "2025-01-25"
CLIENT_NAME = "sprigcast"
REQUEST_TIMEOUT = 5  # seconds, like OBS


class EnhancedError(Exception):
    """Going live with Enhanced Broadcasting isn't possible right now; worded for the user."""


# This PC ---------------------------------------------------------------------------------


def _gpus_windows() -> list[dict]:
    """Display adapters from the registry: model, PCI ids, video memory and driver version.

    This is the same information OBS reads through DXGI. The driver version is the Windows form
    (for example 32.0.15.6094), which is also what DXGI reports.
    """
    import winreg

    gpus = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
        for i in range(64):
            try:
                sub = winreg.EnumKey(root, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(root, sub) as key:
                    values = {}
                    for name in ("DriverDesc", "DriverVersion", "MatchingDeviceId", "HardwareInformation.qwMemorySize",
                                 "HardwareInformation.MemorySize"):
                        try:
                            values[name] = winreg.QueryValueEx(key, name)[0]
                        except OSError:
                            pass
            except OSError:
                continue
            match = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})", str(values.get("MatchingDeviceId", "")), re.I)
            if not match or "DriverDesc" not in values:
                continue  # virtual adapters (remote desktop, Microsoft Basic Display) have no PCI ids
            memory = values.get("HardwareInformation.qwMemorySize") or values.get("HardwareInformation.MemorySize") or 0
            if isinstance(memory, bytes):
                memory = int.from_bytes(memory[:8], "little")
            gpus.append({
                "model": str(values["DriverDesc"]),
                "vendor_id": int(match.group(1), 16),
                "device_id": int(match.group(2), 16),
                "dedicated_video_memory": int(memory),
                "shared_system_memory": 0,
                "driver_version": str(values.get("DriverVersion", "")),
            })
    # Discrete NVIDIA first: that's the GPU SprigCast encodes on.
    gpus.sort(key=lambda g: (g["vendor_id"] != 0x10DE, -g["dedicated_video_memory"]))
    return gpus


def _windows_version() -> dict:
    import winreg

    info = {"name": "Windows", "version": platform.version(), "build": 0, "release": "", "revision": "0"}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
            def read(name, default=""):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return default

            build = int(read("CurrentBuildNumber", "0") or 0)
            info["build"] = build
            info["release"] = str(read("DisplayVersion" if build > 19041 else "ReleaseId", ""))
            info["revision"] = str(read("UBR", 0))
            major, minor = read("CurrentMajorVersionNumber", 10), read("CurrentMinorVersionNumber", 0)
            info["version"] = f"{major}.{minor}"
    except OSError:
        pass
    return info


def capabilities() -> dict:
    """What GetClientConfiguration wants to know about this PC."""
    import psutil

    cpu = {
        "physical_cores": psutil.cpu_count(logical=False) or os.cpu_count() or 1,
        "logical_cores": psutil.cpu_count(logical=True) or os.cpu_count() or 1,
    }
    try:
        freq = psutil.cpu_freq()
        if freq and freq.max:
            cpu["speed"] = int(freq.max)
    except Exception:
        pass
    name = platform.processor()
    if name:
        cpu["name"] = name
    memory = psutil.virtual_memory()
    caps: dict = {"cpu": cpu, "memory": {"total": int(memory.total), "free": int(memory.available)}}
    machine = platform.machine().lower()
    system = {"version": platform.version(), "name": platform.system(), "build": 0, "release": platform.release(),
              "revision": "0", "bits": 64 if platform.architecture()[0] == "64bit" else 32,
              "arm": machine.startswith(("arm", "aarch")), "armEmulation": False}
    if platform.system() == "Windows":
        system.update(_windows_version())
        try:
            gpus = _gpus_windows()
            for gpu in gpus:
                gpu["shared_system_memory"] = int(memory.total) // 2  # Windows lets a GPU borrow half of RAM
            caps["gpu"] = gpus
        except OSError as exc:
            log.warning("could not read the GPU list: %s", exc)
    caps["system"] = system
    return caps


# The request and the answer -------------------------------------------------------------


@dataclass
class CanvasInfo:
    width: int
    height: int
    fps: int


def build_request(stream_key: str, canvases: list[CanvasInfo], version: str, *,
                  max_tracks: int | None = None, max_bitrate_kbps: int | None = None) -> dict:
    """The JSON body for GetClientConfiguration. canvases[0] is the main (horizontal) canvas."""
    preferences: dict = {
        "vod_track_audio": False,
        "composition_gpu_index": 0,
        "audio_samples_per_sec": 48000,
        "audio_channels": 2,
        "audio_max_buffering_ms": 960,
        "audio_fixed_buffering": False,
        "canvases": [
            {"width": c.width, "height": c.height, "canvas_width": c.width, "canvas_height": c.height,
             "framerate": {"numerator": c.fps, "denominator": 1}}
            for c in canvases
        ],
    }
    if max_tracks:
        preferences["maximum_video_tracks"] = int(max_tracks)
    if max_bitrate_kbps:
        preferences["maximum_aggregate_bitrate"] = int(max_bitrate_kbps) * 1000
    return {
        "service": "IVS",
        "schema_version": SCHEMA_VERSION,
        "authentication": stream_key,
        "client": {"name": CLIENT_NAME, "version": version, "supported_codecs": ["h264"]},
        "capabilities": capabilities(),
        "preferences": preferences,
    }


@dataclass
class TrackConfig:
    canvas_index: int
    width: int
    height: int
    fps: int
    bitrate_kbps: int
    keyint_sec: int
    encoder: str  # FFmpeg encoder name
    options: dict[str, str]
    max_b_frames: int


@dataclass
class EnhancedConfig:
    tracks: list[TrackConfig]
    server: str  # rtmp(s)://host/app
    key: str  # stream key plus query (clientConfigId, bandwidthtest)
    audio_kbps: int = 160
    audio_channels: int = 2
    config_id: str = ""
    warning: str = ""  # shown to the user, streaming continues
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def url(self) -> str:
        return f"{self.server.rstrip('/')}/{self.key}"

    def describe(self) -> str:
        return ", ".join(f"{t.width}×{t.height}@{t.fps} {t.bitrate_kbps} kbps" for t in self.tracks)


def _plain(html_text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html_text or "")).strip()


def _fps(value) -> int:
    if isinstance(value, dict) and value.get("numerator"):
        return max(1, round(value["numerator"] / max(1, value.get("denominator", 1))))
    return 0


def _encoder(kind: str, codec: str) -> str:
    """OBS encoder ids in the answer ("obs_nvenc_h264_tex", "jim_nvenc", "obs_x264") → FFmpeg encoders."""
    kind = (kind or "").lower()
    if "nvenc" in kind and "hevc" not in kind and "av1" not in kind:
        return "h264_nvenc"
    if "x264" in kind:
        return "libx264"
    return codec  # fall back to whatever SprigCast picked (NVENC when it works)


def encoder_options(encoder: str, settings: dict, bitrate_kbps: int) -> dict[str, str]:
    """Translate OBS-style encoder settings from the answer into FFmpeg options."""
    rate = {"maxrate": f"{bitrate_kbps}k", "bufsize": f"{bitrate_kbps}k"}
    if encoder == "h264_nvenc":
        options = {
            "preset": str(settings.get("preset2") or "p5"),
            "tune": str(settings.get("tune") or "hq"),
            "rc": "cbr" if str(settings.get("rate_control", "CBR")).upper() == "CBR" else "vbr",
            "profile": str(settings.get("profile") or "high"),
            "no-scenecut": "1",  # keyframes only where we put them, so every track lines up
            "forced-idr": "1",
            **rate,
        }
        multipass = str(settings.get("multipass") or "")
        if multipass in ("qres", "fullres", "disabled"):
            options["multipass"] = multipass
        if settings.get("lookahead"):
            options["rc-lookahead"] = "16"
        if settings.get("psycho_aq", True):
            options["spatial-aq"] = "1"
        return options
    if encoder != "libx264":  # AMD, Intel or Media Foundation: SprigCast's own CBR options for that encoder
        from sprigcast.core.scene import EncoderSettings
        from sprigcast.output.recorder import video_options

        profile = str(settings.get("profile") or "high")
        enc = EncoderSettings(rate_control="cbr", b_frames=0,
                              profile=profile if profile in ("high", "main", "baseline") else "high")
        return video_options(encoder, enc, bitrate_kbps)
    return {"preset": "veryfast", "profile": str(settings.get("profile") or "high"), "nal-hrd": "cbr",
            "sc_threshold": "0", **rate}


def parse_config(data: dict, user_key: str, fallback_codec: str, bandwidth_test: bool = False,
                 use_rtmps: bool = False) -> EnhancedConfig:
    """Turn GetClientConfiguration's answer into tracks and an ingest URL. Raises EnhancedError."""
    status = data.get("status") or {}
    result = str(status.get("result", "success")).lower()
    message = _plain(status.get("html_en_us", ""))
    if result == "error":
        raise EnhancedError(message or "Twitch refused Enhanced Broadcasting for this setup.")
    encoders = data.get("encoder_configurations") or []
    if not encoders:
        raise EnhancedError(message or "Twitch didn't send any stream qualities.")

    tracks = []
    for entry in encoders:
        settings = entry.get("settings") or {}
        kbps = int(settings.get("bitrate") or 0)
        if not kbps or not entry.get("width") or not entry.get("height"):
            raise EnhancedError("Twitch sent a quality SprigCast doesn't understand.")
        encoder = _encoder(entry.get("type", ""), fallback_codec)
        tracks.append(TrackConfig(
            canvas_index=int(entry.get("canvas_index", 0)),
            width=int(entry["width"]),
            height=int(entry["height"]),
            fps=_fps(entry.get("framerate")),
            bitrate_kbps=kbps,
            keyint_sec=int(settings.get("keyint_sec") or 2),
            encoder=encoder,
            options=encoder_options(encoder, settings, kbps),
            max_b_frames=int(settings.get("bf", 2) if settings.get("bf") is not None else 2),
        ))

    endpoints = [e for e in data.get("ingest_endpoints") or [] if str(e.get("protocol", "")).upper().startswith("RTMP")]
    wanted = [e for e in endpoints if (str(e.get("protocol", "")).upper() == "RTMPS") == use_rtmps] or endpoints
    if not wanted:
        raise EnhancedError("Twitch didn't send a server to stream to.")
    endpoint = wanted[0]
    template = str(endpoint.get("url_template", ""))
    server = template.split("/{stream_key}")[0]
    key = str(endpoint.get("authentication") or user_key)

    meta = data.get("meta") or {}
    config_id = str(meta.get("config_id", ""))
    query = []
    if bandwidth_test:
        query.append(("bandwidthtest", "true"))
    if config_id:
        query.append(("clientConfigId", config_id))
    if query:
        key += "?" + urllib.parse.urlencode(query)

    audio = ((data.get("audio_configurations") or {}).get("live") or [{}])[0]
    audio_kbps = int(((audio.get("settings") or {}).get("bitrate")) or 160)
    return EnhancedConfig(
        tracks=tracks,
        server=server,
        key=key,
        audio_kbps=audio_kbps,
        audio_channels=int(audio.get("channels") or 2),
        config_id=config_id,
        warning=message if result == "warning" else "",
        raw=data,
    )


def fetch_config(request: dict, url: str = TWITCH_CONFIG_URL) -> dict:
    body = json.dumps(request).encode()
    http = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(http, timeout=REQUEST_TIMEOUT) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
            if data.get("status"):
                return data  # the answer explains the problem in status.html_en_us
        except ValueError:
            pass
        raise EnhancedError(f"Twitch answered {exc.code} to the Enhanced Broadcasting request.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise EnhancedError(f"Couldn't get the Enhanced Broadcasting settings from Twitch: {exc}") from exc


# Broadcast performance metrics (SEI messages) ----------------------------------------------

BPM_TS_UUID = bytes([0x0A, 0xEC, 0xFF, 0xE7, 0x52, 0x72, 0x4E, 0x2F, 0xA6, 0x2F, 0xD1, 0x9C, 0xD6, 0x1A, 0x93, 0xB5])
BPM_SM_UUID = bytes([0xCA, 0x60, 0xE7, 0x1C, 0x6A, 0x8B, 0x43, 0x88, 0xA3, 0x77, 0x15, 0x1D, 0xF7, 0xBF, 0x8A, 0xC2])
BPM_ERM_UUID = bytes([0xF1, 0xFB, 0xC1, 0xD5, 0x10, 0x1E, 0x4F, 0xB5, 0xA6, 0x1E, 0xB8, 0xCE, 0x3C, 0x07, 0xB8, 0xC0])
TS_RFC3339 = 1
EVENT_CTS, EVENT_FER, EVENT_FERC, EVENT_PIR = 1, 2, 3, 4


def rfc3339(wall_seconds: float) -> bytes:
    """"2024-05-31T12:26:03.591Z" plus the null terminator the format wants."""
    dt = datetime.fromtimestamp(wall_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S").encode() + f".{dt.microsecond // 1000:03d}Z".encode() + b"\0"


def _escape(rbsp: bytes) -> bytes:
    """Add H.264 emulation prevention bytes: 00 00 0x (x ≤ 3) becomes 00 00 03 0x."""
    out, zeros = bytearray(), 0
    for byte in rbsp:
        if zeros >= 2 and byte <= 3:
            out.append(3)
            zeros = 0
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def sei_nal(payload: bytes) -> bytes:
    """One Annex-B H.264 SEI NAL unit carrying a user_data_unregistered (type 5) message."""
    body = bytearray([5])
    size = len(payload)
    while size >= 255:
        body.append(255)
        size -= 255
    body.append(size)
    body += payload
    body.append(0x80)  # rbsp trailing bits
    return b"\x00\x00\x00\x01\x06" + _escape(bytes(body))


@dataclass
class TrackMetrics:
    """Counter values at the previous keyframe, so each report carries the change since then."""
    rendered: int = 0
    lagged: int = 0
    dropped: int = 0
    output: int = 0
    input: int = 0
    skipped: int = 0
    encoded: int = 0
    first: bool = True


def bpm_sei(metrics: TrackMetrics, *, cts: float, fer: float, ferc: float, pir: float,
            session: tuple[int, int, int, int], rendition: tuple[int, int, int]) -> bytes:
    """The three BPM SEI NAL units to add to a keyframe.

    cts/fer/ferc/pir: wall-clock times (seconds) when the frame was composed, sent to the encoder,
    came back encoded, and was handed to the network. session = totals (rendered, lagged,
    dropped, output); rendition = totals for this track (input, skipped, encoded).
    """
    rendered, lagged, dropped, output = session
    frames_in, skipped, encoded = rendition
    if metrics.first:  # the first report says 0 for every change, like OBS
        diffs_s, diffs_r = (0, 0, 0, 0), (0, 0, 0)
        metrics.first = False
    else:
        diffs_s = (rendered - metrics.rendered, lagged - metrics.lagged, dropped - metrics.dropped, output - metrics.output)
        diffs_r = (frames_in - metrics.input, skipped - metrics.skipped, encoded - metrics.encoded)
    metrics.rendered, metrics.lagged, metrics.dropped, metrics.output = session
    metrics.input, metrics.skipped, metrics.encoded = rendition

    stamps = [(EVENT_CTS, cts)]
    if fer > cts:
        stamps.append((EVENT_FER, fer))
        if ferc > fer:
            stamps.append((EVENT_FERC, ferc))
    stamps.append((EVENT_PIR, pir))
    ts = bytearray(BPM_TS_UUID) + bytes([(len(stamps) - 1) & 0x0F])
    for event, when in stamps:
        ts += bytes([TS_RFC3339, event]) + rfc3339(when)

    pir_stamp = bytes([0, TS_RFC3339, EVENT_PIR]) + rfc3339(pir)  # one timestamp
    sm = bytearray(BPM_SM_UUID) + pir_stamp + bytes([3])  # four counters
    for tag, value in zip((1, 2, 3, 4), diffs_s):
        sm += bytes([tag]) + (value & 0xFFFFFFFF).to_bytes(4, "big")
    erm = bytearray(BPM_ERM_UUID) + pir_stamp + bytes([2])  # three counters
    for tag, value in zip((1, 2, 3), diffs_r):
        erm += bytes([tag]) + (value & 0xFFFFFFFF).to_bytes(4, "big")
    return sei_nal(bytes(ts)) + sei_nal(bytes(sm)) + sei_nal(bytes(erm))


def wall_clock(perf: float) -> float:
    """Convert a time.perf_counter() value to wall-clock seconds."""
    return time.time() - (time.perf_counter() - perf)
