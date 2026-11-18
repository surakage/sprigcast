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
