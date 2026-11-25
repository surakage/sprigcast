"""packaging/discord_post.py: the Discord card for a release and for a tester build."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("discord_post", ROOT / "packaging" / "discord_post.py")
discord_post = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discord_post)

NOTES = """Opening words about this beta.

### Added

- First new thing. More words that stay out of the card.
- Second new thing, which goes
  over two lines.
### Fixed

""" + "\n".join(f"- Fix {n}." for n in range(8))


def test_summary_keeps_first_sentences_and_counts_the_rest():
    text = discord_post.changelog_summary(NOTES, "v0.4.0")
    assert text.startswith("Opening words about this beta.")
    assert "• First new thing." in text and "More words" not in text
    assert "• Second new thing, which goes over two lines." in text
    assert "• Fix 4." in text and "• Fix 5." not in text and "…and 3 more" in text


def test_a_long_summary_stops_at_discords_limit():
    notes = "### Added\n\n" + "\n".join(f"- {'word ' * 60}{n}." for n in range(5)) * 8
    discord_post.PER_HEADING = 100
    try:
        text = discord_post.changelog_summary(notes, "v0.4.0")
    finally:
        discord_post.PER_HEADING = 5
    assert len(text) <= 4096 and text.endswith("(https://github.com/surakage/sprigcast/releases/tag/v0.4.0).")


def test_release_and_test_cards(tmp_path, monkeypatch):
    setup = tmp_path / "SprigCast-Setup-0.4.0.exe"
    setup.write_bytes(b"x" * 2_000_000)
    release = discord_post.message("release", "v0.4.0", setup, None)["embeds"][0]
    assert release["title"] == "SprigCast Beta 0.4 is out"
    fields = {f["name"]: f["value"] for f in release["fields"]}
    assert fields["Size"] == "2 MB"
    assert "releases/download/v0.4.0/SprigCast-Setup-0.4.0.exe" in fields["Download"]
    monkeypatch.setattr(discord_post, "merged_since_last_test", lambda tag: "• Something (#1)")
    test = discord_post.message("test", "test-2026-11-22", setup, "Beta 0.3 test build 1 (abc1234)")["embeds"][0]
    assert test["title"] == "New tester build: Beta 0.3 test build 1 (abc1234)" and "image" not in test


def test_no_webhook_posts_nothing(tmp_path, monkeypatch, capsys):
    setup = tmp_path / "SprigCast-Setup-0.4.0.exe"
    setup.write_bytes(b"x")
    monkeypatch.delenv("DISCORD_RELEASES_WEBHOOK", raising=False)
    monkeypatch.setattr(discord_post, "send", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))
    assert discord_post.main(["release", "v0.4.0", str(setup)]) == 0
    assert "nothing was posted" in capsys.readouterr().out
    assert json.dumps(discord_post.message("release", "v0.4.0", setup, None))  # serializable


def test_cards_point_at_the_public_releases_repo_when_set(tmp_path, monkeypatch):
    setup = tmp_path / "SprigCast-Setup-0.4.0.exe"
    setup.write_bytes(b"x")
    monkeypatch.setenv("SPRIGCAST_RELEASES_REPO", "surakage/SprigCast-Releases")
    release = discord_post.message("release", "v0.4.0", setup, None)["embeds"][0]
    fields = {f["name"]: f["value"] for f in release["fields"]}
    assert "github.com/surakage/SprigCast-Releases/releases/download/v0.4.0/" in fields["Download"]
    assert release["url"] == "https://github.com/surakage/SprigCast-Releases/releases/tag/v0.4.0"
    assert release["image"]["url"].startswith("https://raw.githubusercontent.com/surakage/SprigCast-Releases/")
    assert "surakage/sprigcast/" not in json.dumps(release)  # nothing points into the source repo
