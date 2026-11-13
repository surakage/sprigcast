import numpy as np

from sprigcast.core.mailbox import FrameRing


def test_newest_frame_wins_and_buffers_are_reused():
    ring = FrameRing()
    assert ring.take() is None
    seen = set()
    for i in range(20):
        ring.put(np.full((4, 6, 4), i, np.uint8))
        if i % 3 == 0:
            frame = ring.take()
            assert frame[0, 0, 0] == i
            seen.add(id(frame))
    assert len(ring._buffers) <= 2 and len(seen) <= 2
    assert ring.take()[0, 0, 0] == 19 and ring.take() is None


def test_the_frame_being_read_is_never_overwritten():
    ring = FrameRing()
    ring.put(np.full((2, 2), 1, np.uint8))
    held = ring.take()
    for i in range(10):  # the writer keeps going while the reader still uses `held`
        ring.put(np.full((2, 2), 50 + i, np.uint8))
    assert (held == 1).all()
    assert ring.take()[0, 0] == 59


def test_padded_rows_and_size_changes():
    ring = FrameRing()
    padded = np.arange(4 * 8, dtype=np.uint8).reshape(4, 8)[:, :6]  # rows with padding, like a capture buffer
    ring.put(padded)
    out = ring.take()
    assert out.flags.c_contiguous and (out == padded).all()
    ring.put(np.zeros((3, 3), np.uint8))
    assert ring.take().shape == (3, 3)
    assert all(b.shape == (3, 3) or b is out for b in ring._buffers)
