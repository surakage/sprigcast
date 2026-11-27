"""Updates: find a newer SprigCast on GitHub Releases, download its installer, check it and run it.

No Qt here (app/updates.py is the window side), so every step can be tested on its own.

How an update goes:
1. `fetch_newest` asks GitHub for the newest release on your update channel. "stable" (a release copy) reads
   only published versions, as before. A test build (tag test-0.5-3, "Beta 0.5 test build 3") reads the list of
   releases and also sees newer test builds, so testers update in place instead of downloading every build.
   A published version beats every test build of that version: 0.5.0 > 0.5 test build 9 > 0.5 test build 1.
2. `download` saves its installer (SprigCast-Setup-<version>.exe) and hashes it on the way.
3. `verify` compares that hash with the release's SHA256.txt (written by the installer workflow) and,
   on Windows, checks the file's signature isn't broken. Unsigned builds pass until signing starts.
4. `launch_installer` starts a hidden PowerShell that waits for SprigCast to close, then runs Setup
   silently. Setup keeps your blooms, settings and extras (desktop shortcut, SprigCast Camera) as
   they were, and only asks Windows for permission if the camera itself changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sprigcast import __version__, build_stamp, short_version, test_build

log = logging.getLogger(__name__)

REPO = "surakage/sprigcast"
# What people see is always the website, never GitHub (user, 2026-11-25); GitHub is only read behind the scenes.
SITE_URL = "https://sprigcast.slothbloom.com"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=50"
# Update channels. "test" and "both" only make sense on a test build or once Version 1 offers the choice.
STABLE, TESTS, BOTH = "stable", "test", "both"
CHANNELS = (STABLE, TESTS, BOTH)
_TEST_TAG = re.compile(r"^test-(\d+)\.(\d+)-(\d+)$")  # test-0.5-3: Beta 0.5 test build 3 (Pollen's tags)
HASHES_NAME = "SHA256.txt"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# Signature states (PowerShell's Get-AuthenticodeSignature) an installer may have. "NotSigned" stays
# allowed until builds are signed; a signed file whose signature is broken or untrusted never runs.
SIGNATURE_OK = frozenset({"Valid", "NotSigned"})


def parse_version(text: str) -> tuple[int, int, int] | None:
    """'v0.2.1' or '0.2.1' -> (0, 2, 1). Anything else (such as '0.3.0-beta') -> None."""
    match = _VERSION.match(text.strip())
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


def installer_name(version: str) -> str:
    return f"SprigCast-Setup-{version}.exe"


Key = tuple[int, int, int, int, int]  # (major, minor, patch, 1 for a published version else 0, test build)


def build_key(version: str, test: int = 0, published: bool = True) -> Key | None:
    parsed = parse_version(version)
    return (*parsed, 1 if published else 0, 0 if published else test) if parsed else None  # type: ignore[return-value]


def current_key(version: str = __version__, stamp: dict | None = None) -> Key | None:
    """Where this copy stands. Any test build (even an older, unnumbered one) sits below the published version
    with the same number, so that release is offered to it."""
    stamp = build_stamp() if stamp is None else stamp
    return build_key(version, test_build(stamp), published=not stamp)


def is_test_copy(stamp: dict | None = None) -> bool:
    return bool(build_stamp() if stamp is None else stamp)


def default_channel(stamp: dict | None = None) -> str:
    """Test builds hear about test builds and published versions; a release copy only about published ones."""
    return BOTH if is_test_copy(stamp) else STABLE


@dataclass(frozen=True)
class Release:
    version: str
    notes: str  # the release's text (Markdown), from the CHANGELOG
    page_url: str
    installer_url: str
    installer_size: int
    hashes_url: str
    test: int = 0  # the test build number; 0 for a published version
    tag: str = ""
    file: str = ""  # the installer's file name, when it isn't SprigCast-Setup-<version>.exe

    @property
    def installer_name(self) -> str:
        return self.file or installer_name(self.version)

    @property
    def label(self) -> str:
        """"Beta 0.5 test build 3" or "Beta 0.5": what the pop-up calls it."""
        return short_version(self.version, self.test)

    @property
    def key(self) -> Key:
        return build_key(self.version, self.test, published=not self.test)  # type: ignore[return-value]

    @property
    def skip_id(self) -> str:
        """What "Skip this version" remembers."""
        return self.tag if self.test else self.version  # published: "0.5.0", as earlier copies saved it


def _setup_asset(assets: dict, version: str) -> dict | None:
    if version and installer_name(version) in assets:
        return assets[installer_name(version)]
    found = [a for name, a in assets.items() if str(name).startswith("SprigCast-Setup-") and str(name).endswith(".exe")]
    return found[0] if len(found) == 1 else None


def release_from_json(data: dict, tests: bool = False) -> Release | None:
    """A GitHub release -> Release, or None if it isn't a finished release with an installer and hashes.
    Pre-releases count only with `tests`, and only numbered test builds (test-0.5-3)."""
    if data.get("draft"):
        return None
    tag = str(data.get("tag_name", ""))
    test = 0
    if data.get("prerelease"):
        match = _TEST_TAG.match(tag) if tests else None
        if not match:
            return None
        version, test = f"{int(match[1])}.{int(match[2])}.0", int(match[3])
        if test < 1:
            return None
    elif parse_version(tag) is None:
        return None
    else:
        version = tag.removeprefix("v")
    assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
    setup, hashes = _setup_asset(assets, version), assets.get(HASHES_NAME)
    if not setup or not hashes:
        return None
    name = str(setup.get("name", ""))
    return Release(
        version=version,
        notes=str(data.get("body") or "").strip(),
        page_url=SITE_URL,
        installer_url=str(setup.get("browser_download_url", "")),
        installer_size=int(setup.get("size") or 0),
        hashes_url=str(hashes.get("browser_download_url", "")),
        test=test,
        tag=tag,
        file="" if name == installer_name(version) else name,
    )


def newer_than_this(release: Release | None, current: Key | None = None) -> bool:
    current = current_key() if current is None else current
    return release is not None and current is not None and release.key > current


def pick(releases: list[Release], channel: str) -> Release | None:
    """The newest release the channel allows: "stable" published versions, "test" test builds, "both" either."""
    allowed = [r for r in releases if (channel != STABLE or not r.test) and (channel != TESTS or r.test)]
    return max(allowed, key=lambda r: r.key, default=None)


def _request(url: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"SprigCast/{__version__}"})


Opener = Callable[..., object]


def fetch_latest(opener: Opener = urllib.request.urlopen, timeout: float = 15) -> Release | None:
    """The newest published release, or None when there isn't one yet. Network errors raise OSError."""
    try:
        with opener(_request(LATEST_URL, "application/vnd.github+json"), timeout=timeout) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # no releases published yet
            return None
        raise
    return release_from_json(data) if isinstance(data, dict) else None


