"""Sign-in and Optimize workers never hold a window: a worker that drops the last reference to one frees
Qt objects off the UI thread, which crashed Windows with heap corruption after a YouTube sign-in
(2026-11-16, 0xc0000374 as the sign-in thread ended)."""

import os
import threading
import time
import urllib.parse
import urllib.request

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QObject  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from sprigcast.app import signin_dialog  # noqa: E402
from sprigcast.output import oauth_loopback, service_signin  # noqa: E402
from sprigcast.output.oauth_loopback import Cancelled, SignInError, Waiter, free_port  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def app():
    return QApplication.instance() or QApplication([])


class FakeFlow:
    url = "https://accounts.example/auth"

    def __init__(self, finish):
        self._finish = finish

    def finish(self, cancelled):
        return self._finish(cancelled)


def _wait_until(test, seconds=3.0):
    end = time.time() + seconds
    while not test() and time.time() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return test()


def _blocking(cancelled):
    while not cancelled.wait(0.05):
        pass
    raise Cancelled("Sign-in cancelled.")


def test_the_worker_gets_no_window_and_ends_when_closed():
    opened = []
    dialog = signin_dialog.SignInDialog("youtube", begin=lambda s: FakeFlow(_blocking),
                                        open_url=lambda url: opened.append(threading.current_thread()) or True)
    thread = dialog._thread
    assert opened == [threading.main_thread()]  # the browser opens on the UI thread
    assert thread.is_alive()
    assert thread._target is signin_dialog._work and signin_dialog._work.__closure__ is None
    assert not any(isinstance(arg, QObject) for arg in thread._args)
    assert not isinstance(getattr(thread._args[0], "__self__", None), QObject)  # a bound method of the flow
    dialog.reject()
    assert not thread.is_alive()  # cancelled and joined on close
    dialog.deleteLater()


def test_success_hands_back_the_key_on_the_ui_thread():
    fetched = service_signin.Fetched(account="Sloth Bloom", key="k-1")
    dialog = signin_dialog.SignInDialog("youtube", begin=lambda s: FakeFlow(lambda c: fetched), open_url=lambda u: True)
    assert _wait_until(lambda: dialog.result() == QDialog.DialogCode.Accepted)
    assert dialog.fetched is fetched
    dialog.deleteLater()


def test_an_error_is_shown_plainly():
    def denied(cancelled):
        raise SignInError("Google didn't allow the sign-in. Try again, or paste the stream key instead.")

    dialog = signin_dialog.SignInDialog("youtube", begin=lambda s: FakeFlow(denied), open_url=lambda u: True)
    assert _wait_until(lambda: "didn't allow" in dialog.message.text())
    assert dialog.cancel_button.text() == "Close" and dialog.fetched is None
    dialog.deleteLater()


def test_a_sign_in_that_cannot_start_says_so_without_a_worker():
    def broken(service):
        raise SignInError("Port 51587 is busy, so Kick sign-in can't finish.")

    dialog = signin_dialog.SignInDialog("kick", begin=broken, open_url=lambda u: pytest.fail("no browser"))
    assert dialog._thread is None and "busy" in dialog.message.text()
    dialog.reject()
    dialog.deleteLater()


def test_access_denied_from_the_browser_is_worded_for_the_person():
    w = Waiter(free_port())
    w.start()
    back = urllib.parse.urlencode({"state": w.state, "error": "access_denied"})
    threading.Thread(target=lambda: urllib.request.urlopen(f"{w.url}?{back}", timeout=5).read(), daemon=True).start()
    with pytest.raises(SignInError, match="Google didn't allow the sign-in"):
        w.wait(5, who="Google")
    w.stop()  # a second stop is harmless


def test_begin_runs_on_the_ui_thread_and_finish_on_a_worker(monkeypatch):
    monkeypatch.setattr(service_signin, "setting", lambda name: "yt-id")
    flow = service_signin.begin("youtube")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(flow.url).query))
    assert query["client_id"] == "yt-id" and query["redirect_uri"] == flow.waiter.url
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        flow.finish(cancel)  # stops its loopback server
    assert flow.waiter._servers == []


def test_sign_in_steps_are_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(service_signin, "setting", lambda name: "yt-id")
    caplog.set_level(logging.INFO)
    flow = service_signin.begin("youtube")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        flow.finish(cancel)
    text = caplog.text
    assert "sign-in: youtube started" in text and "sign-in: youtube cancelled" in text
    assert "yt-id" not in text  # nothing private in the log


