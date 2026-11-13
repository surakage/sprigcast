"""Let libraries `import cv2` without loading OpenCV until they actually use it.

windows-capture imports OpenCV at the top, but only uses it to save screenshots to disk, which
SprigCast never does. Loading OpenCV costs about 90 MB, so we hand such libraries a stand-in module
that loads the real one the first time anything on it is used.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
import types

_lock = threading.Lock()


class _LazyCv2(types.ModuleType):
    def __getattr__(self, name: str):
        with _lock:
            if sys.modules.get("cv2") is self:
                del sys.modules["cv2"]
            real = importlib.import_module("cv2")  # cached after the first time
        return getattr(real, name)


def install() -> None:
    """Put the stand-in in place, unless OpenCV is already loaded or isn't installed. Safe to call often."""
    with _lock:
        if "cv2" in sys.modules:
            return
        try:
            if importlib.util.find_spec("cv2") is None:
                return
        except (ImportError, ValueError):
            return
        sys.modules["cv2"] = _LazyCv2("cv2")
