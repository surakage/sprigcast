# SprigCast

A lightweight, open-source streaming app for Windows, in the spirit of OBS, Meld and Streamlabs.
SprigCast renders a **horizontal** and a **vertical** canvas at the same time, and each output
(Twitch, TikTok, a recording) streams from the canvas you choose.

Status: early development. This code covers milestones M0 to M8 of the build guide:
toolchain checks, a GPU canvas with live previews, scenes built from color and image sources,
display and window capture, webcams, mouse editing in the previews, an audio mixer, recording, and live streaming.

## Requirements

- Windows 10 or 11, 64-bit
- An NVIDIA GPU with a recent driver (other GPUs work for the previews; NVENC is used for streaming later)
- [uv](https://docs.astral.sh/uv/), Git, and the FFmpeg CLI for the test stream:

```powershell
winget install astral-sh.uv Git.Git Gyan.FFmpeg
```

## Run it

```powershell
git clone https://github.com/surakage/sprigcast
cd sprigcast
uv sync                 # installs Python 3.12 and every library into .venv
uv run sprigcast-check  # M0: confirms PyAV can encode on your NVIDIA GPU
uv run sprigcast        # opens the app
```

To prove your stream key and network work before streaming from the app itself:

```powershell
uv run sprigcast-teststream --service twitch            # 1280x720 test pattern and tone
uv run sprigcast-teststream --service twitch --vertical # 720x1280
```

The key is typed at a hidden prompt and is never saved.

## Using the app

- **Previews**: the horizontal canvas (1920x1080) on the left, the vertical canvas (1080x1920) on the right.
  Edit the scene shown on each canvas directly with the mouse:
  - Click an item to select it, then drag to move it. Edges and centre snap to the canvas; hold Ctrl to stop snapping.
  - Drag a corner handle to resize. Hold Alt while dragging a corner to crop instead.
  - Arrow keys nudge the selected item 1 px, or 10 px with Shift.
  - Tick *Locked* in Properties for backgrounds, so clicks pass through them to the items on top.
- **Scenes dock**: scenes grouped by canvas. Select one to edit it; double-click or press *Show* to put it on its canvas.
  A scene linked to another switches together with it, so one click changes both canvases.
- **Sources dock**: add a display, window, color or image source to the selected scene, tick to show or hide,
  and use *Up* and *Down* to change which item draws on top.
  - *+ Display* captures a whole monitor, cursor included, and fits it to the canvas.
  - *+ Window* captures one window even when other windows cover it. SprigCast remembers the window's
    program and title, so a game that restarts or a browser that changes tabs is found again automatically.
  - *+ Camera* adds a webcam or capture card as a facecam in the bottom-right corner. Pick the device,
    mode (for example 1920x1080 at 30 fps) and mirroring. Cameras are remembered by device, so
    replugging into another USB port is fine, and an unplugged camera reconnects on its own.
- **Audio dock**: a level meter for every audio source plus the master mix.
  - *+ Mic* adds a microphone, *+ Desktop* adds what your speakers or headphones play (WASAPI loopback).
    Both default to whatever device Windows uses, so switching headsets in Windows just works.
  - Drag a slider to set the volume (-60 to +12 dB), click *Mute* to silence it,
    and use *Device…* to pin a source to one device or *Remove* to delete it.
  - Audio sources belong to the whole collection, not one scene, so they keep playing when you switch scenes.
- **Output dock**: go live and record.
  - *Stream…* sets up where you stream: Twitch, YouTube, Kick, TikTok or any RTMP server, which canvas each
    one uses, and its bitrate. Stream keys are saved in Windows Credential Manager, never in `scenes.json`.
  - Tick the places to stream to and press *Go live*. Each one gets its own NVENC encoder (constant bitrate,
    keyframe every 2 s), so Twitch can take the horizontal canvas while TikTok takes the vertical one.
    The status shows the live bitrate per output; a dropped connection reconnects on its own.
  - *Start recording* writes each ticked canvas to its own file at the same time,
    for example `SprigCast 2026-11-10 19-30-05 horizontal.mkv` and `... vertical.mkv`, both with the mixed audio.
    *Recording…* picks the folder (default `Videos\SprigCast`), bitrate and MKV or MP4; MKV stays playable
    if the PC crashes. *Folder* opens your recordings. Streaming and recording can run together.
- **Properties dock**: name, position, scale and rotation of the selected item, its color, image, monitor or
  window, *Fit to canvas*, and a status line (for example "waiting for target" while a game is closed).

Capture uses Windows Graphics Capture. SprigCast hides the yellow capture border on Windows 11;
Windows 10 always shows it.

Scenes save automatically to `scenes.json` in your AppData folder
(*File → Show scenes file location* shows the exact path).

## Project layout

```
src/sprigcast/
  core/      scene collection model (canvases, sources, scenes, outputs), Source base class, mailbox, events
  render/    moderngl compositor and the render engine thread
  sources/   built-in sources: color, image, display and window capture, camera, mic and desktop audio
  audio/     mixer that combines audio sources into one 48 kHz stereo stream
  output/    recorder (MKV/MP4) and RTMP streamer: H.264 (NVENC or x264) and AAC, stream keys via keyring
  app/       PySide6 main window and canvas previews
  tools/     sprigcast-check and sprigcast-teststream
  data/      default scene collection used on first run
tests/       pytest suite (GPU tests skip when no OpenGL context is available)
```

Run the tests with `uv run pytest`.

## Roadmap

M9 multistream polish (platform presets, stream health, bandwidth test) ·
M10 transitions, hotkeys, stats · M11 plugins and polish.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
