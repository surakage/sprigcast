"""Help improve SprigCast ("Share logs automatically"): off unless the person turns it on. When on, SprigCast
sends its scrubbed log after each stream, after a crash (instead of asking with the crash card) and after an
import where something didn't carry over (user 2026-11-27: "track import errors... pull logs from users").

Consent is versioned: a "yes" given before version 2 covered stream summaries only, so those people are asked
once more (`needs_ask`) and stay on streams only until they answer. The answer, its date and the version are kept
on this PC; nothing is recorded on a server. Every upload carries `install_id()`, a random id made on this PC
(not from its hardware), shown in Settings → General → Logs so a tester can ask for their uploads to be deleted.

What is sent, in one .zip (the same scrubbing as Help → Report a problem, app/tester_report.py):
- summary.txt: SprigCast's version, Windows' build and "stream".
- about.txt: the PC (CPU, graphics card, memory, screens) and what the garden holds (canvases, places, sizes).
  No settings values.
- logs/: the stream's own report and the end of today's log.

Stream keys, passwords, tokens, the person's name and their PC's name, e-mail and IP addresses are removed
first, and stream servers keep only their host. Chat messages are never in the logs. Nothing goes out while
live: the zip is made and sent on a worker after the stream has ended. It goes to the same report inbox
(reports.slothbloom.com) with the kind "stream", which keeps it 30 days.

Asked once, after the first stream that lasted a minute or more (`maybe_ask`); Settings → General → Logs has
the same choice afterwards.
"""

from __future__ import annotations

import logging
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SETTING = "sharing/stream_reports"  # True: sharing is on
ASKED = "sharing/asked"
CONSENT = "sharing/consent_version"  # the version of the wording the person said yes or no to
CONSENT_DATE = "sharing/consent_date"
INSTALL = "sharing/install_id"
VERSION = 2  # 1: stream summaries only; 2: also crashes and imports
MIN_SECONDS = 60  # a quick test that never really went out isn't worth a report
DAY_LOG_TAIL = 4 * 2**20  # the end of today's log: the whole stream for a long day, well under the inbox's 8 MB
KIND = "stream"

QUESTION = "Help improve SprigCast?"
EXPLAIN = ("Share SprigCast's log with the developer after each stream, after a crash, and after an import that "
           "missed something, with your PC's details.\n\nStream keys, passwords, names, addresses and chat are "
           "removed first. Nothing is sent while you're live. Logs are kept 30 days. You can change this in "
           "Settings → General → Logs.")
YES, NO = "Share logs automatically", "No thanks"
LABEL = "Share logs automatically"
ASK_AFTER_MS = 10_000  # after a stream: once ending it has settled (encoders closed, the report written)
ASK_TRIES = 30  # looked at again this often (every ASK_AFTER_MS) while SprigCast isn't in front: 5 minutes


def _settings(settings=None):
    if settings is not None:
        return settings
    from PySide6.QtCore import QSettings

    return QSettings()


def enabled(settings=None) -> bool:
    return _settings(settings).value(SETTING, False, type=bool)


def consent_version(settings=None) -> int:
    s = _settings(settings)
    stored = s.value(CONSENT, 0, type=int)
    return stored or (1 if s.value(ASKED, False, type=bool) else 0)  # asked before versions: the stream-only wording


def covers(kind: str, settings=None) -> bool:
    """Is sharing on for this kind ("stream", "crash" or "import")? Crashes and imports need the version 2 yes."""
    if not enabled(settings):
        return False
    return kind == KIND or consent_version(settings) >= VERSION


def set_enabled(on: bool, settings=None) -> None:
    from datetime import date

    s = _settings(settings)
    s.setValue(SETTING, bool(on))
    s.setValue(ASKED, True)
    s.setValue(CONSENT, VERSION)
    s.setValue(CONSENT_DATE, date.today().isoformat())
    log.info("help improve SprigCast: share logs automatically %s (wording %d)", "on" if on else "off", VERSION)


def asked(settings=None) -> bool:
    return _settings(settings).value(ASKED, False, type=bool)


def needs_ask(settings=None) -> bool:
    """Never asked, or said yes to the stream-only wording: ask (once) with today's wording."""
    return not asked(settings) or (enabled(settings) and consent_version(settings) < VERSION)


def install_id(settings=None) -> str:
    """This install's random id, made the first time it's needed (uuid4: nothing about the PC or the person)."""
    import uuid

    s = _settings(settings)
    value = str(s.value(INSTALL, "") or "")
    if len(value) != 36:
        value = str(uuid.uuid4())
        s.setValue(INSTALL, value)
    return value


