"""Pop-ups that let go once they're closed.

A dialog run with .exec() stays a child of the window it was opened from. Most were freed once nothing in Python
held them any more, but any that something still held (a timer still queued, a closure kept by a page, a hook)
stayed for the rest of the session: on 2026-11-17 every Settings visit kept ~1,500 widgets and every Filters visit
another dialog. OBS deletes its properties and settings windows on close; modal() does the same for ours.
"""

from __future__ import annotations


def modal(dialog) -> int:
    """dialog.exec(), then delete it once control returns to the window's event loop. Reading its answers right
    after (dialog.values(), dialog.account) is fine: it's only deleted later."""
    from sprigcast.app.theme import ui_burst

    ui_burst()  # showing it lays out and polishes every widget: Python's lock comes back quickly meanwhile
    from sprigcast.app import privacy

    if privacy.hidden():  # Settings → General → Privacy: kept out of screen captures like the main window
        privacy.apply_to(dialog, True)
    try:
        return dialog.exec()
    finally:
        delete = getattr(dialog, "deleteLater", None)  # stand-ins in tests may not have it
        if delete is not None:
            delete()
