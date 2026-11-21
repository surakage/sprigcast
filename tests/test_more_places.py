"""More places to stream to: the five everyday ones up front, the rest behind "Show all…"."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sprigcast.core.scene import Canvas, Output, SceneCollection  # noqa: E402
from sprigcast.output import stream  # noqa: E402


@pytest.fixture
def fake_keyring(monkeypatch):
    import keyring

    store = {}
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: store.get((svc, ref)))
    monkeypatch.setattr(keyring, "set_password", lambda svc, ref, key: store.__setitem__((svc, ref), key))
    monkeypatch.setattr(keyring, "delete_password", lambda svc, ref: store.pop((svc, ref), None))
    return store


def _collection(*outputs):
    return SceneCollection(
        canvases=[Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)],
        outputs=list(outputs))


def test_the_list_is_a_to_z_with_custom_last_and_nothing_closed_or_for_adults():
    places = stream.all_services()
    keys = [k for k, _ in places]
    labels = [label for _, label in places]
    assert keys[-1] == "custom" and labels[:-1] == sorted(labels[:-1], key=str.lower)
    assert set(keys) == set(stream.SERVICES) and len(set(labels)) == len(labels)
    assert stream.TOP_SERVICES == ("twitch", "youtube", "kick", "tiktok", "facebook")
    assert {"facebook", "x", "chzzk", "soop", "restream", "beam", "shareplay"} <= set(keys)
    left_out = ("trovo", "kakao", "nimo", "cam4", "chaturbate", "stripchat", "myfreecams", "onlyfans", "xlovecam",
                "bongacams", "camsoda", "picarto", "piczel")
    assert not [name for name in labels if any(word in name.lower().replace(" ", "") for word in left_out)]
    for key in keys:  # every place has a starting quality within its own limit, and says where its key is found
        s = stream.SERVICES[key]
        assert 0 < s.kbps <= s.max_kbps and s.help and s.long_side in (1280, 1920) and s.fps in (30, 60)


def test_show_all_searches_and_picks_without_sideways_scroll():
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.place_picker import PlacePicker

    p = PlacePicker()
    assert p.list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert [p.list.item(r).text() for r in range(p.list.count())] == [label for _, label in stream.all_services()]
    assert not p.done_button.isEnabled()  # nothing picked yet
    p.search.setText("face")
    shown = [p.list.item(r).text() for r in range(p.list.count()) if not p.list.item(r).isHidden()]
    assert shown == ["Facebook"] and p.done_button.isEnabled()
    p.search.setText("zzzz")
    assert not p.done_button.isEnabled()
    p._pick()
    assert p.picked is None  # no match: Enter does nothing
    p.search.setText("SOOP")
    p.search.returnPressed.emit()
    assert p.picked == "soop" and p.result() == p.DialogCode.Accepted


def test_service_box_shows_the_top_five_then_show_all(fake_keyring, monkeypatch):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app import place_picker
    from sprigcast.app.stream_dialog import ALL_PLACES, StreamPage

    col = _collection(Output(id="o1", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/o1"),
                      Output(id="o2", type="stream", service="x", canvas="horizontal", key_ref="sprigcast/o2"))
    d = StreamPage(col)
    rows = [d.service.itemText(i) for i in range(d.service.count())]
    assert rows[-1] == stream.SHOW_ALL and rows[-2] == "Custom RTMP"
    assert [r for r in rows if r in ("Twitch", "YouTube", "Kick", "TikTok", "Facebook")] == [
        "Twitch", "YouTube", "Kick", "TikTok", "Facebook"]
    # Closed without a pick: the box goes back to what it showed.
    monkeypatch.setattr(place_picker, "pick_place", lambda parent=None: None)
    d.service.setCurrentIndex(d.service.findData(ALL_PLACES))
    assert d.service.currentData() == "twitch" and col.outputs[0].service == "twitch"
    # A pick from the list gets its own row and becomes the place's service.
    monkeypatch.setattr(place_picker, "pick_place", lambda parent=None: "restream")
    d.service.setCurrentIndex(d.service.findData(ALL_PLACES))
    assert d.service.currentData() == "restream" and d.service.currentText() == "Restream"
    assert d.service.itemData(d.service.count() - 1) == ALL_PLACES  # Show all stays last
    assert d.apply_to(col) and col.outputs[0].service == "restream"
    # A place already on one of the other services shows it by name.
    d2 = StreamPage(col)
    d2.list.setCurrentRow(1)
    assert d2.service.currentText() == "X"


def test_every_place_has_a_tile_and_get_started_offers_the_top_five():
    from sprigcast.app import getting_started, live_stage

    assert set(stream.SERVICES) - {"custom"} <= set(live_stage.PLATFORM_TILES)
    assert getting_started.PLACES == stream.TOP_SERVICES
    assert set(getting_started.PLACES) <= set(getting_started.PLACE_COLORS)
    assert [k for k, _ in live_stage.SERVICE_CHOICES] == [*stream.TOP_SERVICES, "custom"]


def test_settings_drop_downs_carry_the_drawn_chevron():
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app import stream_dialog, theme
    from sprigcast.app.settings_window import window_style

    rule = theme.combo_arrow_rule("#settingsWindow QComboBox")
    assert "::down-arrow" in rule and "chevron" in rule
    assert "#settingsWindow QComboBox::down-arrow { image: url(" in window_style()
    assert "#streamCard QComboBox::down-arrow { image: url(" in stream_dialog.PAGE_STYLE


def test_settings_logs_each_place_added_or_removed(fake_keyring, caplog):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    import logging

    from sprigcast.app.stream_dialog import StreamPage

    col = _collection(Output(id="o1", type="stream", service="twitch", canvas="horizontal", key_ref="sprigcast/o1"))
    d = StreamPage(col)
    d._add()
    d.service.setCurrentIndex(d.service.findData("kick"))
    d.list.setCurrentRow(0)
    d._remove()
    with caplog.at_level(logging.INFO, logger="sprigcast.app.stream_dialog"):
        assert d.apply_to(col)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("places:")]
    assert sorted(lines) == ["places: o1 (twitch) removed in Settings", "places: out-1 (kick) added in Settings"]


def test_places_drag_into_a_new_order_in_settings(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from PySide6.QtCore import QModelIndex
    from PySide6.QtWidgets import QListWidget

    from sprigcast.app.stream_dialog import StreamPage

    col = _collection(*(Output(id=k, type="stream", service=k, canvas="horizontal", key_ref=f"sprigcast/{k}")
                        for k in ("twitch", "youtube", "kick")))
    d = StreamPage(col)
    assert d.list.dragDropMode() == QListWidget.DragDropMode.InternalMove
    d.list.setCurrentRow(2)
    # What a drop does: Kick goes from the bottom to the top, and stays the one shown.
    assert d.list.model().moveRow(QModelIndex(), 2, QModelIndex(), 0)
    assert [o.id for o in d.outputs] == ["kick", "twitch", "youtube"]
    assert d.list.currentRow() == 0 and d.service.currentData() == "kick"
    assert [d.list.item(r).text().split("\n")[0] for r in range(3)] == ["Kick", "Twitch", "YouTube"]
    # Alt+Down from the keyboard: one spot down; at an end it stays put.
    d.move_place(1)
    assert [o.id for o in d.outputs] == ["twitch", "kick", "youtube"] and d.list.currentRow() == 1
    d.move_place(1)
    d.move_place(1)
    assert [o.id for o in d.outputs] == ["twitch", "youtube", "kick"] and d.service.currentData() == "kick"
    assert d.apply_to(col) and [o.id for o in col.outputs] == ["twitch", "youtube", "kick"]
    d.list.setCurrentRow(0)
    d.move_place(1)
    assert d.apply_to(col) and [o.id for o in col.outputs] == ["youtube", "twitch", "kick"]


def test_the_stream_page_can_be_made_a_part_at_a_time(fake_keyring):
    app = QApplication.instance() or QApplication([])  # noqa: F841
    from sprigcast.app.stream_dialog import StreamPage

    col = _collection(*(Output(id=k, type="stream", service=k, canvas="horizontal", key_ref=f"sprigcast/{k}")
                        for k in ("twitch", "youtube", "kick")))
    d = StreamPage(col, later=True)
    # The list is there at once; the form isn't made yet, and what's asked meanwhile is kept for it.
    assert d.list.count() == 3 and not hasattr(d, "service") and not d._built
    d.set_expert(True)
    d.set_live({"youtube"})
    assert d.list.item(1).text().endswith("· Live")
    for _ in range(StreamPage.PARTS):
        d.take_part()
    assert d._built and d.list.currentRow() == 0 and d.service.currentData() == "twitch" and d._expert
    d.take_part()  # nothing left: harmless
    d.select_output("youtube")
    assert not d.remove_button.isEnabled() and not d.live_note.isHidden()
    # Anything that needs the form makes what's left first: a card clicked early, Save pressed early.
    early = StreamPage(col, later=True)
    early.take_part()
    early.list.setCurrentRow(2)
    assert early._built and early.service.currentData() == "kick" and early.list.currentRow() == 2
    saved = StreamPage(col, later=True)
    assert saved.apply_to(col) and [o.id for o in col.outputs] == ["twitch", "youtube", "kick"]
    # Made all at once (the default), it is the same page.
    whole = StreamPage(col)
    assert whole._built and whole.service.currentData() == "twitch"
