"""Placing in full, taking out and copying items, several changes as one undo step, dry runs and item.moved
(StreamBridge tidies layouts with these, 2026-11-20)."""

import json

import pytest

from sprigcast.control.native import ApiError, NativeApi, NativeSession

from test_control_layout import Host as SizesHost, _garden


class Host(SizesHost):
    def __init__(self, collection):
        super().__init__(collection)
        self.commits, self.fresh, self.replaced, self.instances = [], 0, 0, {}

    def commit(self, what):
        self.commits.append(what)

    def new_undo_step(self):
        self.fresh += 1

    def garden_replaced(self):
        self.replaced += 1

    def source_instance(self, source_id):
        return self.instances.get(source_id)


def _api():
    host = Host(_garden())
    return NativeApi(host), host


def _cam(api):
    return api.col.scene("main-h").items[0]


def test_place_takes_crop_bounds_and_alignment():
    api, host = _api()
    where = {"bloom": "Main", "item": "Cam", "shape": "horizontal"}
    out = api.run("item.place", {**where, "crop": {"top": 5, "right": 0}})
    assert _cam(api).crop == (10, 5, 0, 20)  # only the sides given change
    assert out["items"][0]["crop"] == {"left": 10, "top": 5, "right": 0, "bottom": 20}
    api.run("item.place", {**where, "bounds": "fit", "bounds_width": 800, "bounds_height": 600, "bounds_align": "top_left"})
    cam = _cam(api)
    assert (cam.bounds, cam.bounds_size, cam.bounds_align) == ("fit", (800, 600), "top_left")
    corner = cam.box((400, 200))[0]
    out = api.run("item.place", {**where, "align": "bottom_right", "rotation": 0})
    assert cam.align == "bottom_right" and cam.box((400, 200))[0] == pytest.approx(corner)  # the picture stays put
    assert out["items"][0]["bounds"] == {"mode": "fit", "width": 800, "height": 600, "align": "top_left"}
    api.run("item.place", {**where, "bounds": "off"})
    assert cam.bounds == "off"
    for wrong in ({"crop": {"up": 1}}, {"crop": {"left": -1}}, {"bounds": "squash"}, {"align": "middle"},
                  {"bounds_width": 0}, {"crop": {}}):
        with pytest.raises(ApiError) as err:
            api.run("item.place", {**where, **wrong})
        assert err.value.code == "bad_request"
    with pytest.raises(ApiError, match="bounds_width and bounds_height"):
        api.run("item.place", {"bloom": "Main", "item": "Logo", "shape": "horizontal", "bounds": "fill"})
    api.run("item.place", {**where, "x": 1})  # the old fields alone answer as they always did
    assert set(api.run("item.place", {**where, "x": 2})["items"][0]) == {"x", "y", "scale", "rotation"}


def test_lock_remove_and_duplicate():
    api, host = _api()
    where = {"bloom": "Main", "item": "Cam", "shape": "horizontal"}
    assert api.run("item.lock", where) == {"locked": True} and _cam(api).locked
    assert api.run("item.lock", {**where, "locked": False}) == {"locked": False}
    out = api.run("item.duplicate", where)
    assert out["name"] == "Cam 2" and out["shapes"] == ["horizontal"]
    copy = api.col.scene("main-h").items[-1]  # on top, placed the same way
    assert copy.source == out["item"] != "cam" and (copy.x, copy.y, copy.scale, copy.crop) == (100, 50, 2.0, (10, 0, 30, 20))
    assert api.col.source(out["item"]).kind == "camera"
    with pytest.raises(ApiError) as err:
        api.run("item.duplicate", {**where, "name": "Chat"})
    assert err.value.code == "busy"
    with pytest.raises(ApiError, match="Sprigs can't be copied"):
        api.run("item.duplicate", {"bloom": "Main", "item": "Box", "shape": "horizontal"})
    assert api.run("item.remove", where) == {"removed": "Cam", "shapes": ["horizontal"]}
    assert all(i.source != "cam" for i in api.col.scene("main-h").items)
    assert any(s.id == "cam" for s in api.col.sources)  # the source stays (it's still in the Box sprig)
    with pytest.raises(ApiError) as err:
        api.run("item.remove", {"sprig": "Box", "item": "Chat"})
    assert err.value.code == "not_found"


