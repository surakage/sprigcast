"""Help improve SprigCast: after each stream, send its report to the developer. Off unless the person turns it on.

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

SETTING = "sharing/stream_reports"  # True: send after each stream
ASKED = "sharing/asked"
MIN_SECONDS = 60  # a quick test that never really went out isn't worth a report
DAY_LOG_TAIL = 4 * 2**20  # the end of today's log: the whole stream for a long day, well under the inbox's 8 MB
KIND = "stream"

QUESTION = "Help improve SprigCast?"
EXPLAIN = ("After each stream, send a summary to the SprigCast developer: how each place did, your PC's "
           "details and SprigCast's log.\n\nStream keys, passwords, names and addresses are removed first. "
           "Reports are kept 30 days. You can change this in Settings → General.")
YES, NO = "Send after each stream", "No thanks"


def _settings(settings=None):
    if settings is not None:
        return settings
    from PySide6.QtCore import QSettings

    return QSettings()


def enabled(settings=None) -> bool:
    return _settings(settings).value(SETTING, False, type=bool)


def set_enabled(on: bool, settings=None) -> None:
    s = _settings(settings)
    s.setValue(SETTING, bool(on))
    s.setValue(ASKED, True)
    log.info("help improve SprigCast: stream summaries %s", "on" if on else "off")


def asked(settings=None) -> bool:
    return _settings(settings).value(ASKED, False, type=bool)


def make(target: Path, stream_report: Path | None, *, collection=None, log_folder: Path | None = None,
         scrub=None, now: datetime | None = None) -> Path:
    """Write the stream summary zip to `target`. Raises OSError if it can't be written."""
    from sprigcast.app import tester_report
    from sprigcast.app.logbook import log_dir
    from sprigcast.app.problem_report import _summary

    now = now or datetime.now()
    folder = log_folder or log_dir()
    if scrub is None:
        scrub = tester_report.Scrubber.for_this_pc(tester_report.known_secrets(None, collection))
    about = [f"SprigCast stream summary, {now:%Y-%m-%d %H:%M}", "", *tester_report.pc_lines()]
    if collection is not None:
        about += ["", *tester_report.collection_lines(collection)]
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("summary.txt", scrub(_summary(KIND, None)))
        z.writestr("about.txt", scrub("\n".join(about) + "\n"))
        if stream_report is not None and stream_report.is_file():
            z.writestr(f"logs/{stream_report.name}", scrub(tester_report._read(stream_report)))
        day_log = folder / f"sprigcast {now:%Y-%m-%d}.log"
        if day_log.is_file():
            z.writestr(f"logs/{day_log.name}", scrub(tester_report._read(day_log, DAY_LOG_TAIL)))
    return target


def send_after_stream(stream_report: Path | None, seconds: float, *, collection=None, settings=None,
                      sender=None, run=None):
    """After a stream ends: when sharing is on and the stream lasted MIN_SECONDS, make and send the summary on a
    worker. Returns the worker's Future, or None when nothing is sent. Never raises."""
    try:
        if seconds < MIN_SECONDS or not enabled(settings):
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
    if seconds < MIN_SECONDS or asked(settings):
        return None
    if ask is None:
        def ask() -> bool:
            from sprigcast.app.message import MessageCard
            from sprigcast.app.modal import modal

            card = MessageCard(QUESTION, EXPLAIN, window, "ask")
            card.add_button(NO)
            yes = card.add_button(YES, primary=True)
            log.info("asked: %s", QUESTION)
            modal(card)
            return card.clicked is yes

    answer = bool(ask())
    set_enabled(answer, settings)
    return answer
