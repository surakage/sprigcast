"""The Random clip player store plugin (user, 2026-11-27): your own Twitch clips, fetched with SprigCast's own
sign-in (Get Clips, Get Clips Download with channel:manage:clips) and played by SprigCast itself, no browser."""

import io
from types import SimpleNamespace

import pytest

from sprigcast import plugin_hooks, plugin_store
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture
def cp(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    plugin = next(p for p in plugin_store.catalog() if p.id == "clip_player")
    assert plugin_store.install(tmp_path / "plugins", plugin) is None
    import sys

    module = sys.modules["sprigcast_plugin_clip_player"]
    module.POOL = module.Pool()
    monkeypatch.setattr(module, "cache_dir", lambda: tmp_path / "clips")
    yield module
    module.NOW = {"title": "", "by": "", "since": -1e9}
    plugin_hooks.output_changed("stream", None)
    plugin_store.remove(tmp_path / "plugins", "clip_player")
    assert "clip_player" not in known_kinds() and "clip_title" not in known_kinds()


CLIPS = {"data": [{"id": "a", "title": "That jump", "creator_name": "Mikedog151", "duration": 28.0,
                   "created_at": "2026-11-20T01:00:00Z"},
                  {"id": "b", "title": "Long one", "creator_name": "x", "duration": 90.0, "created_at": "2026-11-21"},
                  {"id": "c", "title": "Short", "creator_name": "y", "duration": 3.0, "created_at": "2026-11-21"},
                  {"id": "d", "title": "Oops", "creator_name": "", "duration": 12.0, "created_at": "2026-11-22"}]}


def test_lists_fetches_and_shuffles_without_repeats(cp, monkeypatch):
    from sprigcast.output import twitch_auth

    asked = []

    def helix(path_for, scope=None):
        path = path_for("42")
        asked.append((path, scope))
        if path.startswith("/clips?"):
            return CLIPS
        clip = path.rsplit("=", 1)[1]
        return {"data": [{"clip_id": clip, "landscape_download_url": f"https://production.assets.clips.twitchcdn.net/{clip}.mp4"}]}

    monkeypatch.setattr(twitch_auth, "helix_get", helix)
    monkeypatch.setattr(cp.urllib.request, "urlopen", lambda req, timeout=0: _Body(b"mp4!"))
    cp.POOL.busy = True
    cp.POOL._work()
    assert asked[0] == ("/clips?broadcaster_id=42&first=20", None)
    assert asked[1][0].startswith("/clips/downloads?editor_id=42&broadcaster_id=42&clip_id=")
    assert asked[1][1] == "channel:manage:clips"
    assert [c["id"] for c in cp.POOL.clips] == ["d", "a"]  # 5-60 s, newest first
    ready = cp.POOL.take()
    assert ready["path"].endswith(".mp4") and open(ready["path"], "rb").read() == b"mp4!"
    seen = {ready["title"]}
    seen.add(cp.POOL.pick()["title"])
    assert seen == {"That jump", "Oops"}  # both before any repeats


class _Body(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_only_twitch_hosts_and_a_tidy_folder(cp, tmp_path):
    import os
    import time

    assert cp.allowed("https://production.assets.clips.twitchcdn.net/x.mp4")
    assert not cp.allowed("http://production.assets.clips.twitchcdn.net/x.mp4")
    assert not cp.allowed("https://evil.example.com/twitch.tv.mp4")
    folder = tmp_path / "t"
    folder.mkdir()
    for i in range(35):
        f = folder / f"{i}.mp4"
        f.write_bytes(b"x")
        os.utime(f, (time.time() - i, time.time() - i))
    old = folder / "old.mp4"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 2 * 86400,) * 2)
    cp.tidy(folder)
    assert len(list(folder.glob("*.mp4"))) == 30 and not old.exists()


def test_a_ready_clip_plays_when_live_and_the_title_shows(cp, monkeypatch):
    from sprigcast.sources.media import MediaSource

    files = []
    monkeypatch.setattr(MediaSource, "update", lambda self, s: files.append(s.get("path")) or
                        setattr(self, "settings", dict(s)))
    monkeypatch.setattr(MediaSource, "video_frame", lambda self: None)
    monkeypatch.setattr(cp.POOL, "want", lambda s: None)
    player = create_source("clip_player", "p", {})
    player.video_frame()
    assert "live" in player.status  # off air: waits
    plugin_hooks.output_changed("stream", 1.0)
    cp.POOL.ready = {"path": "/clips/a.mp4", "title": "That jump", "by": "Mikedog151"}
    player.video_frame()
    assert files[-1] == "/clips/a.mp4" and cp.NOW["title"] == "That jump"
    player.update({"gap": 3})
    assert files[-1] == "/clips/a.mp4"  # changing a setting keeps the clip
    title = create_source("clip_title", "t", {})
    card = title.video_frame()
    assert card is not None and card[..., 3].any()
    assert cp.title_words(cp.NOW) == ("That jump", "Clipped by Mikedog151")


def test_signed_in_before_the_permission(cp, monkeypatch):
    from sprigcast.output import twitch_auth

    monkeypatch.setattr(twitch_auth, "_load", lambda: {"scope": [], "user_id": "1", "access_token": "t",
                                                       "expires_at": 9e12})
    monkeypatch.setattr(twitch_auth, "_get", lambda path, token, cid: CLIPS)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    cp.POOL.busy = True
    cp.POOL._work()
    assert "Connect Twitch again" in cp.POOL.problem and not cp.POOL.busy
