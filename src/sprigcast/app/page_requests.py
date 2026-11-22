"""Writes down what a page's sign-in requests to Google send, for pages that work elsewhere but say
"auth/requests-from-referer-<empty>-are-blocked" in SprigCast (Sound Alerts, 2026-11-22).

One request watcher on the page profile, put in front of page_video's when that one is there. Per request it only
looks at the host; for a sign-in host (Firebase Auth / Google token servers) it logs one line:

    page sign-in request: POST identitytoolkit.googleapis.com/v1/accounts:signUp from https://source.soundalerts.com
      (Referer https://source.soundalerts.com, Origin -, initiator https://source.soundalerts.com, kind XHR)

Only addresses' scheme and host are written (no paths of the page, no query values: those hold API keys and ids).
The first PER_PAGE lines per page address, then a line whenever what a page's sign-ins send changes (the case
that matters hours in: Referer / Origin going missing), CHANGES_TOTAL of those in all; a page that keeps
trying the same way never fills the log.
SPRIGCAST_PAGE_REQUESTS_OFF=1 leaves it out.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SIGN_IN_HOSTS = ("identitytoolkit.googleapis.com", "securetoken.googleapis.com", "firebaseinstallations.googleapis.com")
PER_PAGE = 4
TOTAL = 40
CHANGES_TOTAL = 200
_watchers: list = []  # the profile doesn't own its watcher


def origin_of(address: str) -> str:
    """'https://a.com/x?y' -> 'https://a.com'; '' or something without a host -> '-'."""
    address = (address or "").strip()
    if "://" not in address:
        return "-" if not address else address.split("/")[0][:40] or "-"
    scheme, rest = address.split("://", 1)
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rsplit("@", 1)[-1]
    return f"{scheme}://{host}" if host else "-"


class Counter:
    """Which lines are worth writing: each page address's first ones, then any that send something new."""

    def __init__(self):
        self.by_page: dict[str, int] = {}
        self.last: dict[str, tuple] = {}  # page address -> what its last sign-in request sent
        self.total = 0
        self.changes = 0

    def take(self, page: str, sent: tuple = ()) -> bool:
        changed = page in self.last and self.last[page] != sent
        self.last[page] = sent
        if changed and self.changes < CHANGES_TOTAL:
            self.changes += 1
            return True
        if self.total >= TOTAL or self.by_page.get(page, 0) >= PER_PAGE:
            return False
        self.by_page[page] = self.by_page.get(page, 0) + 1
        self.total += 1
        return True


def line(method: str, host: str, path: str, page: str, referer: str, origin: str, initiator: str, kind: str) -> str:
    return (f"page sign-in request: {method} {host}{path[:60]} from {origin_of(page)} (Referer {origin_of(referer)}, "
            f"Origin {origin_of(origin)}, initiator {origin_of(initiator)}, kind {kind})")


def install(profile, inner=None) -> bool:
    """Watch the profile's sign-in requests; inner (page_video's watcher) still sees every request."""
    if os.environ.get("SPRIGCAST_PAGE_REQUESTS_OFF"):
        return False
    try:
        from PySide6.QtWebEngineCore import QWebEngineUrlRequestInterceptor

        counter = Counter()

        class Watcher(QWebEngineUrlRequestInterceptor):
            def interceptRequest(self, info):  # noqa: N802  on the window's thread: a host check, nothing else
                try:
                    url = info.requestUrl()
                    host = url.host()
                    if host in SIGN_IN_HOSTS:
                        page = info.firstPartyUrl().toString()
                        headers = {bytes(k).decode("latin-1").lower(): bytes(v).decode("latin-1")
                                   for k, v in info.httpHeaders().items()}
                        referer, origin = headers.get("referer", ""), headers.get("origin", "")
                        initiator = info.initiator().toString()
                        sent = (origin_of(referer), origin_of(origin), origin_of(initiator))
                        if counter.take(origin_of(page), sent):
                            log.info(line(bytes(info.requestMethod()).decode("latin-1"), host, url.path(), page,
                                          referer, origin, initiator,
                                          info.resourceType().name.removeprefix("ResourceType")))
                except Exception:  # noqa: BLE001
                    log.debug("page sign-in request", exc_info=True)
                if inner is not None:
                    inner.interceptRequest(info)

        watcher = Watcher()
        profile.setUrlRequestInterceptor(watcher)
        _watchers.append(watcher)
        return True
    except Exception:  # noqa: BLE001
        log.exception("page sign-in requests: not watched")
        return False
