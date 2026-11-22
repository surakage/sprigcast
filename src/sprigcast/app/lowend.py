"""Light settings for a slower PC, and noticing when this is one.

A bare-minimum PC (four CPU threads, 8 GB of memory, or no graphics-card encoder) streams well at
720p and 30 fps with a fast encoder preset; it struggles at 1080p60. ``light_changes`` lists what
differs from that and can apply it. Canvas sizes stay as they are here: streams are scaled down as they
are encoded, and only the frame rate changes. Optimize (optimize.canvas_size_rule) also offers a 720p
canvas on a light PC, with every bloom scaled to fit (SceneCollection.resize_canvas).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sprigcast.core.scene import EncoderSettings, SceneCollection

LIGHT_FPS = 30
LIGHT_SHORT_SIDE = 720  # 1280x720 for a horizontal canvas, 720x1280 for a vertical one
LIGHT_STREAM_KBPS = 4000
LIGHT_RECORDING_KBPS = 8000
LIGHT_PRESET = "p3"
CPU_PRESET = "p2"  # x264 on the CPU: superfast, a third less work than veryfast


OLDER_BROWSER_FPS = 24  # browser sources on an older PC: alerts and widgets still look smooth
OLDER_CLIPS_IDLE = 5  # minutes before an unused clips buffer pauses on an older PC

# CPUs from about 2008-2015 (Core i 1st-4th gen, AMD A-series/FX/Phenom) and low-power ones (Atom,
# Celeron, older Pentium, AMD E-series): what a 2013 all-in-one has. Newer ones are judged by threads.
OLDER_CPU = re.compile(
    r"Core\(TM\) i[3579]-(?:[2-4]\d{3}|\d{3})[A-Z]*\b"  # i5-4570S, i3-3220T, i5-2400S, i5-650
    r"|Core\(TM\)2|\bPentium\b.*\b(?:G[0-3]\d{3}|G\d{3}|[JN][1-3]\d{3}|\d{4}[UM]|B9\d{2})\b|\bCeleron\b|\bAtom\b"
    r"|\bAMD (?:A\d{1,2}|E\d)-\d{4}|\bAMD (?:FX|Phenom|Athlon II|Athlon\(tm\) II|Athlon X\d)",
    re.IGNORECASE)
# Built-in graphics of the same years: Intel HD 2000-5200 (four digits or none), AMD APU Radeon HD
# 7000/8000 "G"/"D" and Radeon R2-R7 Graphics. Intel HD 520 and newer, UHD and Iris Xe are left out.
OLDER_GPU = re.compile(
    r"Intel\(R\) HD Graphics(?: P?\d{4})?\s*$|Intel\(R\) Iris\(TM\)(?: Pro)? Graphics(?: \d{4})?\s*$"
    r"|Radeon(?:\(TM\))? HD [5-8]\d{3}[GD]\b|Radeon(?:\(TM\))? R[2-7] Graphics",
    re.IGNORECASE)
# A graphics card next to the built-in graphics: then the built-in one isn't what draws.
CARD_GPU = re.compile(r"GeForce|Quadro|NVIDIA RTX|Radeon(?:\(TM\))? (?:RX|Pro|R9|R7 \d{3}|HD [5-7]\d{3}\b(?![GD]))|\bArc\b",
                      re.IGNORECASE)


@dataclass
class PcFacts:
    threads: int
    memory_gb: float
    codec: str
    cpu_name: str = ""
    gpus: tuple[str, ...] = ()  # display adapter names, as Windows lists them


def cpu_name() -> str:
    import platform

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or ""


def gpu_names() -> tuple[str, ...]:
    """Display adapters Windows has a driver for (the display device class in the registry)."""
    if sys.platform != "win32":
        return ()
    import winreg

    names = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    for i in range(16):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{base}\\{i:04d}") as k:
                name = str(winreg.QueryValueEx(k, "DriverDesc")[0]).strip()
        except OSError:
            continue
        if name and name not in names:
            names.append(name)
    return tuple(names)


def older_cpu(name: str) -> bool:
    return bool(name and OLDER_CPU.search(name))


def older_gpu(gpus: tuple[str, ...] | list[str]) -> str:
    """The older built-in graphics this PC draws with, or "" (a graphics card, newer graphics, or unknown)."""
    if any(CARD_GPU.search(g) for g in gpus):
        return ""
    return next((g for g in gpus if OLDER_GPU.search(g)), "")


def older_amd_gpu(gpus: tuple[str, ...] | list[str]) -> str:
    """Older AMD built-in graphics (a Radeon R2-R7 or HD xxxxG/D APU) with no graphics card, or ""."""
    gpu = older_gpu(gpus)
    return gpu if "radeon" in gpu.lower() else ""


def cpu_encodes(facts: PcFacts) -> bool:
    """Older AMD built-in graphics: x264 on the CPU streams and records, the chip only draws.

    The chip shares the PC's memory, so drawing and encoding on it starve each other: a tester's Radeon R2
    (AMD E2-7110) lost its AMF stream after a graphics reset and viewers saw nothing until x264 took over
    (2026-11-22). At 720p30 superfast the CPU carries it. Intel's Quick Sync is a block of its own beside the
    drawing part and stays. recorder.pick_codec doesn't try the AMD encoder there either."""
    return bool(older_amd_gpu(facts.gpus))


