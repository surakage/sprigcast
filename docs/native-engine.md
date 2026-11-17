# The native engine

SprigCast's engine is moving from Python to C++, one part at a time. Everything that touches frames or sound
moves into `sprigcore.dll` (`native/core`) and runs on the engine's own threads. Python keeps the screens,
settings, plugins, Gardens and Paths import and the Control API, and only sends the engine commands. It is the
same split as OBS: libobs in C and C++, the Qt screens on top.

Why: Python runs one piece of Python at a time (the GIL), so rendering, encoding, sound, the screens and plugins
all take turns on one lock. Most of the hard stalls (QPainter in plugins, the process scan at start, #459's
switch interval, the heavy-run read-backs) were that lock. In the engine, a slow screen or plugin can't drop a
frame.

## Rules

- **Our own code.** The engine is SprigCast's own Python converted to C++. OBS is studied for ideas only; no OBS
  code is copied, and SprigCast is not an OBS clone.
- **Frozen snapshots.** The engine draws from its own copy of what is shown (`render/native_snapshot.py` builds it
  on each change), never from Python's live objects, so no Python lock is held per frame.
- **Crash gates.** Every exported function catches C++ exceptions and, built with `/EHa`, Windows faults. A device
  that faulted is shut off (`SC_ERR_FAULTED`), a lost graphics card answers `SC_ERR_DEVICE_LOST`, and Python
  makes a new device; the app goes on. Each later phase adds its own gates (thread guards, watchdogs, helper
  processes, encoder fallback); see the plan.
- **The engine never waits on Python.** It never calls into Python. Its log lines wait in a queue that
  `sprigcore.drain_log()` moves into SprigCast's log.
- **No frames or sound cross into Python** on the frame path. Reading a canvas back is for tests and screenshots
  only.
- **One C interface**, `native/core/include/sprigcore.h`, reached only from `src/sprigcast/sprigcore.py` with
  ctypes (which lets go of the GIL for each call). Every function returns `SC_OK` or an error code, with
  `sc_last_error()` saying what went wrong. No C++ exception crosses it. Bump `SC_INTERFACE_VERSION` and
  `sprigcore.INTERFACE_VERSION` together when it changes.
- **The app always works.** Each part that moves has an old and a new path. `SPRIGCAST_ENGINE=native` picks the
  engine; anything else, a missing DLL or an older one, means the Python engine. The Python path for a part is
  deleted only after the native one passes its PC tests.
- **Old PCs first.** Direct3D 11 at feature level 10.0 or better, no runtime to install (static C runtime), and
  every part checked on the AMD E2-7110 and the 2013 all-in-one as well as the main PC.
- **Safety nets.** Warnings are errors, every check also runs under AddressSanitizer in CI, Release builds keep a
  `.pdb` (CI artifact) so crash dumps (`app/crash_dump.py`) name engine functions.

## Building and checking

```
cmake -S native/core -B build/core -A x64
cmake --build build/core --config Release
ctest --test-dir build/core -C Release --output-on-failure
set SPRIGCAST_CORE_DLL=%CD%\build\core\Release\sprigcore.dll
uv run pytest tests/test_sprigcore.py
```

`tests/test_native_compositor.py` draws the cases in `tests/native_cases.py` with the engine and compares them
with `tests/native_golden/`, today's compositor's drawing of the same cases. If a change to
`render/compositor.py` is meant, remake those pictures with `python tests/native_cases.py` and change the engine's
shader (`native/core/src/shader_compositor.h`) to match.

The checks draw with Windows' software renderer (WARP), so they give the same answer on every PC and in CI
(`.github/workflows/core.yml`). The installer build puts the DLL in `sprigcast\engine\`.

## The frame loop (`sc_engine_*`)

The engine draws on its own thread, paced by a high-resolution timer (Windows 10 1803+, else a 1 ms system
tick). Python's calls only copy work into a queue (pictures, looks, shapes, the snapshot, canvas sizes, previews);
the frame thread takes the whole queue at the start of a frame, so Python never waits on the graphics card and the
frame thread never waits on Python. A source uploaded twice before a frame only sends the newest picture. A frame
that runs long skips the frame times it missed (counted as `late`) instead of queueing them. With no canvases and
no previews the thread sleeps.

Previews are swap chains on the windows Python gives (flip model where Windows has it), drawn fitted with bars
and shown without waiting: a window that isn't ready skips that frame. `max_fps` lowers a preview's rate.

Crash gates in the loop: a C++ exception, an access violation or a lost graphics card in a frame drops that
device; the next frame builds a new one and hands it back everything kept (pictures uploaded with
`SC_UPLOAD_KEEP`, looks, shapes, the snapshot; video sends its next frame anyway). More than five rebuilds in a
minute, or no device after ten tries, and the engine stops and reports `faulted`, for Python to switch to its own
engine. A watchdog thread reports `hung` while one frame has taken more than 2 s. `sc_engine_debug_fault`
triggers each of these in the checks.

## In the app (`SPRIGCAST_ENGINE=native`)

render/native_bridge.py: the render thread hands every new source picture to the native engine too, and a
snapshot of what is on air (blooms and every sprig layout, as their Live state shows them) whenever that or the
filters change. The native engine draws the canvases and copies preview-sized pictures back without waiting on
the card (`sc_engine_canvas_watch` / `_take`); the screens show those, so Python's compositor no longer draws a
canvas only to show it. It still draws recordings and streams, transitions, Build's edited bloom, the Part
editor, screenshots and the virtual camera until those move. If the native engine stops (faulted), Python's
compositor draws the previews again by itself.

## Phases

Ordered by how much heavy-run stalling each removes. The plan: https://claude.ai/artifact/3zTFrUsXtNhCzszAczL7r7

| Phase | What moves | State |
| --- | --- | --- |
| 1 | Groundwork: build, checks, Python link, engine switch, first canvas | Done |
| 2 | Frozen scene snapshot; Direct3D 11 compositor and previews, feeding NVENC, AMF and QSV on the graphics card | In progress: compositor and snapshot drawn like today's (pixel-checked); the engine's own frame loop and previews (`sc_engine_*`, not yet used by the app) |
| 3 | Media and GIF decode and reformatting, browser pages as shared textures | |
| 4 | Recording, replay (with its sound writing), streaming, Enhanced Broadcasting, virtual camera | |
| 5 | Sound: devices, mixer, filters, sync | |
| 6 | Per-frame plugin painting (store plugins native, your Python plugins in a helper process) | |
| 7 | Capture and camera inside the engine | |
| 8 | Old Python engine and its packages removed | |
| 9 | Only if needed: Control API into the engine, screens to Qt C++ | |
