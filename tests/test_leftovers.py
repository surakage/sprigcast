"""What features taken out of SprigCast left on a PC is tidied up once (app/leftovers.py)."""

import json

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QSettings  # noqa: E402

from sprigcast.app import leftovers  # noqa: E402
from sprigcast.core.scene import SourceConfig  # noqa: E402


def test_removed_features_leave_nothing_behind(tmp_path):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    settings.setValue("captions/google_key", "secret")
    settings.setValue("wavelink/sync", True)
    settings.setValue("kick/pusher_key", "abc")
    settings.setValue("kick/client_id", "keep")  # Kick sign-in stays
    settings.setValue("chat/names", json.dumps({"twitch": "moss", "kick": "fern"}))
    (tmp_path / "models" / "whisper-onnx" / "tiny").mkdir(parents=True)
    (tmp_path / "models" / "whisper-onnx" / "tiny" / "tiny-encoder.onnx").write_bytes(b"x" * 10)

    removed = leftovers.clean_up(tmp_path, settings)
    assert "models folder" in removed and not (tmp_path / "models").exists()
    assert not settings.contains("captions/google_key") and not settings.contains("wavelink/sync")
    assert not settings.contains("kick/pusher_key") and settings.value("kick/client_id") == "keep"
    assert json.loads(settings.value("chat/names")) == {"twitch": "moss"}
    assert leftovers.clean_up(tmp_path, settings) == []  # once per PC


def test_filters_of_removed_features_leave_gardens_as_they_load():
    source = SourceConfig.model_validate({"id": "mic", "kind": "audio_input", "name": "Mic", "filters": [
        {"id": "a", "kind": "vst", "name": "VST"}, {"id": "b", "kind": "background", "name": "Remover"},
        {"id": "c", "kind": "noise_gate", "name": "Gate"}]})
    assert [f.kind for f in source.filters] == ["noise_gate"]
