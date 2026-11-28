"""Stream keys live in the operating system's credential store (Windows Credential Manager),
never in scenes.json. Outputs only keep the name of the entry (`key_ref`).

On Linux the store is the desktop's own (Secret Service: GNOME Keyring, KWallet). A PC without one can't save
a key, and saving says so in plain words (core/systems.py)."""

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

        from sprigcast.core import password_store

        try:
            key = password_store.ask(f"reading {key_ref}",
                                     lambda: keyring.get_password(KEYRING_SERVICE, key_ref)) or None
        except password_store.Unavailable:
            return None  # said once by password_store; the key may well be there (_present left as it was)
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

    from sprigcast.core import password_store

    if key:
        from sprigcast.core import privacy

        privacy.remember_secret(key)
        try:
            password_store.ask(f"saving {key_ref}", lambda: keyring.set_password(KEYRING_SERVICE, key_ref, key))
        except keyring.errors.NoKeyringError as exc:
            from sprigcast.core import systems

            if systems.is_linux():
                raise RuntimeError(systems.NO_KEY_STORE) from exc
            raise
    else:
        try:
            password_store.ask(f"removing {key_ref}", lambda: keyring.delete_password(KEYRING_SERVICE, key_ref))
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError, password_store.Unavailable):
            pass  # nothing saved, or nowhere it could have been saved
    _present[key_ref] = bool(key)
