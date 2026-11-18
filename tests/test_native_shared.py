"""A browser page's pictures go from the helper's shared memory straight to the native engine (no Python copy)."""

from __future__ import annotations

import time
import uuid

import numpy as np
import pytest

from sprigcast import sprigcore
from sprigcast.app.browser_helper import FrameSlot

pytestmark = pytest.mark.skipif(not sprigcore.available(), reason="sprigcore.dll isn't built here")


def _collection():
    from sprigcast.core.scene import Canvas, Scene, SceneCollection, SceneItem, SourceConfig

    return SceneCollection(
        canvases=[Canvas(id="h", width=64, height=48)],
        sources=[SourceConfig(id="page", kind="browser", name="Page", settings={"width": 64, "height": 48})],
        scenes=[Scene(id="a", name="A", canvas="h", items=[SceneItem(source="page", x=0, y=0, align="top_left")])],
        active_scenes={"h": "a"},
    )


def _shown(bridge, want, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        got = bridge.take("h")
        if got is not None:
            img = np.frombuffer(got[0], np.uint8).reshape(48, 64, 4)
            if np.abs(img[24, 32, :3].astype(int) - want).max() <= 2:
                return img
        time.sleep(0.005)
    return None


def test_the_engine_shows_a_pages_pictures_from_shared_memory():
    from sprigcast.render import native_bridge

    col = _collection()
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 64 * 48)
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        red = np.zeros((48, 64, 4), np.uint8)
        red[...] = (255, 0, 0, 255)
        slot.write(red)
        assert bridge.attach_media("page", slot.shm.name)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (64, 48), 0)
        img = _shown(bridge, (255, 0, 0))
        assert img is not None and not bridge.failed  # Python sent no picture at all
        blue = red.copy()
        blue[...] = (0, 0, 255, 255)
        slot.write(blue)  # the next picture, in the other slot
        assert _shown(bridge, (0, 0, 255)) is not None
        for _ in range(3):  # several pictures between two frames: the newest shows
            slot.write(red)
        green = red.copy()
        green[...] = (0, 255, 0, 255)
        slot.write(green)
        assert _shown(bridge, (0, 255, 0)) is not None
        assert not bridge.attach_media("page", None)  # let go: it keeps its picture, and Python's upload is back
        assert _shown(bridge, (0, 255, 0)) is not None and not bridge.failed
    finally:
        bridge.close()
        slot.close()


def test_a_block_that_isnt_there_is_said_once_and_harms_nothing():
    from sprigcast.render import native_bridge

    col = _collection()
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        assert bridge.attach_media("page", "sprigcast-no-such-block")
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (64, 48), 0)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            bridge.take("h")
            time.sleep(0.01)
        assert not bridge.failed
    finally:
        bridge.close()
