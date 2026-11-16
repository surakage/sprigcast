"""Picture conversion (FFmpeg's swscale) without a thread pool per picture.

`frame.reformat()` makes a fresh converter for every call, and since FFmpeg 7 each converter starts its
own pool of threads, one per CPU thread (16 on the user's Ryzen). Every decoded GIF frame, every camera
frame and every webm frame that needed converting started and dropped such a pool. On Windows they
piled up: the 2026-11-16 retest went from 114 to 306 threads in 27 minutes, 215 of them FFmpeg's.

Here each thread keeps one converter (it reuses its work while sizes and formats stay the same) and
converts on that thread alone: a 1080p picture takes about a millisecond.
"""

from __future__ import annotations

import threading

_local = threading.local()


def reformat(frame, threads: int = 1, **options):
    """frame.reformat(**options) through this thread's own converter, with `threads` threads (1)."""
    converter = getattr(_local, "converter", None)
    if converter is None:
        from av.video.reformatter import VideoReformatter

        converter = _local.converter = VideoReformatter()
    return converter.reformat(frame, threads=threads, **options)


def owned(array):
    """`array` as memory of its own. PyAV's to_ndarray() hands back a view into FFmpeg's frame, which keeps the
    frame (and its decoder's buffer pool) alive until whichever thread drops the array last, often the render
    thread long after the decoder thread closed its file. A copy lets FFmpeg's memory go on the thread that
    made it."""
    import numpy as np

    return array if array.flags.owndata else np.array(array, copy=True)


def picture_array(frame, **options):
    """reformat(frame, **options).to_ndarray(), as memory of its own (see owned)."""
    return owned(reformat(frame, **options).to_ndarray())
