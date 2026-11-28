"""Stream keys live in the operating system's credential store (Windows Credential Manager),
never in scenes.json. Outputs only keep the name of the entry (`key_ref`).

On Linux the store is the desktop's own (Secret Service: GNOME Keyring, KWallet). A PC without one can't save
a key, and saving says so in plain words (core/linux.py)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

KEYRING_SERVICE = "SprigCast"

# What we learned about each entry this run. Loading keyring costs several MB (it scans installed
# packages to pick a backend), so screens that only show "key missing" hints use probably_has_key()
# and the credential store is first opened when a key is really needed (going live, editing keys).
_present: dict[str, bool] = {}


def probably_has_key(key_ref: str | None) -> bool:
    """Cheap guess for hints: an output only gets a key_ref when a key is saved for it."""
    if not key_ref:
        return False
    return _present.get(key_ref, True)


def get_key(key_ref: str | None) -> str | None:
    if not key_ref:
        return None
    try:
        from sprigcast.core.warm import windows_store_only

        windows_store_only()
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, key_ref) or None
        _present[key_ref] = key is not None
        if key:
            from sprigcast.core import privacy

            privacy.remember_secret(key)  # a stream key or a sign-in: never in a log line
        return key
    except Exception as exc:  # no backend (some Linux setups), locked store, ...
        log.warning("could not read stream key %s: %s", key_ref, exc)
        return None


def set_key(key_ref: str, key: str) -> None:
    """Save a key, or delete it when key is empty. Raises if the credential store is unavailable."""
    from sprigcast.core.warm import windows_store_only

    windows_store_only()
    import keyring

    if key:
        from sprigcast.core import privacy

        privacy.remember_secret(key)
        try:
            keyring.set_password(KEYRING_SERVICE, key_ref, key)
        except keyring.errors.NoKeyringError as exc:
            from sprigcast.core import linux

            if linux.is_linux():
                raise RuntimeError(linux.NO_KEY_STORE) from exc
            raise
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, key_ref)
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass  # nothing saved, or nowhere it could have been saved
    _present[key_ref] = bool(key)