def test_optimize_checks_run_without_the_window(monkeypatch):
    from sprigcast.app import optimize, wizard

    started = []

    class Recorder:
        def __init__(self, target, args=(), **kwargs):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(wizard.threading, "Thread", Recorder)
    from sprigcast.core.scene import Canvas, SceneCollection

    w = wizard.OptimizeWizard(SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)]), {})
    w._pending = [optimize.Check("x", "X", lambda p: "ok")]
    w._run_next()
    target, args = started[0]
    assert target is wizard._check_on_worker and not any(isinstance(a, QObject) for a in args)
    target(*args)
    assert w._results.get_nowait()[1] == "ok"
    w._timer.stop()
    w.deleteLater()


def test_cancelled_is_a_sign_in_error():
    assert issubclass(oauth_loopback.Cancelled, SignInError)


def test_requests_say_they_are_sprigcast_not_python():
    from sprigcast import __version__

    assert oauth_loopback.HEADERS["User-Agent"].startswith(f"SprigCast/{__version__}")
    assert "Python" not in oauth_loopback.HEADERS["User-Agent"]
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"access_token": "t"}'

    def fake_open(request, timeout):
        seen.update({k.lower(): v for k, v in request.header_items()})
        return Reply()

    import urllib.request as ur

    original = ur.urlopen
    ur.urlopen = fake_open
    try:
        oauth_loopback.post_form("https://signin.example/v1/token", {"a": "b"}, "helper")
        oauth_loopback.get_json("https://api.example/x", "tok", "API")
    finally:
        ur.urlopen = original
    assert seen["user-agent"].startswith("SprigCast/") and seen["x-sprigcast"] == __version__


def test_a_cloudflare_refusal_reads_as_words_not_a_dict(monkeypatch):
    cloudflare = {"error": {"type": "https://developers.cloudflare.com/support/troubleshooting/http-status-codes/"
                                    "cloudflare-1xxx-errors/error-1010/", "title": "Error 1010: Access denied",
                            "status": 403, "detail": "banned browser signature"}, "status": 403,
                  "error_description": ""}
    monkeypatch.setattr(service_signin, "post_form", lambda url, fields, who, **kw: cloudflare)
    with pytest.raises(SignInError) as err:
        service_signin._swap("youtube", "c", "v", "http://127.0.0.1:1/callback", "Google")
    text = str(err.value)
    assert text.startswith("Google sign-in failed: the sign-in service turned SprigCast away (Error 1010")
    assert "{" not in text and "developers.cloudflare" not in text and text.endswith(".")
    assert oauth_loopback.problem_text({"error": "slow_down", "error_description": "x"}).startswith("too many")
    assert oauth_loopback.problem_text({"error": "invalid_grant", "status": 400}) == "invalid_grant (400)"


def test_a_refusal_shows_the_reason_from_the_body():
    r = oauth_loopback.refusal_reason
    assert r(b'{"data": {}, "message": "Forbidden: missing scope streamkey:read"}') == \
        "Forbidden: missing scope streamkey:read"
    assert r(b'{"error": {"code": 403, "message": "The user is not enabled for live streaming."}}') == \
        "The user is not enabled for live streaming."
    assert r(b'{"title": "Error 1010: Access denied", "status": 403}') == "Error 1010: Access denied (Cloudflare)"
    assert r(b"<html><head><title>Attention Required! | Cloudflare</title></head></html>") == \
        "Attention Required! | Cloudflare"
    assert r(b"") == ""


def test_youtube_without_live_streaming_says_how_to_turn_it_on(monkeypatch):
    def refused(url, token, who, headers=None):
        raise SignInError("YouTube said 403: The user is not enabled for live streaming.")

    monkeypatch.setattr(service_signin, "get_json", refused)
    with pytest.raises(SignInError, match="youtube.com/features"):
        service_signin._youtube_key("tok")


def test_the_browser_page_names_the_service_and_escapes_what_comes_back():
    ok = oauth_loopback.result_page(True, "YouTube")
    assert "Signed in to YouTube" in ok and "<svg" in ok and "close this tab" in ok
    bad = oauth_loopback.result_page(False, "Kick", "<script>alert(1)</script>")
    assert "Sign-in didn't finish" in bad and "<script>" not in bad and "&lt;script&gt;" in bad
