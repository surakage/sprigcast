"""Updates: find a newer SprigCast on GitHub Releases, download its installer, check it and run it.

No Qt here (app/updates.py is the window side), so every step can be tested on its own.

How an update goes:
1. `fetch_latest` asks GitHub for the newest release (drafts and pre-releases are skipped).
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

from sprigcast import __version__

log = logging.getLogger(__name__)

REPO = "surakage/sprigcast"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
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


@dataclass(frozen=True)
class Release:
    version: str
    notes: str  # the release's text (Markdown), from the CHANGELOG
    page_url: str
    installer_url: str
    installer_size: int
    hashes_url: str

    @property
    def installer_name(self) -> str:
        return installer_name(self.version)


def release_from_json(data: dict) -> Release | None:
    """A GitHub release -> Release, or None if it isn't a finished release with an installer and hashes."""
    if data.get("draft") or data.get("prerelease"):
        return None
    tag = str(data.get("tag_name", ""))
    if parse_version(tag) is None:
        return None
    version = tag.removeprefix("v")
    assets = {a.get("name"): a for a in data.get("assets", []) if isinstance(a, dict)}
    setup, hashes = assets.get(installer_name(version)), assets.get(HASHES_NAME)
    if not setup or not hashes:
        return None
    return Release(
        version=version,
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or f"https://github.com/{REPO}/releases"),
        installer_url=str(setup.get("browser_download_url", "")),
        installer_size=int(setup.get("size") or 0),
        hashes_url=str(hashes.get("browser_download_url", "")),
    )


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
                    raise InterruptedError("download cancelled")
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