def to_cpu(enc: EncoderSettings, found: str) -> bool:
    """Whether this encoder setting still encodes on the graphics chip (``found``: what "auto" picks)."""
    return enc.encoder != "libx264" and not (enc.encoder == "auto" and found == "libx264")


def short_gpu(name: str) -> str:
    return re.sub(r"\((?:R|TM)\)", "", name).replace("  ", " ").strip()


def older_reasons(facts: PcFacts) -> list[str]:
    """Signs of a PC from about 2015 or before (like a 2013 all-in-one): older CPU or built-in graphics."""
    reasons = []
    if older_cpu(facts.cpu_name):
        reasons.append("an older CPU")
    gpu = older_gpu(facts.gpus)
    if gpu:
        reasons.append(f"older built-in graphics ({short_gpu(gpu)})")
    return reasons


def pc_facts() -> PcFacts:
    import os

    from sprigcast.output.recorder import pick_codec

    try:
        import psutil

        memory_gb = psutil.virtual_memory().total / 2**30
    except Exception:
        memory_gb = 0.0  # unknown: not counted against the PC
    try:
        gpus = gpu_names()
    except Exception:
        gpus = ()
    return PcFacts(os.cpu_count() or 1, memory_gb, pick_codec(), cpu_name(), gpus)


def weak_reasons(facts: PcFacts) -> list[str]:
    """Why this PC should use light settings; empty when it has room to spare."""
    reasons = []
    if facts.threads <= 4:
        reasons.append(f"{facts.threads} CPU thread{'s' if facts.threads != 1 else ''}")
    if 0 < facts.memory_gb < 9:
        reasons.append(f"{round(facts.memory_gb)} GB of memory")
    if facts.codec == "libx264" and not cpu_encodes(facts):  # older AMD graphics: said by older_reasons
        reasons.append("no graphics-card encoder, so the CPU encodes")
    return reasons + older_reasons(facts)


