"""Can this Qt WebEngine play H.264/AAC? Opens a page with a small H.264+AAC MP4 (made with PyAV) and asks.

Usage: webengine_probe.py [--expect-h264]
Prints what the page engine says; with --expect-h264 it exits 1 unless the MP4 actually loads (its first
picture decoded, 64x64) and canPlayType / MediaSource say yes. Used after the codec build (webengine-codecs.yml)
and on the installed copy (windows-installer.yml).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def make_clip(path: Path) -> None:
    import av
    import numpy as np

    with av.open(str(path), "w", format="mp4") as out:
        video = out.add_stream("libx264" if "libx264" in av.codecs_available else "h264", rate=10)
        video.width = video.height = 64
        video.pix_fmt = "yuv420p"
        sound = out.add_stream("aac", rate=48000)
        sound.layout = "stereo"
        for i in range(10):
            picture = av.VideoFrame.from_ndarray(np.full((64, 64, 3), i * 20, dtype=np.uint8), format="rgb24")
            for packet in video.encode(picture):
                out.mux(packet)
        samples = np.zeros((2, 1024), dtype=np.float32)
        for i in range(47):
            frame = av.AudioFrame.from_ndarray(samples, format="fltp", layout="stereo")
            frame.sample_rate, frame.pts = 48000, i * 1024
            for packet in sound.encode(frame):
                out.mux(packet)
        for stream in (video, sound):
            for packet in stream.encode():
                out.mux(packet)


PAGE = """<body><video id=v muted autoplay playsinline src="clip.mp4"></video><script>
const v = document.getElementById('v');
const r = {
  canPlay: v.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  mse: !!(window.MediaSource && MediaSource.isTypeSupported('video/mp4; codecs="avc1.42E01E"')),
  mseAac: !!(window.MediaSource && MediaSource.isTypeSupported('audio/mp4; codecs="mp4a.40.2"')),
};
const done = (how) => { r.result = how; r.size = [v.videoWidth, v.videoHeight];
  r.error = v.error ? v.error.code + ' ' + v.error.message : ''; document.title = 'probe:' + JSON.stringify(r); };
v.addEventListener('loadeddata', () => done('loaded'));
v.addEventListener('error', () => done('error'));
setTimeout(() => done('timeout'), 20000);
</script></body>"""


def main(argv: list[str]) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox")
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWidgets import QApplication

    folder = Path(tempfile.mkdtemp(prefix="sprigcast-probe-"))
    make_clip(folder / "clip.mp4")
    (folder / "page.html").write_text(PAGE, "utf-8")
    app = QApplication([sys.argv[0]])
    page = QWebEnginePage()
    found: dict = {}

    def titled(title: str) -> None:
        if title.startswith("probe:") and not found:
            found.update(json.loads(title[len("probe:"):]))
            app.quit()

    page.titleChanged.connect(titled)
    QTimer.singleShot(60000, app.quit)
    page.load(QUrl.fromLocalFile(str(folder / "page.html")))
    app.exec()
    print("page engine:", json.dumps(found))
    plays = (found.get("result") == "loaded" and found.get("size") == [64, 64] and found.get("canPlay") != ""
             and found.get("mse") and found.get("mseAac"))
    print("H.264/AAC plays" if plays else "H.264/AAC does NOT play")
    if "--expect-h264" in argv and not plays:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
