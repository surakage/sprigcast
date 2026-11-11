"""Stream keys live in the operating system's credential store (Windows Credential Manager),
never in scenes.json. Outputs only keep the name of the entry (`key_ref`)."""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

KEYRING_SERVICE = "SprigCast"


def get_key(key_ref: str | None) -> str | None:
    if not key_ref:
        return None
    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, key_ref) or None
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
