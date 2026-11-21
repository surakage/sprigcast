"""What apps call each place you stream to: a name a person can read ("Twitch (horizontal)"), the same on both
doors. Ids (out-5) keep working everywhere a name does."""

from __future__ import annotations

from typing import Any

OLD_PREFIX = "sprigcast:"  # the OBS door named a place sprigcast:<id> before it had a name: still understood


def place_names(col: Any) -> dict[str, str]:
    """Output id -> its name: the service and the shape it sends; a second one alike gets a number."""
    from sprigcast.output.stream import SERVICES

    names: dict[str, str] = {}
    for o in col.outputs:
        if o.type != "stream":
            continue
        service = SERVICES.get(o.service or "custom", SERVICES["custom"])
        base, n = f"{service.label} ({o.canvas})", 1
        name = base
        while name in names.values():
            n += 1
            name = f"{base} {n}"
        names[o.id] = name
    return names


def find_place(col: Any, wanted: str) -> Any | None:
    """The stream output asked for by id, name, old OBS-door name (sprigcast:<id>) or service."""
    places = [o for o in col.outputs if o.type == "stream"]
    names = place_names(col)
    key = wanted[len(OLD_PREFIX):] if wanted.startswith(OLD_PREFIX) else wanted
    return (next((o for o in places if o.id == key), None)
            or next((o for o in places if names[o.id] == wanted), None)
            or next((o for o in places if names[o.id].lower() == wanted.lower()), None)
            or next((o for o in places if o.service == wanted), None))


def place_state(now: dict[str, Any] | None) -> str:
    """One word for a place while live: stopped, connecting, live or reconnecting."""
    if not now:
        return "stopped"
    if now.get("state"):
        return str(now["state"])
    return "reconnecting" if now.get("reconnecting") else "live"
