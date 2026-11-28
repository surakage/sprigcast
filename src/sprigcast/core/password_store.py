"""Asking the system's password store (Secret Service, the Mac's Keychain) without ever waiting on it.

On a Linux desktop the store can put up its own box ("Choose password for new keyring", "Unlock keyring") and
the call waits for the answer. With automatic sign-in the keyring is locked, so the very first start sat behind
that box: no window for 3 min 23 s, and after Cancel the box came back twice more (Ubuntu 24.04 virtual PC, test
build 16, 2026-11-28).

So on Linux and macOS every call to the store goes through ask():
- it runs on its own thread and is waited on for WAIT_S at most. While the system's box is open, SprigCast
  carries on as if the secret weren't there, and nothing else is asked;
- a store that says no (the box dismissed, locked, none there) isn't asked again this run: one box at most.

Windows' Credential Manager never asks anything, and is called directly as before.
"""

from __future__ import annotations

import logging
import sys
import threading

log = logging.getLogger(__name__)

WAIT_S = 1.5  # how long a call may hold its caller (the window's thread, at start)
BOX_OPEN = "the system's password box is open; answer it and try again"
_REFUSALS = ("InitError", "KeyringLocked", "NoKeyringError", "PromptDismissedException", "LockedException",
             "SecretServiceNotAvailableException")

_lock = threading.Lock()
_pending: threading.Thread | None = None
_off = ""  # why the store isn't asked any more this run


class Unavailable(RuntimeError):
    """The store can't answer right now (its box is open) or won't this run."""


def gated() -> bool:
    return sys.platform != "win32"


def state() -> str:
    """"" while the store may be asked; else why not."""
    with _lock:
        if _off:
            return _off
        return BOX_OPEN if _pending is not None and _pending.is_alive() else ""


def reset() -> None:
    """Forget this run's refusals (tests; Settings' own "try again")."""
    global _pending, _off
    with _lock:
        _pending, _off = None, ""


def _refused(exc: BaseException) -> bool:
    words = f"{type(exc).__name__} {exc}".lower()
    return type(exc).__name__ in _REFUSALS or "dismissed" in words or "locked" in words


def ask(what: str, call, wait_s: float | None = None):
    """call()'s answer. Raises Unavailable when the store is waiting on its own box or was refused this run,
    and whatever call() raised otherwise."""
    global _pending, _off
    if not gated():
        return call()
    why = state()
    if why:
        raise Unavailable(why)
    box: dict = {}

    def work() -> None:
        global _off
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 (handed to the caller, or logged below)
            box["error"] = exc
            if _refused(exc):
                with _lock:
                    first = not _off
                    _off = f"the system's password store said no ({type(exc).__name__}); not asked again this run"
                if first:
                    none_here = type(exc).__name__ == "NoKeyringError"
                    log.log(logging.INFO if none_here else logging.WARNING,
                            "password store: %s (%s). Stream keys and sign-ins can't be read or saved until %s",
                            _off, what, "this PC has one (GNOME Keyring or KWallet)" if none_here else
                            "SprigCast starts again with the store unlocked")

    thread = threading.Thread(target=work, name="sprigcast-password-store", daemon=True)
    thread.start()
    thread.join(WAIT_S if wait_s is None else wait_s)
    if thread.is_alive():
        with _lock:
            _pending = thread
        log.warning("password store: no answer in %.1f s (%s): the system is probably asking for the keyring's "
                    "password. SprigCast carries on without it until that box is answered", WAIT_S, what)
        raise Unavailable(BOX_OPEN)
    if "error" in box:
        raise box["error"]
    return box.get("value")
