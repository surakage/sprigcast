"""Things that run while the window's thread waits on a job (workers.wait_answering).

PC crash 2026-11-20 17:22 UTC (build 44779596950): StreamBridge asked for a bloom screenshot over the Control API.
It was answered inside the socket's own signal, the screenshot waited with the window answering, Settings was
opened and closed meanwhile, and the connection was let go under Qt's socket code: an access violation in
Qt6Core once the answer came back.
"""

import json
import time

from sprigcast.control import native
from sprigcast.control.native import NativeApi
from test_control_api import FakeHost, _collection, _qt, _wait


def test_a_step_asked_for_mid_wait_runs_once_the_wait_is_over():
    app = _qt()
    from sprigcast.core import workers

    ran, during = [], []

    def job():
        time.sleep(0.15)
        return "done"

    def meanwhile():
        during.append(workers.when_free(lambda: ran.append(workers.answering())))

    from PySide6.QtCore import QTimer

    QTimer.singleShot(20, meanwhile)
    assert workers.wait_answering(job) == "done"
    assert during == [True] and ran == []  # held, not run inside the wait
    assert _wait(app, lambda: ran)
    assert ran == [False]  # it ran after, outside any wait
    assert workers.when_free(lambda: None) is False  # not waiting: do it now


def test_a_connection_closed_while_its_answer_is_made_is_let_go_safely(monkeypatch):
    app = _qt()
    from PySide6.QtCore import QCoreApplication, QEvent, QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi
    from sprigcast.core import workers

    client = QWebSocket()
    started = []

    def slow(_api, _data):
        started.append(1)

        def hang_up_then_work():
            time.sleep(0.4)
            return {"ok": 1}

        client.abort()  # the asker gives up while the answer is being made
        out = workers.wait_answering(hang_up_then_work)
        # What a pop-up's own event loop would do meanwhile: let go of everything deleteLater() queued.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return out

    monkeypatch.setattr(native, "_PLUGIN_ACTIONS", {})
    native.register_action("test.slow", "Answer slowly.", {}, slow)
    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    assert server.start(0)
    got = []
    client.textMessageReceived.connect(lambda t: got.append(json.loads(t)))
    try:
        client.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        assert _wait(app, lambda: got)
        client.sendTextMessage(json.dumps({"id": 1, "do": "test.slow"}))
        assert _wait(app, lambda: started)
        assert _wait(app, lambda: not server.sessions)
        for _ in range(20):  # the server carries on after the wait; nothing crashed
            app.processEvents()
            time.sleep(0.01)
        assert not any(m.get("id") == 1 for m in got)
    finally:
        server.stop()


def test_messages_that_arrive_during_an_answer_wait_their_turn(monkeypatch):
    """PC 2026-11-20 12:05: screenshots asked twice a second, each 1.7 s, every next one answered inside the one
    before's wait, until the window's thread ran out of stack. Now one at a time, in order, none nested."""
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi
    from sprigcast.core import workers

    depth, deepest, order = [0], [0], []

    def slow(_api, data):
        depth[0] += 1
        deepest[0] = max(deepest[0], depth[0])
        try:
            workers.wait_answering(time.sleep, 0.1)
            order.append(data.get("n"))
            return {"n": data.get("n")}
        finally:
            depth[0] -= 1

    monkeypatch.setattr(native, "_PLUGIN_ACTIONS", {})
    native.register_action("test.slow", "Answer slowly.", {"n": "?which"}, slow)
    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    assert server.start(0)
    client, got = QWebSocket(), []
    client.textMessageReceived.connect(lambda t: got.append(json.loads(t)))
    try:
        client.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        assert _wait(app, lambda: got)
        for n in range(6):  # all at once: far faster than they're answered
            client.sendTextMessage(json.dumps({"id": n, "do": "test.slow", "n": n}))
        assert _wait(app, lambda: sum(1 for m in got if "id" in m) == 6, 10.0)
        assert deepest[0] == 1 and order == list(range(6))
        assert [m["id"] for m in got if "id" in m] == list(range(6))
    finally:
        client.close()
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()


def test_a_connection_asking_far_faster_than_answers_come_is_closed(monkeypatch):
    app = _qt()
    from PySide6.QtCore import QUrl
    from PySide6.QtWebSockets import QWebSocket

    from sprigcast.app import control_server
    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi
    from sprigcast.core import workers

    monkeypatch.setattr(control_server, "MAX_WAITING", 3)
    monkeypatch.setattr(native, "_PLUGIN_ACTIONS", {})
    native.register_action("test.slow", "Answer slowly.", {}, lambda _a, _d: workers.wait_answering(time.sleep, 0.3))
    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    assert server.start(0)
    client, got, closed = QWebSocket(), [], []
    client.textMessageReceived.connect(lambda t: got.append(json.loads(t)))
    client.disconnected.connect(lambda: closed.append(client.closeCode()))
    try:
        client.open(QUrl(f"ws://127.0.0.1:{server.port}/sprigcast"))
        assert _wait(app, lambda: got)
        for n in range(10):
            client.sendTextMessage(json.dumps({"id": n, "do": "test.slow"}))
        assert _wait(app, lambda: closed, 10.0)
        assert closed[0].value == 4008
    finally:
        _wait(app, lambda: not server.sessions, 2.0)
        server.stop()


def test_settings_asked_for_between_answers_opens_before_the_next_one(monkeypatch):
    """Requests kept coming while Settings was asked for from the Sound gear: it waited behind all of them."""
    app = _qt()
    from PySide6.QtCore import QTimer

    from sprigcast.app.control_server import ControlServer
    from sprigcast.control.api import ControlApi
    from sprigcast.core import workers

    host = FakeHost(_collection())
    server = ControlServer(ControlApi(host), native=NativeApi(host))
    events = []

    def answer(n):
        def run():
            if n == 0:
                QTimer.singleShot(20, lambda: workers.when_free(lambda: events.append("settings")) or
                                  events.append("settings now"))
            workers.wait_answering(time.sleep, 0.1)
            events.append(n)
        return run

    for n in range(3):
        server._queue(None, answer(n))
    assert _wait(app, lambda: len(events) == 4, 5.0)
    assert events == [0, "settings", 1, 2]
