# SprigCast

A lightweight, open-source streaming app for Windows, in the spirit of OBS, Meld and Streamlabs.
SprigCast renders a **horizontal** and a **vertical** canvas at the same time, and each output
(Twitch, TikTok, a recording) streams from the canvas you choose.

Status: early development. This code covers milestones M0 to M2 of the build guide:
toolchain checks, a GPU canvas with live previews, and scenes built from color and image sources.

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

## Using the app (M2)

- **Previews**: the horizontal canvas (1920x1080) on the left, the vertical canvas (1080x1920) on the right.
- **Scenes dock**: scenes grouped by canvas. Select one to edit it; double-click or press *Show* to put it on its canvas.
  A scene linked to another switches together with it, so one click changes both canvases.
- **Sources dock**: add color or image sources to the selected scene, tick to show or hide,
  and use *Up* and *Down* to change which item draws on top.
- **Properties dock**: name, position, scale and rotation of the selected item, plus its color or image file.

Scenes save automatically to `scenes.json` in your AppData folder
(*File → Show scenes file location* shows the exact path).

## Project layout

```
src/sprigcast/
  core/      scene collection model (canvases, sources, scenes, outputs), Source base class, mailbox, events
  render/    moderngl compositor and the render engine thread
  sources/   built-in sources: color, image
  app/       PySide6 main window and canvas previews
  tools/     sprigcast-check and sprigcast-teststream
  data/      default scene collection used on first run
tests/       pytest suite (GPU tests skip when no OpenGL context is available)
```

Run the tests with `uv run pytest`.

## Roadmap

M3 screen and window capture · M4 drag-to-edit in the preview · M5 camera · M6 audio mixer ·
M7 NVENC encoding and recording · M8 RTMP streaming · M9 vertical canvas outputs and multistream ·
M10 transitions, hotkeys, stats · M11 plugins and polish.

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
