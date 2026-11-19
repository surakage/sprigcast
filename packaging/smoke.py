"""Start the built SprigCast for 30 seconds, with a video playing, and fail on any start-up error.

Usage: smoke.py <seconds> <command...>   (CI: the bundled python.exe runs this with SprigCast.exe as the command)

The checks before it import SprigCast's pieces one at a time. This one starts the whole app the way a
person does, so what only goes wrong when everything starts at once shows up here: imports racing on
different threads (a half-initialized PyAV, 2026-11-17), a missing DLL that's only loaded lazily, a
crash in the first seconds.

1. A short video (picture and sound) is made in another process, so this one never imports PyAV first.
2. The garden gets a sprig playing it, on top of the bloom on air; Getting started is marked as done.
3. SprigCast starts offscreen and must still be running after <seconds>.
4. Its log for the run must have no errors, no import trouble and no media that couldn't play, and it
   must say which graphics it drew with. crash.log must not have grown.
The whole log is printed either way.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CLIP = """
import sys, av, numpy as np
out = av.open(sys.argv[1], "w")
video = out.add_stream("mpeg4", rate=30)
video.width, video.height, video.pix_fmt = 320, 180, "yuv420p"
sound = out.add_stream("aac", rate=48000)
sound.layout = "stereo"
for i in range(90):
    picture = np.zeros((180, 320, 3), np.uint8)
    picture[:, :, 1] = 80 + i
    for packet in video.encode(av.VideoFrame.from_ndarray(picture, format="rgb24")):
        out.mux(packet)
t = np.arange(48000 * 3) / 48000
tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
for start in range(0, len(tone), 1024):
    chunk = np.ascontiguousarray(np.tile(tone[start:start + 1024], (2, 1)))
    frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
    frame.sample_rate = 48000
    for packet in sound.encode(frame):
        out.mux(packet)
for stream in (video, sound):
    for packet in stream.encode():
        out.mux(packet)
out.close()
"""

SETUP = """
import json, sys
from PySide6.QtCore import QCoreApplication, QSettings
QCoreApplication.setOrganizationName("Slothbloom")
QCoreApplication.setApplicationName("SprigCast")
from sprigcast.app import getting_started, logbook
from sprigcast.app.main import config_dir, load_collection
from sprigcast.core.gardens import Shelf
from sprigcast.core.scene import SourceConfig
QSettings().setValue(getting_started.DONE, True)  # its window would ask to replace the blooms
path = Shelf(config_dir()).garden_file()
path.parent.mkdir(parents=True, exist_ok=True)
garden = load_collection(path)
canvas = next(c for c in garden.canvases if c.enabled)
bloom = garden.scene(garden.active_scenes.get(canvas.id) or garden.scenes_for(canvas.id)[0].id)
clip = SourceConfig(id=garden.new_id("media"), kind="media", name="Smoke clip",
                    settings={"path": sys.argv[1], "loop": True})
garden.sources.append(clip)
garden.own_sprig(bloom, clip.id, (320, 180), clip.name)
garden.save(path)
print(json.dumps({"garden": str(path), "logs": str(logbook.log_dir()), "bloom": bloom.name}))
"""

# In a WARNING line: trouble that must never happen at start-up.
BAD = re.compile(r"partially initialized|circular import|ImportError|ModuleNotFoundError|DLL load failed"
                 r"|could not be played|isn't available|OpenGL 3\.3 unavailable|Traceback", re.IGNORECASE)
LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (ERROR|CRITICAL|WARNING) ")


def problems(text: str) -> list[str]:
    """The lines in a run's log that fail the check (an error, or a warning about import or media trouble)."""
    found = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        level = LEVEL.match(line)
        if level is None:
            continue
        follow = "\n".join(lines[i + 1:i + 30]).split("\n20", 1)[0]  # a traceback written under it
        if level.group(1) != "WARNING" or BAD.search(line) or BAD.search(follow):
            found.append(line)
    return found


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def main() -> int:
    seconds = float(sys.argv[1])
    command = sys.argv[2:]
    work = Path(tempfile.mkdtemp(prefix="sprigcast-smoke-"))
    clip = work / "smoke clip.mp4"
    subprocess.run([sys.executable, "-I", "-c", CLIP, str(clip)], check=True, timeout=120)
    print(f"Made {clip} ({size(clip) // 1024} KB)", flush=True)
    paths = json.loads(subprocess.run([sys.executable, "-I", "-c", SETUP, str(clip)], check=True, timeout=120,
                                      capture_output=True, text=True).stdout.strip().splitlines()[-1])
    print(f"Garden {paths['garden']}: the clip plays on top of {paths['bloom']}", flush=True)
    logs = Path(paths["logs"])
    day_log = logs / f"sprigcast {time.strftime('%Y-%m-%d')}.log"
    crash_log = logs / "crash.log"
    log_from, crash_from = size(day_log), size(crash_log)

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    # SprigCast's output goes to a file, not this console: a helper it starts that outlives it would otherwise
    # hold the console pipe open, and the CI step would wait for it forever (2026-11-19).
    out_file = work / "sprigcast output.txt"
    with open(out_file, "wb") as out:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT)
    started = time.monotonic()
    while time.monotonic() - started < seconds and proc.poll() is None:
        time.sleep(0.5)
    alive = proc.poll() is None
    if alive:
        if sys.platform == "win32":  # with the helpers it started
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=60)
        else:
            proc.kill()
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            print("SprigCast was still running 30 s after it was ended", flush=True)
    else:
        print(f"SprigCast closed by itself after {time.monotonic() - started:.0f} s (exit {proc.returncode})")

    printed = out_file.read_bytes().decode("utf-8", "replace").strip() if out_file.is_file() else ""
    if printed:
        print(f"----- what SprigCast printed -----\n{printed[-6000:]}\n-----", flush=True)
    text = day_log.read_bytes()[log_from:].decode("utf-8", "replace") if day_log.is_file() else ""
    print(f"----- {day_log.name} (this run) -----\n{text}----- end of log -----", flush=True)
    failed = [] if alive else ["SprigCast didn't stay open"]
    if not text:
        failed.append(f"nothing was written to {day_log}")
    elif "graphics: " not in text:
        failed.append("the log doesn't say which graphics it drew with: the renderer didn't start")
    failed += problems(text)
    if size(crash_log) > crash_from:
        crash = crash_log.read_bytes()[crash_from:].decode("utf-8", "replace")
        print(f"----- crash.log -----\n{crash[-6000:]}")
        if "Fatal Python error" in crash or "Windows fatal exception" in crash:
            failed.append("crash.log has a crash")
    for line in failed:
        print(f"FAILED: {line}")
    if not failed:
        print(f"Start-up check OK: SprigCast ran {seconds:.0f} s with a video playing and logged no errors")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
