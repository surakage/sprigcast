"""Accounts: who is signed in to Twitch, YouTube and Kick, and signing out.

One sign-in per platform serves everything that platform offers: the stream key on your places, Stream details
(title, category, tags) and chat. Signing in and out lives on each place in Settings → Stream (user, 2026-11-19:
no separate Accounts tab), so a platform's sign-in only shows once you've added a place on it. Stream details and
the Chat panel open Settings there (open_accounts) and use whoever is signed in.

Signing out of YouTube or Kick asks the service to forget SprigCast (a short web call, on a worker). Stream keys
already filled in on your places keep working: a key doesn't depend on the sign-in.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtWidgets import QApplication, QWidget

log = logging.getLogger(__name__)

PLATFORMS = (("twitch", "Twitch"), ("youtube", "YouTube"), ("kick", "Kick"))


def signed_in_as(service: str) -> str:
    """Who is signed in on that platform, "" when nobody. Cheap: QSettings only."""
    if service == "twitch":
        from sprigcast.output import twitch_auth

        try:
            return twitch_auth.remembered_login() or ""
        except Exception:  # noqa: BLE001
            return ""
    from sprigcast.output import stream_details

    return stream_details.account(service)


def offered(service: str) -> bool:
    """Twitch always; YouTube and Kick when this copy of SprigCast has their sign-in set up."""
    if service == "twitch":
        return True
    from sprigcast.output import service_signin

    return service_signin.available(service)


def sign_out(service: str) -> str:
    """Sign out of that platform everywhere (key fetching, Stream details, chat). "" or why it couldn't."""
    log.info("accounts: %s signed out", service)
    if service == "twitch":
        from sprigcast.output import twitch_auth

        try:
            twitch_auth.disconnect()
        except Exception as exc:  # noqa: BLE001
            return f"Couldn't sign out of Twitch: {exc}"
        return ""
    from sprigcast.output import stream_details

    stream_details._remember(service, "")  # shown at once; the service is told on a worker
    threading.Thread(target=stream_details.sign_out, args=(service,), name="accounts-sign-out",
                     daemon=True).start()
    return ""


def _main_window(parent: QWidget | None):
    """The main window (the one with Settings), from a pop-up over it or from anywhere."""
    widget = parent
    while widget is not None:
        if hasattr(widget, "_open_settings") and hasattr(widget, "collection"):
            return widget
        widget = widget.parentWidget()
    return next((w for w in QApplication.topLevelWidgets()
                 if hasattr(w, "_open_settings") and hasattr(w, "collection")), None)


def open_accounts(parent: QWidget | None, service: str | None = None) -> None:
    """Sign in to `service`: Settings → Stream, on that platform's first place (or the place list if none)."""
    main = _main_window(parent)
    if main is None:
        log.warning("accounts: no main window to open Settings from")
        return
    place = next((o.id for o in main.collection.outputs
                  if o.type == "stream" and service and (o.service or "") == service), None)
    main._open_settings("Stream/Places", place)
