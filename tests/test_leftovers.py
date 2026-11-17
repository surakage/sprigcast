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


def test_auto_clip_leaves_nothing_behind(tmp_path):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    settings.setValue(leftovers.DONE, 1)  # a PC that already tidied up after the first removals
    (tmp_path / "auto_clip.json").write_text("{}", encoding="utf-8")
    assert "auto_clip.json" in leftovers.clean_up(tmp_path, settings)
    assert not (tmp_path / "auto_clip.json").exists()

    rec = tmp_path / "Recordings"
    day = rec / "Suggested clips" / "2026-11-16"
    day.mkdir(parents=True)
    listed = day / "SprigCast Replay 2026-11-16 19-30-05 horizontal.mkv"
    listed.write_bytes(b"clip")
    mine = rec / "Suggested clips" / "my notes.txt"  # not Auto clip's: stays
    mine.write_text("x", encoding="utf-8")
    outside = rec / "Clips" / "keep.mkv"  # a listed path outside the folder is never touched
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"clip")
    (rec / "Suggested clips" / "suggested.json").write_text(json.dumps({"clips": [
        {"id": "a", "files": [str(listed), str(outside)]}]}), encoding="utf-8")
    gone = leftovers.clean_up_recordings(rec, settings)
    assert listed in gone and not listed.exists() and not day.exists()
    assert not (rec / "Suggested clips" / "suggested.json").exists()
    assert mine.exists() and outside.exists()
    assert leftovers.clean_up_recordings(rec, settings) == []  # once per PC


def test_an_added_auto_clip_copy_is_deleted_at_start(tmp_path):
    from sprigcast import plugin_store

    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "auto_clip.py").write_text('PLUGIN = {"id": "auto_clip", "version": 1}\n', encoding="utf-8")
    plugin_store.refresh_installed(plugins)
    assert not (plugins / "auto_clip.py").exists()
    assert not any(p.id == "auto_clip" for p in plugin_store.catalog())
