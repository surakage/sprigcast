"""Model files that plugins download the first time they're used (never shipped in the installer).

A plugin names each file, where to get it and its SHA-256, and asks for it when it's needed:

    from sprigcast import models

    path = models.ensure("selfie/fast.onnx", URL, SHA256, size=463_000,
                         progress=lambda done, total: ..., cancel=threading.Event())

Files live in the models folder (next to your settings, see folder()), with any "/" in the name
as a subfolder, so "whisper-base/model.bin" is models/whisper-base/model.bin. A download goes to a
.part file first and only takes the real name once its SHA-256 matches, so a cut-off or tampered
download never looks finished. Two callers asking for the same file share one download.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

_folder: Path | None = None
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()
_checked: dict[str, tuple[float, int]] = {}  # name -> (mtime, size) of a file whose hash was checked this run


class ModelError(RuntimeError):
    """A model couldn't be downloaded (the message is short and plain, fit to show)."""


def folder() -> Path:
    """Where models are kept: <settings folder>/models (on Windows, AppData/Local/SprigCast/models)."""
    if _folder is not None:
        return _folder
    base = ""
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    except Exception:  # noqa: BLE001 (no Qt: tools and tests)
        base = ""
    return Path(base or Path.home() / ".sprigcast") / "models"


def set_folder(path: Path | None) -> None:
    """Keep models somewhere else (tests). None goes back to the usual place."""
    global _folder
    _folder = None if path is None else Path(path)
    _checked.clear()


def path_for(name: str) -> Path:
    """The file for a model name. Names are relative, with "/" between folders; nothing may climb out."""
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts or any(p in ("", ".", "..") or ":" in p for p in parts) or name.startswith(("/", "\\")):
        raise ValueError(f"bad model name: {name!r}")
    return folder().joinpath(*parts)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_ready(name: str, sha256: str | None = None) -> bool:
    """The file is here (and, given a hash, matches it). The hash is checked once a run per file."""
    try:
        path = path_for(name)
        stat = path.stat()
    except (OSError, ValueError):
        return False
    if not sha256:
        return True
    seen = (stat.st_mtime, stat.st_size)
    if _checked.get(name) == seen:
        return True
    try:
        ok = _hash(path) == sha256.lower()
    except OSError:
        return False
    if ok:
        _checked[name] = seen
    return ok


def _lock(name: str) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(name, threading.Lock())


def ensure(name: str, url: str, sha256: str, size: int = 0,
           progress: Callable[[int, int], None] | None = None,
           cancel: threading.Event | None = None, opener=None, timeout: float = 30) -> Path:
    """The model file, downloading it first if it isn't here yet. Blocks: call it off the UI thread.

    progress(done_bytes, total_bytes) is called as it downloads (total is `size` when the server
    doesn't say, else 0). Setting `cancel` stops it: raises InterruptedError. A failed download or
    a file that doesn't match `sha256` raises ModelError, and nothing is kept.
    """
    from sprigcast import update

    path = path_for(name)
    with _lock(name):
        if is_ready(name, sha256):
            return path
        if cancel is not None and cancel.is_set():
            raise InterruptedError("download cancelled")
        log.info("downloading model %s", name)

        def report(done: int, total: int) -> None:
            if progress is not None:
                progress(done, total or size)

        kwargs = {"opener": opener} if opener is not None else {}
        try:
            got = update.download(url, path, report, (lambda: cancel is not None and cancel.is_set()),
                                  timeout=timeout, **kwargs)
        except InterruptedError:
            raise
        except OSError as exc:
            log.warning("model %s download failed: %s", name, exc)
            raise ModelError("Couldn't download it. Check your internet and try again.") from exc
        if got != sha256.lower():
            path.unlink(missing_ok=True)
            log.warning("model %s: hash %s, expected %s", name, got, sha256)
            raise ModelError("The download didn't match. Try again later.")
        stat = path.stat()
        _checked[name] = (stat.st_mtime, stat.st_size)
        log.info("model %s ready (%d bytes)", name, stat.st_size)
        return path


def remove(name: str) -> None:
    """Delete a downloaded model (and its folder, once empty)."""
    path = path_for(name)
    path.unlink(missing_ok=True)
    _checked.pop(name, None)
    parent = path.parent
    while parent != folder() and parent.is_relative_to(folder()):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
