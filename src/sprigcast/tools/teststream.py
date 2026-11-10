"""M0 test stream: `uv run sprigcast-teststream`. Sends a test pattern and tone to your ingest URL.

Your stream key is typed at a hidden prompt and only passed to ffmpeg; it is never saved or printed.
"""

from __future__ import annotations

import argparse
import getpass
import shutil
import subprocess
import sys

INGESTS = {
    "twitch": "rtmp://live.twitch.tv/app",
    "youtube": "rtmp://a.rtmp.youtube.com/live2",
}


def build_command(url: str, encoder: str, size: str, fps: int, bitrate_kbps: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats",
        "-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}",
        "-f", "lavfi", "-i", "sine=frequency=440",
        "-c:v", encoder, *(["-preset", "p5"] if "nvenc" in encoder else ["-preset", "veryfast"]),
        "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k", "-bufsize", f"{bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p", "-g", str(fps * 2),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-f", "flv", url,
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", choices=sorted(INGESTS), default="twitch")
    parser.add_argument("--ingest", help="custom ingest URL without the key (overrides --service)")
    parser.add_argument("--encoder", default="h264_nvenc", help="h264_nvenc (default) or libx264")
    parser.add_argument("--vertical", action="store_true", help="send 720x1280 instead of 1280x720")
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=3000, help="video bitrate in kbps")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("ffmpeg was not found. Install it with: winget install Gyan.FFmpeg  (then reopen PowerShell)")
        return 1
    key = getpass.getpass("Stream key (hidden): ").strip()
    if not key:
        print("No key entered.")
        return 1
    url = f"{(args.ingest or INGESTS[args.service]).rstrip('/')}/{key}"
    size = "720x1280" if args.vertical else "1280x720"
    print(f"Streaming a {size} test pattern with {args.encoder}. Press q or Ctrl+C to stop.")
    try:
        return subprocess.call(build_command(url, args.encoder, size, args.fps, args.bitrate))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
