"""Start the built SprigCast for 30 seconds, with a video playing, and fail on any start-up error.

Usage: smoke.py <seconds> <command...>   (CI: the bundled python.exe runs this with SprigCast.exe as the command)

The checks before it import SprigCast's pieces one at a time. This one starts the whole app the way a
person does, so what only goes wrong when everything starts at once shows up here: imports racing on
different threads (a half-initialized PyAV, 2026-11-17), a missing DLL that's only loaded lazily, a
crash in the first seconds.

1. A short video (picture and sound) is made in another process, so this one never imports PyAV first.
2. The garden gets a sprig playing it, on top of the bloom on air; Getting started is marked as done.
3. SprigCast starts offscreen and must still be running after <seconds>.
   With SMOKE_LIGHT=1 the canvases are made 640x360 at 30 fps (CI runs it with SPRIGCAST_ENGINE=native: the
   new engine on the build machine's software renderer, which can't keep up with full-size canvases).
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
import threading
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
from sprigcast.app import crash_report
# A run this check ended before (it's ended by force) would make this one ask about safe mode and wait.
(config_dir() / crash_report.MARK).unlink(missing_ok=True)
path = Shelf(config_dir()).garden_file()
path.parent.mkdir(parents=True, exist_ok=True)
garden = load_collection(path)
if sys.argv[2] == "light":  # small canvases at 30 fps: what even the software renderer keeps up with
    for c in garden.canvases:
        c.width, c.height = (640, 360) if c.width >= c.height else (360, 640)
        c.fps = 30
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


# The capture helper (app/capture_helper.py) is in the installed program and starts: it says "ready" and ends.
HELPER = """
import subprocess, sys
from sprigcast.app import capture_helper
command = capture_helper.helper_command()
if command is None:
    sys.exit("SprigCastCapture.exe isn't next to sprigcore.dll")
out = subprocess.run([*command, "--check"], capture_output=True, text=True, timeout=30)
if out.returncode != 0 or "ready" not in out.stdout:
    sys.exit(f"SprigCastCapture.exe --check: exit code {out.returncode}, said {out.stdout!r} {out.stderr!r}")
print(command[0])
"""


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


HANG_AFTER_S = 150.0  # beyond the run itself: a check stuck this long prints what it has and fails (a CI build hung
# here for 24 min with nothing to show, 2026-11-19)
_stage = ["starting"]
_day_log: list[Path] = []


def stage(text: str) -> None:
    _stage[0] = text
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def _hung(seconds: float) -> None:
    print(f"FAILED: the start-up check hung for {seconds:.0f} s at: {_stage[0]}", flush=True)
    if _day_log and _day_log[0].is_file():
        text = _day_log[0].read_bytes()[-20000:].decode("utf-8", "replace")
        print(f"----- {_day_log[0].name} (last 20 KB) -----\n{text}----- end of log -----", flush=True)
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/V"], capture_output=True, text=True, timeout=20, check=False).stdout
            print("----- processes -----\n" + "\n".join(line for line in out.splitlines()
                                                         if "python" in line.lower() or "sprig" in line.lower()))
        except (OSError, subprocess.SubprocessError):
            pass
    os._exit(2)


def size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def main() -> int:
    seconds = float(sys.argv[1])
    command = sys.argv[2:]
    watchdog = threading.Timer(seconds + HANG_AFTER_S, _hung, args=(seconds + HANG_AFTER_S,))
    watchdog.daemon = True
    watchdog.start()
    work = Path(tempfile.mkdtemp(prefix="sprigcast-smoke-"))
    clip = work / "smoke clip.mp4"
    stage("making the test video")
    subprocess.run([sys.executable, "-I", "-c", CLIP, str(clip)], check=True, timeout=120)
    print(f"Made {clip} ({size(clip) // 1024} KB)", flush=True)
    if sys.platform == "win32":
        stage("checking the capture helper")
        checked = subprocess.run([sys.executable, "-I", "-c", HELPER], capture_output=True, text=True, timeout=120,
                                 check=False)
        if checked.returncode != 0:
            print(f"SMOKE TEST FAILED: the capture helper doesn't start: {checked.stderr.strip()}", flush=True)
            return 1
        print(f"The capture helper starts ({checked.stdout.strip()})", flush=True)
    stage("setting up the garden")
    light = os.environ.get("SMOKE_LIGHT") == "1"
    paths = json.loads(subprocess.run([sys.executable, "-I", "-c", SETUP, str(clip), "light" if light else "full"],
                                      check=True, timeout=120,
                                      capture_output=True, text=True).stdout.strip().splitlines()[-1])
    print(f"Garden {paths['garden']}: the clip plays on top of {paths['bloom']}", flush=True)
    logs = Path(paths["logs"])
    day_log = logs / f"sprigcast {time.strftime('%Y-%m-%d')}.log"
    crash_log = logs / "crash.log"
    log_from, crash_from = size(day_log), size(crash_log)
    _day_log.append(day_log)

    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    stage(f"running SprigCast for {seconds:.0f} s")
    began = time.time()
    # The app writes to a file, not this step's console: a helper that outlives taskkill (stuck in the
    # graphics driver, say) would otherwise hold the console open and the CI step waits on it for ever.
    output = work / "app output.txt"
    with output.open("wb") as sink:
        proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT)
    started = time.monotonic()
    while time.monotonic() - started < seconds and proc.poll() is None:
        time.sleep(0.5)
    alive = proc.poll() is None
    if alive:
        stage("closing SprigCast")
        try:
            if sys.platform == "win32":  # with the helpers it started
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30, check=False)
            else:
                proc.kill()
        except subprocess.TimeoutExpired:
            print("taskkill didn't finish in 30 s", flush=True)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            print("SprigCast was still running 30 s after it was ended", flush=True)
            proc.kill()
        stage("SprigCast closed")
    else:
        print(f"SprigCast closed by itself after {time.monotonic() - started:.0f} s (exit {proc.returncode})")

    watchdog.cancel()
    said = output.read_bytes()[-20000:].decode("utf-8", "replace").strip() if output.is_file() else ""
    if said:
        print(f"----- what SprigCast printed -----\n{said}\n----- end -----", flush=True)
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
        # Python's faulthandler also writes "Windows fatal exception" for native exceptions that are caught and
        # handled (it sees them first): with SprigCast still running and no Python stack, that's not a crash.
        python_stack = "most recent call first" in crash
        if "Fatal Python error" in crash or ("Windows fatal exception" in crash and (python_stack or not alive)):
            failed.append("crash.log has a crash")
        elif "Windows fatal exception" in crash:
            print("NOTE: crash.log has native exceptions that were handled (SprigCast kept running); not a crash")
    dumps = sorted(p.name for p in logs.glob("*.dmp") if p.stat().st_mtime >= began)
    if dumps:
        print(f"crash dumps written during this run (kept with the logs when the check fails): {', '.join(dumps)}")
    for line in failed:
        print(f"FAILED: {line}")
    if not failed:
        print(f"Start-up check OK: SprigCast ran {seconds:.0f} s with a video playing and logged no errors")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
