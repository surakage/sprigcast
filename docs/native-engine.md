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

The checks draw with Windows' software renderer (WARP), so they give the same answer on every PC and in CI
(`.github/workflows/core.yml`). The installer build puts the DLL in `sprigcast\engine\`.

## Phases

Ordered by how much heavy-run stalling each removes. The plan: https://claude.ai/artifact/3zTFrUsXtNhCzszAczL7r7

| Phase | What moves | State |
| --- | --- | --- |
| 1 | Groundwork: build, checks, Python link, engine switch, first canvas | Done |
| 2 | Direct3D 11 compositor and previews, feeding NVENC, AMF and QSV on the graphics card | Next |
| 3 | Media and GIF decode, browser pages as shared textures | |
| 4 | Recording, streaming, Enhanced Broadcasting, replay, virtual camera | |
| 5 | Per-frame plugin painting (store plugins native, your Python plugins in a helper process) | |
| 6 | Sound: devices, mixer, filters, sync | |
| 7 | Capture and camera inside the engine | |
| 8 | Old Python engine and its packages removed | |
| 9 | Only if needed: Control API into the engine, screens to Qt C++ | |