def make(target: Path, stream_report: Path | None, *, collection=None, log_folder: Path | None = None,
         scrub=None, now: datetime | None = None, kind: str = KIND) -> Path:
    """Write the stream summary zip to `target`. Raises OSError if it can't be written."""
    from sprigcast.app import tester_report
    from sprigcast.app.logbook import log_dir
    from sprigcast.app.problem_report import _summary

    now = now or datetime.now()
    folder = log_folder or log_dir()
    if scrub is None:
        scrub = tester_report.Scrubber.for_this_pc(tester_report.known_secrets(None, collection))
    about = [f"SprigCast {kind} report, {now:%Y-%m-%d %H:%M}", "", *tester_report.pc_lines()]
    if collection is not None:
        about += ["", *tester_report.collection_lines(collection)]
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("summary.txt", scrub(_summary(kind, None)))
        z.writestr("about.txt", scrub("\n".join(about) + "\n"))
        if stream_report is not None and stream_report.is_file():
            tester_report._copy_scrubbed(z, f"logs/{stream_report.name}", stream_report, scrub)
        day_log = folder / f"sprigcast {now:%Y-%m-%d}.log"
        if day_log.is_file():
            tester_report._copy_scrubbed(z, f"logs/{day_log.name}", day_log, scrub, DAY_LOG_TAIL)
    return target


def send_after_stream(stream_report: Path | None, seconds: float, *, collection=None, settings=None,
                      sender=None, run=None):
    """After a stream ends: when sharing is on and the stream lasted MIN_SECONDS, make and send the summary on a
    worker. Returns the worker's Future, or None when nothing is sent. Never raises."""
    try:
        if seconds < MIN_SECONDS or not covers(KIND, settings):
            return None
        from sprigcast.app import problem_report
        from sprigcast.core import workers

        sender = sender or problem_report.send
        run = run or workers.run

        def job():
            import tempfile

            target = Path(tempfile.gettempdir()) / f"SprigCast stream {datetime.now():%Y-%m-%d %H-%M-%S}.zip"
            try:
                make(target, stream_report, collection=collection)
                report_id = sender(target, KIND, f"stream of {int(seconds) // 60} min")
                log.info("stream summary sent to the developer (%s)", report_id)
                return report_id
            except OSError as exc:
                log.info("stream summary not sent (%s)", exc)
                return None
            finally:
                try:
                    target.unlink()
                except OSError:
                    pass

        return run(job, name="sprigcast-share")
    except Exception:  # noqa: BLE001 - sharing must never get in the way of ending a stream
        log.exception("stream summary")
        return None


def maybe_ask(window, seconds: float, settings=None, ask=None) -> bool | None:
    """Ask once, after the first stream of a minute or more. Returns the answer, or None when not asked."""
    if seconds < MIN_SECONDS or not needs_ask(settings):
        return None
    if ask is None:
        def ask() -> bool:
            from sprigcast.app.message import MessageCard
            from sprigcast.app.modal import modal

            from sprigcast.app.gardens_menu import FREE_OF_GARDEN

            card = MessageCard(QUESTION, EXPLAIN, window, "ask")
            card.setProperty(FREE_OF_GARDEN, True)  # only a question: apps may still switch gardens and paths
            card.add_button(NO)
            yes = card.add_button(YES, primary=True)
            log.info("asked: %s", QUESTION)
            modal(card)
            log.info("answered: %s", card.clicked.text() if card.clicked is not None else "closed")
            return card.clicked is yes

    answer = bool(ask())
    set_enabled(answer, settings)
    return answer


def wants_import_report(missed: str, settings=None) -> bool:
    """Whether an import that missed `missed` would send a report (cheap: check before copying the garden)."""
    try:
        return bool(missed) and covers("import", settings)
    except Exception:  # noqa: BLE001 - a report is never worth an error
        return False


def send_after_import(app: str, missed: str, notes: int, *, collection=None, settings=None, sender=None, run=None):
    """After an import that missed something (convert.missed_summary(): types only, no names): when sharing covers
    imports, send the log and what didn't carry over, on a worker. Returns the Future, or None. Never raises."""
    try:
        if not wants_import_report(missed, settings):
            return None
        from sprigcast.app import problem_report, tester_report
        from sprigcast.core import workers

        sender = sender or problem_report.send
        run = run or workers.run

        def job():
            import tempfile

            target = Path(tempfile.gettempdir()) / f"SprigCast import {datetime.now():%Y-%m-%d %H-%M-%S}.zip"
            try:
                make(target, None, collection=collection, kind="import")
                with zipfile.ZipFile(target, "a", compression=zipfile.ZIP_DEFLATED) as z:
                    scrub = tester_report.Scrubber.for_this_pc()
                    z.writestr("import.txt", scrub(f"Imported from {app}\nNotes shown: {notes}\n"
                                                   f"Didn't carry over: {missed}\n"))
                report_id = sender(target, "import", f"{app}: {missed}"[:160])
                log.info("import report sent to the developer (%s)", report_id)
                return report_id
            except OSError as exc:
                log.info("import report not sent (%s)", exc)
                return None
            finally:
                try:
                    target.unlink()
                except OSError:
                    pass

        return run(job, name="sprigcast-share")
    except Exception:  # noqa: BLE001 - sharing must never get in the way of an import
        log.exception("import report")
        return None
