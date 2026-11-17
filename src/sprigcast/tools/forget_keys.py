"""Delete every SprigCast entry from Windows Credential Manager (stream keys and sign-in tokens).

The uninstaller runs this (python -I -m sprigcast.tools.forget_keys) when you tick "Also delete my
SprigCast data". keyring saves entries as "SprigCast", or "<name>@SprigCast" when one already uses that
name, so both shapes go. Nothing else in Credential Manager is touched.
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


def main() -> None:
    print(f"Deleted {forget()} SprigCast entries from Credential Manager")


if __name__ == "__main__":
    main()