def test_source_remove_and_refresh():
    api, host = _api()
    assert api.run("source.remove", {"source": "Cam"}) == {"removed": "Cam", "places": 3}
    assert all(i.source != "cam" for s in api.col.scenes for i in s.items) and all(s.id != "cam" for s in api.col.sources)
    with pytest.raises(ApiError, match="isn't a web page"):
        api.run("source.refresh", {"source": "Logo"})
    with pytest.raises(ApiError) as err:
        api.run("source.refresh", {"source": "Chat"})  # off screen: nothing open to load again
    assert err.value.code == "not_allowed"

    class Page:
        _open, reloads = True, 0

        def reload(self):
            self.reloads += 1

    page = host.instances["chat"] = Page()
    assert api.run("source.refresh", {"source": "Chat"}) == {"refreshed": "Chat"} and page.reloads == 1


def _send(api, message):
    session = NativeSession(api=api)
    list(session.receive(json.dumps({"events": "none"})))  # welcomed
    return json.loads(list(session.receive(json.dumps(message)))[-1])


STEPS = [{"do": "item.place", "bloom": "Main", "item": "Cam", "shape": "horizontal", "x": 0, "y": 0},
         {"do": "item.place", "bloom": "Main", "item": "Chat", "shape": "horizontal", "x": 1000},
         {"do": "item.order", "bloom": "Main", "item": "Cam", "shape": "horizontal", "to": "top"}]


def test_steps_as_one_are_saved_once_as_one_undo_step():
    api, host = _api()
    out = _send(api, {"id": "7", "steps": STEPS, "one_step": True})
    assert out["ok"] and out["id"] == "7" and out["result"]["applied"] is True
    assert [s["ok"] for s in out["result"]["steps"]] == [True, True, True]
    assert host.commits == ["items"] and host.fresh == 1 and host.replaced == 1  # once, its own undo step
    items = api.col.scene("main-h").items
    assert items[-1].source == "cam" and (items[-1].x, items[-1].y) == (0, 0)
    assert next(i for i in items if i.source == "chat").x == 1000


def test_one_failing_step_changes_nothing():
    api, host = _api()
    before = api.col.to_text()
    out = _send(api, {"steps": [*STEPS[:2], {"do": "item.place", "bloom": "Main", "item": "Nope", "x": 1}], "one_step": True})
    assert not out["ok"] and out["result"]["applied"] is False
    assert out["result"]["steps"][-1]["error"]["code"] == "not_found"
    assert api.col.to_text() == before and host.commits == []
    # Only changes to the garden can be taken as one: anything else is refused, and nothing is done.
    out = _send(api, {"steps": [STEPS[0], {"do": "stream.start"}], "one_step": True})
    assert not out["ok"] and "can't be in one step" in out["result"]["steps"][-1]["error"]["message"]
    assert api.col.to_text() == before and host.live is None
    out = _send(api, {"steps": [STEPS[0], {"wait": 100}], "one_step": True})
    assert not out["ok"] and out["error"]["code"] == "bad_request" and api.col.to_text() == before


def test_a_dry_run_says_where_things_would_be_and_keeps_nothing():
    api, host = _api()
    before = api.col.to_text()
    out = _send(api, {"steps": [{"do": "item.place", "bloom": "Main", "item": "Chat", "shape": "horizontal", "x": 700}],
                      "dry_run": True})
    result = out["result"]
    assert out["ok"] and result["dry_run"] is True and result["applied"] is False
    assert api.col.to_text() == before and host.commits == [] and host.replaced == 0
    shape = result["layouts"][0]["shapes"]["horizontal"]
    chat = next(i for i in shape["items"] if i["name"] == "Chat")
    assert chat["box"] == {"x": 700, "y": 100, "width": 400, "height": 400}  # where it would be
    assert ("Chat", "Cam") in {(p["a_name"], p["b_name"]) for p in shape["overlaps"]}
    # Plain steps (no one_step) still work as before: each saved on its own.
    out = _send(api, {"steps": STEPS})
    assert out["ok"] and "applied" not in out["result"] and host.commits == ["items"] * 3


def test_undo_and_redo_for_apps():
    api, host = _api()
    with pytest.raises(ApiError) as err:
        api.run("edit.undo", {})
    assert err.value.code == "not_allowed"  # this host has none
    host.undo, host.redo = lambda: True, lambda: False
    assert api.run("edit.undo", {}) == {"undone": True} and api.run("edit.redo", {}) == {"redone": False}


