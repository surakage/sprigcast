"""Qt Quick starts on the engine's card even when PySide can't take Direct3D pointers: fromDeviceAndContext
refused them on the PC (2026-11-18), so Qt Quick makes its own device on the card (by its LUID) and the page's
texture is made on that device instead."""

from __future__ import annotations

import pytest

QtQuick = pytest.importorskip("PySide6.QtQuick")

from sprigcast.app.browser_quick import QuickPage  # noqa: E402


class _Target:
    def __init__(self, device):
        self.device, self.context, self.texture, self.closed = device, 7, 9, False

    def close(self):
        self.closed = True


class _Host:
    card = (0x1234, 5)

    def __init__(self):
        self.made = []

    def target_for(self, source_id, w, h, device=None):
        self.made.append((source_id, w, h, device))
        return _Target(device)


class _Renderer:
    def getResource(self, window, what):  # noqa: N802 - Qt's name
        return 0xBEEF


class _Window:
    def setGraphicsDevice(self, device):  # noqa: N802
        self.device = device

    def rendererInterface(self):  # noqa: N802
        return _Renderer()


class _Control:
    def initialize(self):
        return True


def _page():
    page = object.__new__(QuickPage)
    page.id, page.host, page.window, page.control = "browser-1", _Host(), _Window(), _Control()
    page._initialized = False
    return page


def test_qt_quick_makes_its_own_device_when_pyside_refuses_the_pointers(monkeypatch):
    class Device:
        @staticmethod
        def fromDeviceAndContext(device, context):  # noqa: N802
            raise TypeError("'fromDeviceAndContext' called with wrong argument types")

        @staticmethod
        def fromAdapter(low, high):  # noqa: N802
            return ("adapter", low, high)

    monkeypatch.setattr(QtQuick, "QQuickGraphicsDevice", Device)
    page = _page()
    first = _Target(11)
    page.target = first
    target = page._start_quick(first, 640, 360)
    assert page.window.device == ("adapter", 0x1234, 5)
    assert first.closed and page._initialized
    assert page.host.made == [("browser-1", 640, 360, 0xBEEF)]  # the texture on Qt Quick's own device
    assert page.target is target and target.device == 0xBEEF


def test_the_engine_texture_device_is_used_when_pyside_takes_it(monkeypatch):
    class Device:
        @staticmethod
        def fromDeviceAndContext(device, context):  # noqa: N802
            return ("device", device, context)

    monkeypatch.setattr(QtQuick, "QQuickGraphicsDevice", Device)
    page = _page()
    first = _Target(11)
    page.target = first
    assert page._start_quick(first, 640, 360) is first
    assert page.window.device == ("device", 11, 7) and not first.closed and page.host.made == []


def test_pointers_go_as_voidptr_when_pyside_refuses_big_ints(monkeypatch):
    shiboken6 = pytest.importorskip("shiboken6")

    class Device:
        @staticmethod
        def fromDeviceAndContext(device, context):  # noqa: N802
            if isinstance(device, int):
                raise ValueError("called with wrong argument values: Found signature (device: int, context: int)")
            return ("voidptr", int(device), int(context))

    monkeypatch.setattr(QtQuick, "QQuickGraphicsDevice", Device)
    page = _page()
    first = _Target(2687846546624)
    page.target = first
    assert page._start_quick(first, 640, 360) is first
    assert page.window.device == ("voidptr", 2687846546624, 7)
    assert isinstance(shiboken6.VoidPtr(1), shiboken6.VoidPtr)


def test_the_page_texture_goes_as_a_voidptr_when_pyside_refuses_the_int():
    from shiboken6 import VoidPtr

    class RenderTarget:
        @staticmethod
        def fromD3D11Texture(texture, size):  # noqa: N802
            if isinstance(texture, int):  # the PC, 2026-11-18: an int over 2^31 refused
                raise ValueError("'fromD3D11Texture' called with wrong argument values")
            return ("target", texture, size)

    page = _page()
    made = page._render_target(RenderTarget, 2762627897376, (600, 400))
    assert made[0] == "target" and isinstance(made[1], VoidPtr) and int(made[1]) == 2762627897376


def test_a_texture_no_form_fits_says_why():
    class RenderTarget:
        @staticmethod
        def fromD3D11Texture(texture, size):  # noqa: N802
            raise ValueError("no")

    with pytest.raises(RuntimeError, match="can't take the page texture"):
        _page()._render_target(RenderTarget, 5, (1, 1))


def test_a_page_hidden_a_while_lets_go_of_its_device_and_starts_again_when_shown(monkeypatch):
    """PC 2026-11-21: ~60 pages, each with its own device on the card (~50 driver threads each). A page kept running
    while hidden lets go of its device after a while; shown again, it starts on one again."""
    from sprigcast.app import browser_quick

    class Control(_Control):
        invalidated = 0

        def invalidate(self):
            Control.invalidated += 1

    page = _page()
    page.control = Control()
    page._initialized, page.paused, page.on_card = True, True, True
    page.target = _Target(11)
    old = page.target
    page.settings = {"width": 320, "height": 180}
    page._paused_at = 0.0
    monkeypatch.setattr(browser_quick.time, "monotonic", lambda: 1000.0)
    page._release_card_if_still_hidden()
    assert Control.invalidated == 1 and old.closed and page.target is None and not page._initialized
    resized = []
    page._call = lambda *a: None
    page.root = type("Root", (), {"setProperty": lambda self, k, v: None})()
    page.timer = type("Timer", (), {"start": lambda self, ms: None})()
    page._resize = lambda w, h: resized.append((w, h))
    page.set_paused(False)
    assert resized == [(320, 180)]


def test_a_page_shown_again_before_its_time_keeps_its_device(monkeypatch):
    from sprigcast.app import browser_quick

    class Control(_Control):
        def invalidate(self):
            raise AssertionError("not let go")

    page = _page()
    page.control = Control()
    page._initialized, page.paused = True, False  # shown again meanwhile
    page._release_card_if_still_hidden()
    page.paused, page._paused_at = True, 999.0  # hidden again since: its own timer comes later
    monkeypatch.setattr(browser_quick.time, "monotonic", lambda: 1000.0)
    page._release_card_if_still_hidden()
    assert page._initialized
