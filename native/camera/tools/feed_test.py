"""CI check: while an app (ffmpeg) has SprigCast Camera open, feed it frames the way SprigCast does and
make sure they come out the other side. Usage: feed_test.py <seconds>"""

import sys
import time

from sprigcast.output import virtualcam

deadline = time.time() + float(sys.argv[1])
block = None
fed = 0
while time.time() < deadline:
    block = block or virtualcam.open_block()
    if block is None:
        time.sleep(0.05)
        continue
    w, h = block.wanted()
    if w and h:
        # Bright magenta-ish NV12: Y=180, U=200, V=200, so the camera's placeholder green can't pass for it.
        frame = bytes([180]) * (w * h) + bytes([200]) * (w * h // 2)
        fed += block.write(w, h, frame)
    time.sleep(1 / 30)
print(f"fed {fed} frames")
sys.exit(0 if fed else 1)