def fetch_newest(channel: str = STABLE, opener: Opener = urllib.request.urlopen,
                 timeout: float = 15) -> Release | None:
    """The newest release on `channel`. Stable asks for the latest published release, as always; the test
    channels read the list of releases (the newest 50) and pick from published versions and test builds."""
    if channel not in CHANNELS or channel == STABLE:
        return fetch_latest(opener, timeout)
    try:
        with opener(_request(RELEASES_URL, "application/vnd.github+json"), timeout=timeout) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    found = [r for item in (data if isinstance(data, list) else []) if isinstance(item, dict)
             for r in [release_from_json(item, tests=True)] if r is not None]
    return with_notes_since(found, pick(found, channel))


def with_notes_since(releases: list[Release], newest: Release | None, current: Key | None = None) -> Release | None:
    """`newest` with the notes of every test build between this copy and it, newest first, so a tester who skipped
    a few builds sees everything that changed. A published version keeps its own notes (its CHANGELOG section)."""
    current = current_key() if current is None else current
    if newest is None or not newest.test or current is None:
        return newest
    between = sorted((r for r in releases if r.test and current < r.key <= newest.key and r.notes),
                     key=lambda r: r.key, reverse=True)
    if len(between) < 2:
        return newest
    import dataclasses

    notes = "\n\n".join(f"## {r.label}\n\n{r.notes}" for r in between)
    return dataclasses.replace(newest, notes=notes)


