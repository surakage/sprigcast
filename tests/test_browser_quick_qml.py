"""The card page's QML takes its settings on the root item: Python can't hold the WebEngineView itself (PySide6 has
no converter for QQuickWebEngineView*, so on the PC every page fell back to memory, 2026-11-18)."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("PySide6.QtWebEngineQuick") is None,
                                reason="needs Qt WebEngine for Qt Quick")

SCRIPT = """
import sys
from PySide6.QtWebEngineQuick import QtWebEngineQuick, QQuickWebEngineProfile
QtWebEngineQuick.initialize()
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
app = QGuiApplication(sys.argv)
from sprigcast.app import browser_quick
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(browser_quick.QML, QUrl())
root = component.create()
assert root is not None, [e.toString() for e in component.errors()]
profile = QQuickWebEngineProfile("sprigcast-test")
for name, value in (("profile", profile), ("url", QUrl("about:blank")), ("audioMuted", True),
                    ("pageVisible", False)):
    assert root.setProperty(name, value), name
assert root.property("url") == QUrl("about:blank") and root.property("audioMuted") is True
assert root.property("pageVisible") is False
print("ok")
"""


def test_the_page_settings_go_through_the_root_item():
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    out = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120, check=False)
    assert out.stdout.strip().endswith("ok"), out.stderr[-2000:]


def test_python_never_asks_for_the_view_itself():
    source = (Path(__file__).resolve().parents[1] / "src/sprigcast/app/browser_quick.py").read_text("utf-8")
    assert 'property("view")' not in source and "self.view" not in source


NOTE_SCRIPT = """
import sys
from PySide6.QtWebEngineQuick import QtWebEngineQuick, QQuickWebEngineProfile
QtWebEngineQuick.initialize()
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
app = QGuiApplication(sys.argv)
from sprigcast.app import browser_quick
engine = QQmlEngine()
component = QQmlComponent(engine)
component.setData(browser_quick.QML, QUrl())
root = component.create()
window = QQuickWindow()
root.setParentItem(window.contentItem())
root.setWidth(64)
root.setHeight(64)
profile = QQuickWebEngineProfile("sprigcast-test")
root.setProperty("profile", profile)
got = []
root.mediaNote.connect(lambda message: (got.append(message), app.quit()))
root.setProperty("url", QUrl("data:text/html,<script>console.log('other');console.info('sprigcast:media h264 x')</script>"))
QTimer.singleShot(20000, app.quit)
app.exec()
print(got)
"""


def test_the_card_page_passes_on_a_video_that_cant_play():
    """A card page's console line from MEDIA_JS reaches Python (other console lines don't)."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox",
           "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    out = subprocess.run([sys.executable, "-c", NOTE_SCRIPT], env=env, capture_output=True, text=True, timeout=120,
                         check=False)
    assert out.stdout.strip().endswith("['sprigcast:media h264 x']"), out.stderr[-2000:]
