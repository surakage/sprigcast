"""Keep secrets (the API password, paired devices' keys) encrypted for this Windows user.

On Windows, protect() uses DPAPI (CryptProtectData, per user): the value in the registry reads
"dpapi:<base64>" and only this Windows account on this PC can turn it back. Old plain values still read
(unprotect passes them through) and are re-saved encrypted (see control_server).

On Linux and macOS these stay in SprigCast's own settings file, which only this user can read
(core/systems.py private_settings). Test build 16 put them in the system's password store ("keyring:<name>"),
and SprigCast needs them the moment it starts: with the keyring locked (automatic sign-in) the first start sat
3 min 23 s behind the keyring's own box, and a locked keyring would have meant a new password, and every app
paired again, at each start. Values saved that way are still read (through core/password_store.py, which
never waits on the box) and saved plain from then on. Stream keys and sign-ins do stay in the password store
(output/keys.py): they are read when first needed.
"""

from __future__ import annotations

import base64
import logging
import sys

log = logging.getLogger(__name__)

PREFIX = "dpapi:"
KEYRING_PREFIX = "keyring:"
KEYRING_SERVICE = "SprigCast control"
_ENTROPY = b"SprigCast control"


def available() -> bool:
    return sys.platform == "win32"


def is_protected(value: str) -> bool:
    return value.startswith((PREFIX, KEYRING_PREFIX))


def _keyring_here() -> bool:
    """Linux and macOS keep secrets in the system's password store."""
    return sys.platform.startswith("linux") or sys.platform == "darwin"


def _keyring_unprotect(saved: str) -> str:
    """A secret test build 16 put in the system's password store."""
    try:
        import keyring

        from sprigcast.core import password_store

        name = saved[len(KEYRING_PREFIX):]
        return password_store.ask("a saved Control API secret",
                                  lambda: keyring.get_password(KEYRING_SERVICE, name)) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("secret store: couldn't read a saved secret (%s)", type(exc).__name__)
        return ""


def _blob(data: bytes):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buffer = ctypes.create_string_buffer(data, len(data))
    return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer, Blob


def _call(name: str, data: bytes) -> bytes:
    import ctypes

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in, _keep, Blob = _blob(data)
    entropy, _keep2, _ = _blob(_ENTROPY)
    blob_out = Blob()
    ui_forbidden = 0x1
    # CryptProtectData and CryptUnprotectData take the same arguments (the name/description one is unused).
    ok = getattr(crypt32, name)(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, ui_forbidden,
                                ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"{name} failed ({ctypes.GetLastError()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def protect(value: str) -> str:
    """The value to save. Encrypted on Windows; as it is elsewhere."""
    from sprigcast.core import privacy

    privacy.remember_secret(value)  # never in a log line from now on
    if not value or not available():
        return value
    try:
        return PREFIX + base64.b64encode(_call("CryptProtectData", value.encode("utf-8"))).decode("ascii")
    except OSError as exc:
        log.warning("secret store: couldn't encrypt (%s); kept as it was", exc)
        return value


def unprotect(saved: str) -> str:
    """The secret from a saved value: decrypted, or a plain (old) value as it is. "" if it can't be read
    (saved by another Windows user or PC)."""
    from sprigcast.core import privacy

    if not is_protected(saved):
        privacy.remember_secret(saved)
        return saved
    if saved.startswith(KEYRING_PREFIX):
        value = _keyring_unprotect(saved) if _keyring_here() else ""
        if value:
            privacy.remember_secret(value)
        return value
    if not available():
        return ""
    try:
        value = _call("CryptUnprotectData", base64.b64decode(saved[len(PREFIX):])).decode("utf-8")
        privacy.remember_secret(value)  # never in a log line from now on
        return value
    except (OSError, ValueError) as exc:
        log.warning("secret store: couldn't read a saved secret (%s)", exc)
        return ""