def hash_for(hashes_text: str, name: str) -> str | None:
    """The SHA-256 listed for `name` in a SHA256.txt ("<hash>  <file name>" per line)."""
    for line in hashes_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lower() == name.lower() and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def fetch_hash(release: Release, opener: Opener = urllib.request.urlopen, timeout: float = 15) -> str | None:
    with opener(_request(release.hashes_url, "application/octet-stream"), timeout=timeout) as reply:
        text = reply.read(64 * 1024).decode("utf-8", "replace")
    return hash_for(text, release.installer_name)


def download(url: str, dest: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False, opener: Opener = urllib.request.urlopen,
             timeout: float = 30) -> str:
    """Save `url` to `dest` (through dest.part, so a half download never looks finished). Returns its SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    done = 0
    try:
        with opener(_request(url, "application/octet-stream"), timeout=timeout) as reply, open(part, "wb") as out:
            total = int(getattr(reply, "headers", {}).get("Content-Length") or 0)
            while chunk := reply.read(256 * 1024):
                if cancelled():
                    raise InterruptedError("download canceled")
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return digest.hexdigest()


def signature_state(path: Path) -> str:
    """Windows' verdict on the file's signature ("Valid", "NotSigned", "HashMismatch", ...)."""
    if sys.platform != "win32":
        return "NotSigned"
    script = f"(Get-AuthenticodeSignature -LiteralPath '{_ps_quote(str(path))}').Status.ToString()"
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("could not check the installer's signature: %s", exc)
        return "Unknown"
    return result.stdout.strip() or "Unknown"


def verify(path: Path, actual_hash: str, expected_hash: str | None,
           signature: Callable[[Path], str] = signature_state) -> str | None:
    """None if the installer is safe to run, else a short reason."""
    if not expected_hash:
        return "The release doesn't list a fingerprint for this installer."
    if actual_hash.lower() != expected_hash.lower():
        return "The download doesn't match the release's fingerprint."
    state = signature(path)
    if state not in SIGNATURE_OK:
        return f"Windows doesn't trust the installer's signature ({state})."
    return None


def installed_dir(executable: str | None = None) -> Path | None:
    """The folder Setup installed SprigCast to ({app}, which holds python\\ and unins000.exe), or None
    when running from source, where an installer has nothing to update."""
    app = Path(executable or sys.executable).resolve().parent.parent
    return app if (app / "unins000.exe").exists() else None


def _ps_quote(text: str) -> str:
    return text.replace("'", "''")


def installer_command(installer: Path, pid: int, restart: bool) -> list[str]:
    """A hidden PowerShell that waits for SprigCast (`pid`) to close, then runs Setup silently.

    Setup's own progress window shows while it works. /restartapp=1 makes Setup start SprigCast again
    when it's done (sprigcast.iss, RestartAfterUpdate).
    """
    args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOCANCEL"]
    if restart:
        args.append("/restartapp=1")
    arg_list = ",".join(f"'{a}'" for a in args)
    script = (
        f"$p = Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue; "
        "if ($p) { [void]$p.WaitForExit(180000) }; "
        f"Start-Process -FilePath '{_ps_quote(str(installer))}' -ArgumentList {arg_list}"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script]


def launch_installer(installer: Path, pid: int, restart: bool) -> None:
    """Start installer_command detached, so it outlives SprigCast."""
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(installer_command(installer, pid, restart), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log.info("update: %s runs once SprigCast closes (restart=%s)", installer.name, restart)


def clear_old(folder: Path, keep: str | None = None) -> None:
    """Remove earlier downloads (installers that already ran, or half downloads)."""
    if not folder.is_dir():
        return
    for item in folder.iterdir():
        if item.is_file() and item.name != keep:
            try:
                item.unlink()
            except OSError:
                pass  # Setup may still be running from it