def on_hard_drive(folder: str | Path) -> bool | None:
    """True when folder is on a spinning hard drive, False on an SSD, None when Windows can't say."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    path = Path(folder)
    while not path.exists() and path.parent != path:
        path = path.parent
    drive = os.path.splitdrive(str(path.resolve()))[0]
    if not drive.endswith(":"):
        return None  # a network share

    class Query(ctypes.Structure):
        _fields_ = [("PropertyId", ctypes.c_int), ("QueryType", ctypes.c_int), ("Extra", ctypes.c_ubyte * 1)]

    class SeekPenalty(ctypes.Structure):
        _fields_ = [("Version", wintypes.DWORD), ("Size", wintypes.DWORD), ("IncursSeekPenalty", ctypes.c_ubyte)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(f"\\\\.\\{drive}", 0, 3, None, 3, 0, None)  # no access needed for this query
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        query, out, got = Query(7, 0), SeekPenalty(), wintypes.DWORD()  # StorageDeviceSeekPenaltyProperty
        ok = k32.DeviceIoControl(wintypes.HANDLE(handle), 0x2D1400, ctypes.byref(query), ctypes.sizeof(query),
                                 ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(got), None)
        return bool(out.IncursSeekPenalty) if ok and got.value >= ctypes.sizeof(out) else None
    finally:
        k32.CloseHandle(wintypes.HANDLE(handle))


def recordings_folder(collection: SceneCollection) -> Path:
    if collection.recording.folder:
        return Path(collection.recording.folder)
    try:
        from PySide6.QtCore import QStandardPaths

        videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    except Exception:
        videos = ""
    return Path(videos or Path.home() / "Videos") / "SprigCast"


def light_size(width: int, height: int) -> tuple[int, int] | None:
    """The size to encode at, or None when it is already 720p or smaller."""
    from sprigcast.output.recorder import shrink_to

    return shrink_to(width, height, LIGHT_SHORT_SIDE)


def _light_encoder(enc: EncoderSettings, apply: bool, preset: str = LIGHT_PRESET) -> bool:
    wanted = {"multipass": "disabled", "lookahead": False, "b_frames": 0}
    if int(enc.preset[1:]) > int(preset[1:]):
        wanted["preset"] = preset
    differs = any(getattr(enc, name) != value for name, value in wanted.items())
    if differs and apply:
        for name, value in wanted.items():
            setattr(enc, name, value)
    return differs


def spare_canvas(collection: SceneCollection):
    """The canvas an older PC can switch off: the vertical one (else the other), when both are on and
    no stream goes out from it. None when only one is on, or both are streamed."""
    on = [c for c in collection.canvases if c.enabled]
    if len(on) < 2:
        return None
    streamed = {o.canvas for o in collection.outputs if o.type == "stream"}  # set up, ticked or not
    from sprigcast.output.enhanced import sends_vertical

    if any(o.type == "stream" and sends_vertical(o) for o in collection.outputs):
        streamed.add("vertical")  # Twitch dual format sends the vertical canvas too
    return next((c for c in sorted(on, key=lambda c: c.id != "vertical") if c.id not in streamed), None)


def older_changes(collection: SceneCollection, apply: bool = False, hdd: bool = False,
                  older: bool = True) -> list[tuple[str, str, str, object]]:
    """Extra savings for an older PC (and for recordings on a hard drive): (group, line, now, apply) rows.

    One canvas instead of two, clips only when pressed, browser sources at OLDER_BROWSER_FPS and, on a
    hard drive, one MKV file per recording. With ``apply`` each is done too."""
    from sprigcast.sources.browser import page_fps

    rows: list[tuple[str, str, str, object]] = []
    r = collection.recording
    spare = spare_canvas(collection) if older else None
    if spare is not None:
        def canvas_off(canvas=spare):
            canvas.enabled = False
            if canvas.id in collection.recording.canvases and len(collection.recording.canvases) > 1:
                collection.recording.canvases = [c for c in collection.recording.canvases if c != canvas.id]
        rows.append(("Video", f"{spare.id.capitalize()} canvas off: one canvas to draw (switch it back on any time)",
                     "Both canvases on", canvas_off))
    if older and (r.clips_on_start or not 0 < r.clips_idle_minutes <= OLDER_CLIPS_IDLE):
        def clips_light():
            r.clips_on_start = False
            r.clips_idle_minutes = OLDER_CLIPS_IDLE
        rows.append(("Recording", f"Clips get ready only when you press Clip, and pause after {OLDER_CLIPS_IDLE} "
                     "minutes unused", "Ready at start" if r.clips_on_start else
                     f"Pause after {r.clips_idle_minutes or 'no'} minutes", clips_light))
    browsers = [src for src in collection.sources if older and src.kind == "browser"
                and page_fps(src.settings) > OLDER_BROWSER_FPS]
    if browsers:
        def browsers_light(sources=browsers):
            for src in sources:
                src.settings = {**src.settings, "custom_fps": True, "fps": OLDER_BROWSER_FPS}
        count = f"{len(browsers)} browser source{'s' if len(browsers) != 1 else ''}"
        rows.append(("Video", f"{count} drawn at {OLDER_BROWSER_FPS} fps",
                     f"{max(page_fps(src.settings) for src in browsers)} fps", browsers_light))
    enhanced = [o for o in collection.outputs if older and o.type == "stream" and o.enhanced]
    if enhanced:
        def enhanced_off(outputs=enhanced):
            for o in outputs:
                o.enhanced = False
        rows.append(("Streaming", "Twitch sent one quality, not several (Enhanced Broadcasting off: each quality is "
                     "another picture made and encoded here)", "Enhanced Broadcasting on", enhanced_off))
    cameras = [src for src in collection.sources if older and src.kind == "camera" and (
        min(int(src.settings.get("width", 1280)), int(src.settings.get("height", 720))) > LIGHT_SHORT_SIDE
        or int(src.settings.get("fps", 30)) > LIGHT_FPS)]
    if cameras:
        def cameras_light(sources=cameras):
            for src in sources:
                w, h = int(src.settings.get("width", 1280)), int(src.settings.get("height", 720))
                smaller = light_size(w, h) or (w, h)
                src.settings = {**src.settings, "width": smaller[0], "height": smaller[1],
                                "fps": min(LIGHT_FPS, int(src.settings.get("fps", 30)))}
        count = f"{len(cameras)} camera{'s' if len(cameras) != 1 else ''}"
        rows.append(("Video", f"{count} read at {LIGHT_SHORT_SIDE}p {LIGHT_FPS} fps", "Bigger or faster", cameras_light))
    from sprigcast.sources.media import is_sound_file

    videos = [src for src in collection.sources if older and src.kind == "media" and src.settings.get("path")
              and not is_sound_file(str(src.settings.get("path"))) and not src.settings.get("hw_decode")]
    if videos:
        # Decoded on the CPU, each picture went up to the graphics card whole: on the tester's Radeon R2 that held
        # the frame for seconds 155 times in 45 minutes (2026-11-22). Decoded on the card, it stays there.
        def videos_on_card(sources=videos):
            for src in sources:
                src.settings = {**src.settings, "hw_decode": True}
        count = f"{len(videos)} video{'s' if len(videos) != 1 else ''}"
        rows.append(("Video", f"{count} decoded on the graphics card (no picture copied up to it each frame)",
                     "Decoded on the CPU", videos_on_card))
    if hdd and (r.container != "mkv" or r.source_record):
        def hdd_light():
            r.container = "mkv"
            r.source_record = False
        rows.append(("Recording", "Recordings saved as one MKV file (suits a hard drive, safe if the PC stops)",
                     r.container.upper() + (", plus a source-record file" if r.source_record else ""), hdd_light))
    if apply:
        for *_text, change in rows:
            change()
    return rows


def light_changes(collection: SceneCollection, apply: bool = False, pending_fps: dict[str, int] | None = None,
                  cpu_encoding: bool = False, older: bool = False, hdd: bool = False,
                  cpu_encodes: bool = False) -> tuple[list[str], dict[str, int]]:
    """What light settings would change, as short lines, and the canvas frame rates to switch to.

    With ``apply`` the collection is changed too, except the canvas frame rates: those change at the
    next start (Settings → Video), so the caller stores them. ``pending_fps`` is what is already waiting.
    ``older`` adds the savings for an older PC and ``hdd`` those for recording to a hard drive (older_changes).
    ``cpu_encodes`` moves streams and recordings to x264 on the CPU (see cpu_encodes()).
    """
    lines: list[str] = []
    if older or hdd:
        rows = older_changes(collection, apply, hdd, older)
        lines += [row[1] for row in rows]
    waiting = pending_fps or {}
    fps = {c.id: LIGHT_FPS for c in collection.canvases if c.enabled and waiting.get(c.id, c.fps) > LIGHT_FPS}
    if fps:
        lines.append(f"{LIGHT_FPS} fps instead of {max(c.fps for c in collection.canvases if c.id in fps)} "
                     "(after a restart)")
    canvases = {c.id: c for c in collection.canvases}
    sized, capped = [], False
    for output in collection.outputs:
        canvas = canvases.get(output.canvas)
        if output.type != "stream" or canvas is None:
            continue
        width, height = output.size or (canvas.width, canvas.height)
        smaller = light_size(width, height)
        if smaller:
            sized.append(f"{smaller[0]}×{smaller[1]}")
            if apply:
                output.size = smaller
        if apply and output.fps and output.fps > LIGHT_FPS:
            output.fps = None  # follow the canvas
        if output.bitrate_kbps > LIGHT_STREAM_KBPS:
            capped = True
            if apply:
                output.bitrate_kbps = LIGHT_STREAM_KBPS
    if sized:
        lines.append(f"Streams sent at {' and '.join(dict.fromkeys(sized))} (your scenes stay as they are)")
    if capped:
        lines.append(f"Stream bitrate {LIGHT_STREAM_KBPS} kbps")
    recorded = [c for c in collection.canvases if c.enabled and c.id in collection.recording.canvases]
    if any(light_size(c.width, c.height) for c in recorded) and (
            not collection.recording.short_side or collection.recording.short_side > LIGHT_SHORT_SIDE):
        lines.append(f"Recordings and replays saved at {LIGHT_SHORT_SIDE}p")
        if apply:
            collection.recording.short_side = LIGHT_SHORT_SIDE
    if collection.recording.bitrate_kbps > LIGHT_RECORDING_KBPS:
        lines.append(f"Recording bitrate {LIGHT_RECORDING_KBPS} kbps")
        if apply:
            collection.recording.bitrate_kbps = LIGHT_RECORDING_KBPS
    moved = [e for e in (collection.stream_encoder, collection.recording.encoder)
             if cpu_encodes and to_cpu(e, "libx264" if cpu_encoding else "")]
    if moved:
        lines.append("Encoded with x264 on the CPU: the built-in graphics only draws")
        if apply:
            for e in moved:
                e.encoder = "libx264"
    cpu_encoding = cpu_encoding or cpu_encodes
    preset = CPU_PRESET if cpu_encoding else LIGHT_PRESET
    stream = _light_encoder(collection.stream_encoder, apply, preset)
    recording = _light_encoder(collection.recording.encoder, apply, preset)
    if stream or recording:
        lines.append("Fastest encoder settings: " + ("superfast preset" if cpu_encoding else "fast preset")
                     + ", no look-ahead, multipass or B-frames")
    return lines, fps


def store_fps(fps: dict[str, int], collection=None, sizes: dict[str, tuple[int, int]] | None = None) -> bool:
    """Queue canvas frame rates for the next start, next to anything Settings → Video already queued. With the
    collection given, a change that asks for what a canvas already is isn't kept (60 → 30 → 60 with no restart
    in between left "60 fps" waiting on a 60 fps canvas, PC 2026-11-19). True when something waits afterwards."""
    import json
    import logging

    from PySide6.QtCore import QSettings

    from sprigcast.app.settings_window import PENDING_VIDEO, pending_video

    if not fps and not sizes:
        return bool(pending_video())
    pending = pending_video()
    for canvas_id, value in fps.items():
        pending.setdefault(canvas_id, {})["fps"] = value
    for canvas_id, (width, height) in (sizes or {}).items():
        pending.setdefault(canvas_id, {}).update(width=width, height=height)
    if collection is not None:
        for canvas in collection.canvases:
            now = {"width": canvas.width, "height": canvas.height, "fps": canvas.fps, "enabled": canvas.enabled}
            change = {k: v for k, v in pending.get(canvas.id, {}).items() if now.get(k, v) != v}
            if change:
                pending[canvas.id] = change
            else:
                pending.pop(canvas.id, None)
    if pending:
        QSettings().setValue(PENDING_VIDEO, json.dumps(pending))
    else:
        QSettings().remove(PENDING_VIDEO)
    logging.getLogger(__name__).info("video: canvas changes queued for the next start: %s", pending or "nothing")
    return bool(pending)


def waiting_fps() -> dict[str, int]:
    from sprigcast.app.settings_window import pending_video

    return {cid: int(change["fps"]) for cid, change in pending_video().items() if "fps" in change}


# How much this PC can carry, shown on Settings → General → This PC. Optimize and Light settings store
# the tier they set; until then it's worked out from the PC itself.
TIERS = {"light": "Light", "balanced": "Balanced", "best": "Best"}
TIER_SETTING = "general/pc_tier"


def tier(facts: PcFacts) -> str:
    """"light" for a weak or older PC, "best" for one with room to spare, else "balanced"."""
    from sprigcast.output.recorder import is_hardware

    if weak_reasons(facts):
        return "light"
    if facts.threads >= 12 and facts.memory_gb >= 15 and is_hardware(facts.codec):
        return "best"
    return "balanced"


def stored_tier() -> str | None:
    from PySide6.QtCore import QSettings

    value = QSettings().value(TIER_SETTING, "", type=str)
    return value if value in TIERS else None


def store_tier(value: str) -> None:
    from PySide6.QtCore import QSettings

    if value in TIERS:
        QSettings().setValue(TIER_SETTING, value)
