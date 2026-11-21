"""Before going live: can each place's server be reached at all?

A place that can't connect was only found out after going live, by its status and the log (YouTube refused five
times, live 2026-11-21). This looks first, for every place at once, and the Stream doctor's card names the ones
that would fail before anything is sent.

Only a network connection to the server's address is made (and closed): nothing is said to it, and the stream key
is never used. Publishing with the key, even for a moment, can start a stream or tell followers one started, so
whether a key is accepted is asked from the platform for signed-in places instead (output/service_signin.py,
main_window._fresh_youtube_key and _fresh_twitch_key).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

EACH_S = 1.5  # one server's time to answer
ALL_S = 2.0  # the whole check: going live never waits longer for it
PORTS = {"rtmp": 1935, "rtmps": 443, "srt": 0, "http": 80, "https": 443}


def address(url: str) -> tuple[str, int] | None:
    """(host, port) of a stream address, None when it isn't a network address this can try (a file, SRT's UDP)."""
    try:
        parts = urlsplit(str(url))
        port = parts.port or PORTS.get(parts.scheme.lower(), 0)
    except ValueError:
        return None
    return (parts.hostname, port) if parts.hostname and port else None


def reach(host: str, port: int, timeout: float = EACH_S) -> str:
    """"" when a connection to host:port opens, else why not, in plain words."""
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except socket.gaierror:
        return "can't be found by name"
    except TimeoutError:
        return "didn't answer"
    except ConnectionRefusedError:
        return "refused the connection"
    except OSError as exc:
        return f"can't be reached ({exc.strerror or exc.__class__.__name__})"
    return ""


def check_places(targets, each_s: float = EACH_S, all_s: float = ALL_S, try_one=reach) -> list[str]:
    """One line for each place whose server can't be reached, by the place's name and the server's (never the
    key or the address's path). Every server is tried at once; [] when all answer or there is nothing to try."""
    places: dict[tuple[str, int], list[str]] = {}
    for t in targets:
        url = getattr(getattr(t, "enhanced", None), "url", None) or getattr(t, "server", None) or ""
        where = address(url)
        if where is not None:
            places.setdefault(where, []).append(t.name)
    found: dict[tuple[str, int], str] = {}

    def one(where: tuple[str, int]) -> None:
        found[where] = try_one(where[0], where[1], each_s)

    began = time.monotonic()
    threads = [threading.Thread(target=one, args=(where,), name="sprigcast-preflight", daemon=True) for where in places]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, began + all_s - time.monotonic()))
    failed = {where: found.get(where, "didn't answer") for where in places if found.get(where, "late") != ""}
    if not failed:
        return []
    if len(failed) == len(places) and all("found by name" in why for why in failed.values()):
        return ["This PC can't reach the internet right now, so no place would connect."]
    return [f"{_and(names)} would not connect: {where[0]} {failed[where]}."
            for where, names in places.items() if where in failed]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
