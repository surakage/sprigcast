"""Stream keys live in the operating system's credential store (Windows Credential Manager),
never in scenes.json. Outputs only keep the name of the entry (`key_ref`)."""

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
        import keyring

        key = keyring.get_password(KEYRING_SERVICE, key_ref) or None
        _present[key_ref] = key is not None
        return key
    except Exception as exc:  # no backend (some Linux setups), locked store, ...
        log.warning("could not read stream key %s: %s", key_ref, exc)
        return None


def set_key(key_ref: str, key: str) -> None:
    """Save a key, or delete it when key is empty. Raises if the credential store is unavailable."""
    import keyring

    if key:
        keyring.set_password(KEYRING_SERVICE, key_ref, key)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, key_ref)
        except keyring.errors.PasswordDeleteError:
            pass
    _present[key_ref] = bool(key)