def test_apps_hear_when_something_is_moved_sized_cropped_or_turned():
    api, host = _api()
    api.changes()
    _cam(api).x = 300  # by hand, in Build
    api.col.scene("p-box").items[0].rotation = 15
    events = [(n, d) for n, d in api.changes() if n == "item.moved"]
    assert len(events) == 2
    cam = next(d for _, d in events if d.get("bloom") == "Main")
    assert (cam["item"], cam["itemId"], cam["shape"], cam["x"], cam["y"]) == ("Cam", "cam", "horizontal", 300, 50)
    assert cam["crop"] == {"left": 10, "top": 0, "right": 30, "bottom": 20} and cam["scale_x"] == 2.0
    logo = next(d for _, d in events if d.get("sprig") == "Box")
    assert logo["item"] == "Logo" and logo["rotation"] == 15
    assert [n for n, _ in api.changes()] == []  # said once
    _cam(api).y = 60
    assert api.changes(full=False) == []  # the light watch doesn't write places out; the edit's full look does
    assert [n for n, _ in api.changes()] == ["item.moved"]
    from sprigcast.control.native import event_group

    assert event_group("item.moved") == "item"
    assert {"item.place_full", "item.edit", "edit.undo", "steps.dry_run", "events.item_moved"} <= set(
        api.run("app.info", {})["features"])


def test_in_the_real_window_a_batch_is_one_ctrl_z(tmp_path, monkeypatch):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets")
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    _app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastLayoutEditTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(_garden(), tmp_path / "scenes.json")
    try:
        api = w.control.native
        cam = lambda: next(i for i in w.collection.scene("main-h").items if i.source == "cam")  # noqa: E731
        api.run("item.place", {"bloom": "Main", "item": "Cam", "shape": "horizontal", "x": 150})  # a change just before
        steps = len(w._undo)
        out = api.run_batch([{"do": "item.place", "bloom": "Main", "item": "Cam", "shape": "horizontal", "x": 5, "y": 6},
                             {"do": "item.place", "bloom": "Main", "item": "Chat", "shape": "horizontal", "x": 7}])
        assert out["applied"] and (cam().x, cam().y) == (5, 6)
        assert len(w._undo) == steps + 1  # its own step, though it came right after the other change
        assert api.run("edit.undo", {}) == {"undone": True}
        assert (cam().x, cam().y) == (150, 50)  # exactly the batch is taken back
        assert next(i for i in w.collection.scene("main-h").items if i.source == "chat").x == 1500
        assert api.run("edit.redo", {}) == {"redone": True} and cam().x == 5
        before = w.collection.to_text()
        dry = api.run_batch([{"do": "item.place", "bloom": "Main", "item": "Cam", "shape": "horizontal", "x": 900}], True)
        assert dry["dry_run"] and w.collection.to_text() == before and len(w._undo) == steps + 1
    finally:
        w.close()


def test_a_sound_made_in_a_batch_can_be_muted_in_the_same_batch(caplog):
    """StreamBridge 4.1.136, 2026-11-21: source.new app_audio then sound.mute as one step failed the whole batch,
    because a sound's own settings couldn't be in one. They can: they're saved in the garden like the rest."""
    import logging

    api, host = _api()
    steps = [{"do": "source.new", "name": "Raid clip sound", "kind": "app_audio", "settings": {"app": "clip.exe"}},
             {"do": "sound.mute", "sound": "Raid clip sound", "muted": True},
             {"do": "sound.volume", "sound": "Raid clip sound", "db": -12},
             {"do": "sound.balance", "sound": "Raid clip sound", "balance": -0.5}]
    before = api.col.to_text()
    with caplog.at_level(logging.INFO):
        out = _send(api, {"steps": steps, "dry_run": True})
        assert out["ok"] and api.col.to_text() == before and "changed Raid clip sound" not in caplog.text  # nothing done
        out = _send(api, {"steps": steps, "one_step": True})
    assert out["ok"] and out["result"]["applied"] is True and [s["ok"] for s in out["result"]["steps"]] == [True] * 4
    made = next(s for s in api.col.sources if s.name == "Raid clip sound")
    assert made.settings["muted"] is True and made.settings["volume_db"] == -12 and made.settings["balance"] == -0.5
    assert out["result"]["steps"][1]["result"]["muted"] is True
    assert host.commits == ["sound"] and host.fresh == 1 and caplog.text.count("changed Raid clip sound") == 3
    # One that fails still changes nothing.
    out = _send(api, {"steps": [{"do": "sound.mute", "sound": "Raid clip sound", "muted": False},
                                {"do": "sound.mute", "sound": "Nope"}], "one_step": True})
    assert not out["ok"] and made.settings["muted"] is True
