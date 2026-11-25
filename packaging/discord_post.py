"""Post a release to the SprigCast Discord: a card with the version, what's new, the download and its size.

    python packaging/discord_post.py release v0.4.0 <installer.exe>      (public #releases)
    python packaging/discord_post.py test test-2026-11-22 <installer.exe> --label "Beta 0.3 test build …"
                                                                           (Tester Garden's #tester-news)
    add --print to show the message as JSON instead of sending it

A public release reads DISCORD_RELEASES_WEBHOOK, a test build DISCORD_TESTER_WEBHOOK. With no webhook set it
says so and does nothing, so a build never fails over Discord. A test build's "What's new" is the pull requests
merged since the previous test-* tag (needs the git history: checkout with fetch-depth 0).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_notes import CHANGELOG, _shown, section  # noqa: E402

REPO = "https://github.com/surakage/sprigcast"  # test builds only live here (pre-releases)
# A public release card sends people to the website, never to GitHub (user, 2026-11-25).
SITE = "https://sprigcast.slothbloom.com"
BANNER = "https://raw.githubusercontent.com/surakage/sprigcast/main/brand/sprigcast-banner.png"
EMERALD = 0x2A9C5E  # brand/README.md: the sprig S
MINT = 0x3ECF7F  # the app icon's S, for test builds
PER_HEADING = 5  # entries shown under Added, Changed, Fixed and Removed; the rest are counted
LIMIT = 3800  # Discord allows 4096 characters in a card's text; leave room for the closing line


def _size(path: Path) -> str:
    return f"{path.stat().st_size / 1_000_000:.0f} MB"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _first_sentence(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return match.group(1) if match else text


def _fit(lines: list[str], more: str) -> str:
    """Join lines up to the limit; if some don't fit, end with a line that points to the rest."""
    out: list[str] = []
    for line in lines:
        if sum(len(l) + 1 for l in out) + len(line) + len(more) + 2 > LIMIT:
            out.append(more)
            break
        out.append(line)
    return "\n".join(out)


def changelog_summary(notes: str, tag: str) -> str:
    """The section's opening words, then each heading (Added, Changed, …) with the first sentence of each entry."""
    intro, blocks, heading, entry = [], [], None, None
    for line in notes.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            blocks.append((heading, []))
        elif line.startswith("- "):
            entry = [line[2:]]
            (blocks[-1][1] if blocks else intro).append(entry)
        elif line.startswith("  ") and entry is not None:
            entry.append(line.strip())
        elif line.strip() and heading is None:
            intro.append([line.strip()])
            entry = None
    lines = []
    if intro:
        lines += [" ".join(" ".join(part) for part in intro), ""]
    for name, entries in blocks:
        if not entries:
            continue
        lines.append(f"**{name}**")
        lines += [f"• {_first_sentence(' '.join(e))}" for e in entries[:PER_HEADING]]
        if len(entries) > PER_HEADING:
            lines.append(f"• …and {len(entries) - PER_HEADING} more")
        lines.append("")
    return _fit(lines, f"…and more in the [full notes]({SITE}/changelog).")


def merged_since_last_test(tag: str) -> str:
    """Titles of the pull requests merged since the previous test-* tag (or the last 15 if there is none)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=False).stdout

    tags = [t for t in git("tag", "--list", "test-*", "--sort=-creatordate").split() if t != tag]
    span = [f"{tags[0]}..HEAD"] if tags else ["-15"]
    lines = []
    for row in git("log", "--first-parent", "--format=%H%x09%s", *span).splitlines():
        commit, _, subject = row.partition("\t")
        number = re.search(r"\(#(\d+)\)$|^Merge pull request #(\d+)", subject)
        if not number:
            continue
        pr = number.group(1) or number.group(2)
        if re.match(r"(test|tests|ci|chore)\b[:(]", subject, re.I):
            continue  # changes to the tests or the build alone mean nothing to a tester
        title = re.sub(r"\s*\(#\d+\)$", "", subject)
        if title.startswith("Merge pull request"):
            body = git("log", "-1", "--format=%b", commit)
            title = (body.strip().splitlines() or [f"Pull request #{pr}"])[0]
        lines.append(f"• {_first_sentence(title)} ([#{pr}]({REPO}/pull/{pr}))")
    if not lines:
        return "Fixes and small changes since the last test build."
    since = f"Since {tags[0]}:" if tags else "Latest changes:"
    return _fit([since, *lines], f"…and more in the [commit list]({REPO}/commits/{tag}).")


def message(kind: str, tag: str, installer: Path, label: str | None) -> dict:
    download = f"{REPO}/releases/download/{tag}/{installer.name}"
    page = f"{REPO}/releases/tag/{tag}"
    sha = _sha256(installer)
    if kind == "release":
        download, page = SITE, f"{SITE}/changelog"
        version = tag.removeprefix("v")
        shown = _shown(version)
        notes = section(CHANGELOG.read_text(encoding="utf-8"), version) or ""
        title, color = f"SprigCast {shown} is out", EMERALD
        text = changelog_summary(notes, tag)
        footer = "SprigCast offers this update by itself. Or download it below."
    else:
        shown = label or tag
        title, color = f"New tester build: {shown}", MINT
        text = merged_since_last_test(tag)
        footer = "Test builds aren't offered as updates. Please send a report for anything that feels off."
    fields = [
        {"name": "Download", "value": f"[{installer.name}]({download})", "inline": True},
        {"name": "Size", "value": _size(installer), "inline": True},
        {"name": "What's new", "value": f"[Full notes]({page})", "inline": True} if kind == "release" else
        {"name": "Release page", "value": f"[Notes and checksums]({page})", "inline": True},
        {"name": "SHA-256", "value": f"`{sha[:16]}…`", "inline": False},
    ]
    embed = {"title": title, "url": page, "description": text, "color": color, "fields": fields,
             "footer": {"text": footer}}
    if kind == "release":
        embed["image"] = {"url": BANNER}
    return {"username": "SprigCast", "embeds": [embed], "allowed_mentions": {"parse": []}}


def send(webhook: str, payload: dict) -> None:
    request = urllib.request.Request(webhook + ("&" if "?" in webhook else "?") + "wait=true",
                                     data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "SprigCast-release (github.com/surakage/sprigcast)"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status >= 300:
            raise RuntimeError(f"Discord answered {response.status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=["release", "test"])
    parser.add_argument("tag")
    parser.add_argument("installer", type=Path)
    parser.add_argument("--label", help="a test build's shown name (sprigcast.build_label)")
    parser.add_argument("--print", action="store_true", help="print the message instead of sending it")
    args = parser.parse_args(argv)
    payload = message(args.kind, args.tag, args.installer, args.label)
    if args.print:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    name = "DISCORD_RELEASES_WEBHOOK" if args.kind == "release" else "DISCORD_TESTER_WEBHOOK"
    webhook = os.environ.get(name, "").strip()
    if not webhook:
        print(f"{name} isn't set, so nothing was posted to Discord")
        return 0
    send(webhook, payload)
    print("Posted to Discord")
    return 0


if __name__ == "__main__":
    sys.exit(main())
