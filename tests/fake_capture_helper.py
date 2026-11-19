"""Stands in for SprigCastCapture.exe in tests/test_capture_helper.py: the same lines in and out, no capture.
A window numbered 404 is refused, 500 ends after its first picture, and "die" ends the program at once."""

import sys


def say(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


say("ready")
for line in sys.stdin:
    parts = line.split()
    if not parts:
        continue
    if parts[0] == "quit":
        break
    if parts[0] == "die":
        sys.exit(3)
    if parts[0] == "start":
        number, window = parts[1], int(parts[4])
        say(f"log 20 capture: started a window ({' '.join(parts[2:])})")
        if window == 404:
            say(f"state {number} 4 0 0 0")
            continue
        say(f"state {number} 1 0 0 0")
        say(f"state {number} 2 640 360 1")
        if window == 500:
            say(f"state {number} 3 640 360 1")
    elif parts[0] == "stop":
        say(f"log 20 capture: closed {parts[1]}")
