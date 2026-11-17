"""Delete every secret SprigCast keeps, so none outlives an uninstall (the user's rule, 2026-11-17).

- Windows Credential Manager: stream keys and sign-in tokens. keyring saves entries as "SprigCast", or
  "<name>@SprigCast" when one already uses that name, so both shapes go. Nothing else there is touched.
- SprigCast's settings: every encrypted value (control API password, paired devices' keys, this PC's
  local key, plugin secrets) and any unreadable copy kept aside.
- api.json and connection.json, which hold a key for apps on this PC (Stream Deck plugin and others).

The uninstaller always runs this (python -I -m sprigcast.tools.forget_keys); "Also delete my SprigCast
data" then removes the rest. Settings that aren't secret (blooms, choices) stay.
"""
from __future__ import annotations

import sys

SERVICE = "SprigCast"


def is_ours(target: str) -> bool:
    return target == SERVICE or target.endswith("@" + SERVICE)


def forget() -> int:
    """Deletes SprigCast's credentials and returns how many went. Does nothing off Windows."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                    ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                    ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.c_void_p),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                      ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL)))]
    advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    advapi.CredFree.argtypes = [ctypes.c_void_p]

    count = wintypes.DWORD()
    creds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not advapi.CredEnumerateW("*" + SERVICE, 0, ctypes.byref(count), ctypes.byref(creds)):
        return 0  # none saved
    found = []
    try:
        for i in range(count.value):
            cred = creds[i].contents
            if cred.TargetName and is_ours(cred.TargetName):
                found.append((cred.TargetName, cred.Type))
    finally:
        advapi.CredFree(creds)
    gone = 0
    for target, kind in found:
        if advapi.CredDeleteW(target, kind, 0):
            gone += 1
    return gone


# Settings that hold a key, however they're saved (plain from older builds, or kept aside unreadable).
SECRET_SETTINGS = ("control/password", "control/devices", "control/local_key")


def forget_settings(settings=None) -> int:
    """Removes secret values from SprigCast's settings and returns how many went."""
    from PySide6.QtCore import QSettings

    from sprigcast.control.secret_store import PREFIX

    q = settings if settings is not None else QSettings("Slothbloom", "SprigCast")
    gone = 0
    for key in q.allKeys():
        value = q.value(key)
        secret = key in SECRET_SETTINGS or key.endswith("_unreadable") \
            or (isinstance(value, str) and value.startswith(PREFIX))
        if secret:
            q.remove(key)
            gone += 1
    q.sync()
    return gone


def forget_files() -> int:
    """Removes api.json and connection.json, which hand a key to apps on this PC."""
    import os
    from pathlib import Path

    from PySide6.QtCore import QCoreApplication, QStandardPaths

    QCoreApplication.setOrganizationName("Slothbloom")
    QCoreApplication.setApplicationName("SprigCast")
    config = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    files = [Path(config) / "api.json"] if config else []
    if os.environ.get("APPDATA"):
        files.append(Path(os.environ["APPDATA"]) / "SprigCast" / "connection.json")
    gone = 0
    for f in files:
        try:
            f.unlink()
            gone += 1
        except OSError:
            pass
    return gone


def main() -> None:
    print(f"Deleted {forget()} SprigCast entries from Credential Manager, {forget_settings()} saved secrets "
          f"and {forget_files()} key files")


if __name__ == "__main__":
    main()
