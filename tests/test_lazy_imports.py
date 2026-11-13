"""Heavy packages stay unloaded until something needs them."""

import subprocess
import sys

from sprigcast.output import keys


def test_probably_has_key_follows_what_was_learned(monkeypatch):
    assert keys.probably_has_key(None) is False
    assert keys.probably_has_key("sprigcast/x") is True  # a key_ref only exists once a key was saved
    monkeypatch.setitem(keys._present, "sprigcast/x", False)
    assert keys.probably_has_key("sprigcast/x") is False


def test_window_modules_do_not_load_keyring_or_pillow():
    code = (
        "import sys, sprigcast.app.main_window, sprigcast.sources.image, sprigcast.sources.text;"
        "print('keyring' in sys.modules, 'PIL' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**__import__("os").environ, "QT_QPA_PLATFORM": "offscreen"}, check=True)
    assert out.stdout.split()[-2:] == ["False", "False"]
