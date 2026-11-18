"""Browser pages drawn on the engine's card (native/core/src/page_texture.cpp): the helper's texture, shared by
name, copied by the engine into the page's source on its own device. No picture passes through memory."""

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
            if np.abs(img[24, 32, :3].astype(int) - want).max() <= 3:
                return img
        time.sleep(0.005)
    return None


def _draw(target, rgba, wait_s=5.0) -> None:
    deadline = time.monotonic() + wait_s
    while not target.begin(20):  # the engine takes the last picture first
        assert time.monotonic() < deadline, "the engine never took the last picture"
    target.fill(rgba)


def test_a_page_drawn_on_the_card_shows_without_passing_through_memory():
    from sprigcast.render import native_bridge

    col = _collection()
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 64 * 48)
    bridge = native_bridge.NativeCanvases(col, warp=True)
    target = None
    try:
        luid = bridge.engine.luid()
        assert luid is not None
        target = sprigcore.PageTarget(luid, 64, 48, f"{slot.shm.name}-tex1")
        _draw(target, (1.0, 0.0, 0.0, 1.0))
        target.end(publish=True)
        slot.mark_on_card(64, 48, 1)
        assert slot.read() is None and slot.on_card()  # nothing in memory
        assert bridge.attach_media("page", slot.shm.name)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (64, 48), 0)
        assert _shown(bridge, (255, 0, 0)) is not None and not bridge.failed

        _draw(target, (0.0, 0.0, 1.0, 1.0))  # waits for the engine to have taken the red one
        assert tuple(target.read()[10, 10]) == (0, 0, 255, 255)  # read back too (SprigCast drawing it itself)
        target.end(publish=True)
        slot.mark_on_card(64, 48, 1)
        assert _shown(bridge, (0, 0, 255)) is not None

        _draw(target, (0.0, 0.25, 0.0, 0.5))  # premultiplied half green: read back as straight alpha
        assert np.abs(target.read()[0, 0].astype(int) - (0, 128, 0, 128)).max() <= 1
        target.end(publish=False)  # only read back: still the helper's, the engine shows blue on
        assert target.begin(0)
        target.end(publish=False)
        assert _shown(bridge, (0, 0, 255)) is not None

        # A resized page: a new texture on the same device (Qt Quick keeps drawing with it), a new number.
        bigger = sprigcore.PageTarget(luid, 64, 48, f"{slot.shm.name}-tex2", device=target.device)
        assert bigger.device == target.device
        target.close()
        target = bigger
        _draw(target, (0.0, 1.0, 0.0, 1.0))
        target.end(publish=True)
        slot.mark_on_card(64, 48, 2)
        assert _shown(bridge, (0, 255, 0)) is not None and not bridge.failed
    finally:
        bridge.close()
        if target is not None:
            target.close()
        slot.close()


def test_a_texture_that_isnt_there_harms_nothing():
    from sprigcast.render import native_bridge

    col = _collection()
    slot = FrameSlot.create(f"sprigcast-test-{uuid.uuid4().hex[:8]}", 64 * 48)
    bridge = native_bridge.NativeCanvases(col, warp=True)
    try:
        slot.mark_on_card(64, 48, 7)  # no such texture
        assert bridge.attach_media("page", slot.shm.name)
        bridge.sync(col, {}, lambda collection, layout: (layout, None), False)
        bridge.watch("h", (64, 48), 0)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            bridge.take("h")
            time.sleep(0.01)
        assert not bridge.failed
        red = np.zeros((48, 64, 4), np.uint8)
        red[...] = (255, 0, 0, 255)
        slot.write(red)  # back in memory: shown from there
        assert _shown(bridge, (255, 0, 0)) is not None
    finally:
        bridge.close()
        slot.close()
