"""The tour with Pip: offered once at the first start, lights up each spot, waits on its "try it" stops."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture()
def window(tmp_path, monkeypatch):
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("TourTest")
    QSettings().clear()
    from sprigcast.app.main import load_collection
    from sprigcast.app.main_window import MainWindow

    w = MainWindow(load_collection(tmp_path / "scenes.json"), tmp_path / "scenes.json")
    w.resize(1366, 728)
    w.show()
    app.processEvents()
    yield w
    w.close()
    QSettings().clear()


def _settle():
    from PySide6.QtWidgets import QApplication

    for _ in range(5):
        QApplication.processEvents()


def test_every_stop_says_where_it_is_and_the_estimate_counts_try_it_stops_longer():
    from sprigcast.app import tour

    every = tour.stops()
    assert {s.area for s in every} >= {"Live", "Build", "Settings", "Menus"}
    tries = [s for s in every if s.done is not None]
    assert len(tries) == 3
    assert tour.minutes(every) == round(((len(every) - 3) * tour.PLAIN_S + 3 * tour.TRY_S) / 60)
    assert all(s.text and s.title for s in every)


def test_the_tour_is_offered_once_and_never_again(window, monkeypatch):
    from sprigcast.app import tour

    asked = []
    monkeypatch.setattr(tour, "ask", lambda w: asked.append(True) or False)
    tour.offer(window)
    tour.offer(window)
    assert asked == [True]
    assert any(a.text() == "Take the tour with Pip" for m in window.menuBar().actions() if m.menu()
               for a in m.menu().actions())


def test_a_stop_lights_up_its_spot_and_a_try_it_stop_waits(window):
    from sprigcast.app import tour

    stops = [s for s in tour.stops() if s.settings is None]
    t = tour.start(window)
    t.stops = stops
    t._go(next(i for i, s in enumerate(stops) if s.title == "Go live"))
    _settle()
    live = window.live_side.live
    spot = t.shade.hole
    assert window.mode == "live" and spot is not None
    assert spot.contains(live.mapTo(window, live.rect().center()))
    assert window.rect().contains(t.card.geometry())  # the card stays inside the window
    assert t.card.next.isEnabled() and not t.card.skip.isVisible()

    at = next(i for i, s in enumerate(stops) if s.title == "Add your camera")
    t._go(at)
    _settle()
    assert window.mode == "build" and not t.card.next.isEnabled() and t.card.skip.isVisible()
    scene = window.collection.scene(window.selected_scene_id)
    from sprigcast.core.scene import SceneItem, SourceConfig

    window.collection.sources.append(SourceConfig(id="cam-new", kind="camera", name="Cam"))
    scene.items.append(SceneItem(source="cam-new"))
    t._look()
    assert t.card.next.isEnabled()  # done: Next lights up
    t.card.next.click()
    assert t.at == at + 1

    t.end()
    _settle()
    assert t.card is None and t.shade is None  # nothing left over the window


def test_the_menus_are_lit_up_in_the_menu_bar(window):
    from sprigcast.app import tour

    t = tour.Tour(window, [s for s in tour.stops() if s.area == "Menus"])
    t.start()
    _settle()
    bar = window.menuBar()
    assert t.shade.hole is not None and t.shade.hole.top() <= bar.height()
    t.end()


def test_the_phone_and_search_stops_show_all_their_words(window):
    """Phone Camera and Android Screen are part of the basic set-up; each stop's card is as tall as its words
    (Your phone's last line was cut off)."""
    from sprigcast.app import tour

    stops = [s for s in tour.stops() if s.settings is None]
    titles = [s.title for s in stops]
    assert "Your phone" in titles and "Search" in titles
    t = tour.start(window)
    t.stops = stops
    for title in ("Your phone", "Blooms and sprigs", "Search"):
        t._go(titles.index(title))
        _settle()
        text = t.card.text
        assert text.height() >= text.heightForWidth(text.width())
    t.end()
    _settle()
