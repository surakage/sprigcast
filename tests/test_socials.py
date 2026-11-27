"""The Socials store plugin (user, 2026-11-27): handles one at a time with official platform icons (vector)."""

import pytest

from sprigcast import plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def so(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "socials")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    yield sys.modules["sprigcast_plugin_socials"]
    plugin_store.remove(tmp_path / "plugins", "socials")
    assert "socials" not in known_kinds()


def test_lines_become_handles_with_icons(so):
    got = so.entries("Twitch: @Sura\n\nyoutube:@Sura\nDiscord: sloths.gg: Join us\nWebsite: https://slothbloom.com\nnope")
    assert [(e["handle"], e["label"], e["icon"]) for e in got] == [
        ("@Sura", "Twitch", "twitch"), ("@Sura", "YouTube", "youtube"), ("sloths.gg", "Join us", "website"),
        ("https://slothbloom.com", "Website", "website")]
    for key, (_name, icon, _color) in so.PLATFORMS.items():
        assert (so._assets_dir() / f"{icon}.svg").exists(), key
    assert (so._assets_dir() / "website.svg").exists()


def test_one_at_a_time_drawn_only_when_it_changes(so, monkeypatch):
    now = [80.0]
    monkeypatch.setattr(so.time, "time", lambda: now[0])
    source = create_source("socials", "s", {"socials": "Twitch: @a\nKick: b", "every": 8})
    first = source.video_frame()
    assert first is not None and first[..., 3].any() and so.which(2, source.settings, now[0]) == 0
    assert source.video_frame() is None
    now[0] = 88.0
    second = source.video_frame()
    assert second is not None and second.shape == first.shape  # same box for every handle
    source.update({"socials": ""})
    assert not source.video_frame()[..., 3].any() and "add a line" in source.status
