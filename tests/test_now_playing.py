"""The Now playing store plugin: the song Windows says is playing, on a small card that fades away when it stops."""

import sys
import threading
import time
import types

import pytest

from sprigcast.control.native import ApiError, NativeApi
from sprigcast.core.scene import SourceConfig
from sprigcast.core.source import create_source, known_kinds

pytest.importorskip("PySide6.QtWidgets")

from test_control_api import _collection  # noqa: E402
from test_store_effects import Host, added, app  # noqa: E402,F401


@pytest.fixture
def np_mod(added):
    added("now_playing")
    return sys.modules["sprigcast_plugin_now_playing"]


class FakeWatcher:
    made: list = []

    def __init__(self, on_track, only_from="", on_problem=None):
        self.on_track, self.only_from, self.on_problem = on_track, only_from, on_problem
        self.running = False
        FakeWatcher.made.append(self)

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


@pytest.fixture
def fake(np_mod, monkeypatch):
    FakeWatcher.made = []
    monkeypatch.setattr(np_mod, "make_watcher", FakeWatcher)
    return np_mod


def _png(colour=(200, 40, 40)):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(64, 36, QImage.Format.Format_RGB32)  # wide, like a video's
    image.fill(QColor(*colour))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data.data())


def test_its_only_there_once_added():
    assert "now_playing" not in known_kinds()
    assert "nowplaying.get" not in NativeApi(Host(_collection())).all_actions()


def test_only_from_and_which_song_shows(np_mod):
    T = np_mod.Track
    assert np_mod.app_matches("Spotify.exe", "") and np_mod.app_matches("Spotify.exe", "spotify")
    assert np_mod.app_matches("Chrome", "Spotify, Chrome") and not np_mod.app_matches("Chrome", "Spotify")
    assert np_mod.app_matches("YouTubeMusic.exe", "YouTube Music")
    spotify, chrome = T("Song", "Band", app="Spotify.exe"), T("Video", app="Chrome", playing=False)
    assert np_mod.pick([spotify, chrome], "Chrome", "") is spotify  # the current one is paused: the one playing
    assert np_mod.pick([spotify, chrome], "Chrome", "Chrome") is chrome  # only Chrome: shown paused (fades out)
    assert np_mod.pick([spotify, chrome], "Spotify.exe", "") is spotify
    assert np_mod.pick([T("", app="x")], "x", "") is None  # nothing named
    assert np_mod.pick([], "", "") is None


def test_lines_and_the_card(np_mod):
    T = np_mod.Track
    track = T("A very long song title that goes on and on and on forever and ever", "Band", "Album", art=_png())
    assert np_mod.lines(track, {}) == (track.title, "Band")
    assert np_mod.lines(track, {"show_album": True}) == (track.title, "Band  ·  Album")
    assert np_mod.lines(T("Single", "Band", "Single"), {"show_album": True}) == ("Single", "Band")
    for settings in ({}, {"show_art": False}, {"card_color": "#00000000"}):
        image = np_mod.paint(track, settings)
        assert (image.width(), image.height()) == (np_mod.W, np_mod.H)
    from PySide6.QtGui import QColor

    with_art = np_mod.paint(track, {})
    assert QColor(with_art.pixel(14 + 22 + 60, 100)).red() > 150  # the cover, at the left
    np_mod.paint(T("Song", art=b"not a picture"), {})  # a broken cover: just no cover


def test_it_listens_only_while_on_screen(fake):
    source = create_source("now_playing", "np", {})
    assert not FakeWatcher.made
    source.start()
    watcher = FakeWatcher.made[-1]
    assert watcher.running
    source.sleep()
    assert not watcher.running
    source.wake()
    assert FakeWatcher.made[-1].running and FakeWatcher.made[-1] is not watcher
    source.set_hidden(True)
    assert not FakeWatcher.made[-1].running
    source.set_hidden(False)
    source.update({**source.settings, "only_from": "Spotify"})  # listens again, for Spotify only
    assert FakeWatcher.made[-1].only_from == "Spotify" and FakeWatcher.made[-1].running
    assert sum(w.running for w in FakeWatcher.made) == 1
    source.stop()
    assert not any(w.running for w in FakeWatcher.made)


def test_it_fades_in_holds_still_and_fades_out_when_paused(fake, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(fake.time, "monotonic", lambda: now[0])
    source = create_source("now_playing", "np", {})
    source.wake()
    blank = source.video_frame()
    assert blank is not None and not blank[..., 3].any()  # nothing playing: nothing shown
    assert source.video_frame() is None
    FakeWatcher.made[-1].on_track(fake.Track("Song", "Band", app="Spotify.exe"))
    half = source.video_frame()
    assert half is not None and not half[..., 3].any()  # the fade starts from nothing
    now[0] += fake.FADE / 2
    middle = source.video_frame()[..., 3].max()
    now[0] += fake.FADE
    full = source.video_frame()[..., 3].max()
    assert 0 < middle < full
    assert source.video_frame() is None  # still: nothing drawn
    FakeWatcher.made[-1].on_track(fake.Track("Song", "Band", app="Spotify.exe", playing=False))
    assert source.video_frame() is None  # paused a moment (skipping a song): it stays
    now[0] += fake.HIDE_AFTER + 0.01
    source.video_frame()
    now[0] += fake.FADE + 0.01
    gone = source.video_frame()
    assert gone is not None and not gone[..., 3].any()
    FakeWatcher.made[-1].on_track(None)  # the app closed
    assert source.video_frame() is None


def test_apps_can_ask_what_is_playing(fake):
    col = _collection()
    col.sources.append(SourceConfig(id="np", kind="now_playing", name="Now playing"))
    api = NativeApi(Host(col))
    assert api.run("nowplaying.get", {}) == {"playing": False, "title": "", "artist": "", "album": "", "app": ""}
    source = create_source("now_playing", "np", {})
    source.wake()
    FakeWatcher.made[-1].on_track(fake.Track("Song", "Band", "Album", "Spotify.exe"))
    assert api.run("nowplaying.get", {}) == {"playing": True, "title": "Song", "artist": "Band", "album": "Album",
                                             "app": "Spotify.exe"}
    source.sleep()
    assert api.run("nowplaying.get", {})["playing"] is False
    with pytest.raises(ApiError) as err:
        NativeApi(Host(_collection())).run("nowplaying.get", {})
    assert err.value.code == "not_found"


def test_adding_it_puts_a_small_card_in_the_bottom_left_of_both_shapes():
    col = _collection()
    col.sources.append(SourceConfig(id="n", kind="now_playing", name="Now playing"))
    part = col.own_sprig(col.scene("chat-h"), "n", (1100, 200), "Now playing", "bottom_left")
    for shape in col.shapes(part):
        canvas = col.canvas(shape.canvas)
        item = shape.items[0]
        margin = min(canvas.width, canvas.height) * 0.035
        assert item.x == pytest.approx(margin)
        bottom = 0.225 * canvas.height if canvas.height > canvas.width else margin  # over TikTok's captions
        assert item.y + 200 * item.scale == pytest.approx(canvas.height - bottom)


# The watcher against a stand-in for Windows' media sessions ------------------------------------------


class FakeSession:
    def __init__(self, app, title, artist="", playing=True):
        self.source_app_user_model_id = app
        self.title, self.artist, self.playing = title, artist, playing
        self.handlers = {}

    async def try_get_media_properties_async(self):
        return types.SimpleNamespace(title=self.title, artist=self.artist, album_title="", thumbnail=None)

    def get_playback_info(self):
        return types.SimpleNamespace(playback_status=4 if self.playing else 5)

    def __getattr__(self, name):
        if name.startswith("add_"):
            return lambda handler: self.handlers.setdefault(name[4:], []).append(handler) or len(self.handlers)
        if name.startswith("remove_"):
            return lambda token: self.handlers.pop(name[7:], None)
        raise AttributeError(name)

    def fire(self, what="media_properties_changed"):
        for handler in list(self.handlers.get(what, [])):
            handler(self, None)


class FakeManager(FakeSession):
    the = None

    def __init__(self, sessions):
        super().__init__("", "")
        self.sessions = sessions
        self.current = sessions[0] if sessions else None

    @classmethod
    async def request_async(cls):
        return cls.the

    def get_sessions(self):
        return list(self.sessions)

    def get_current_session(self):
        return self.current


@pytest.fixture
def windows(monkeypatch):
    control = types.ModuleType("winrt.windows.media.control")
    control.GlobalSystemMediaTransportControlsSessionManager = FakeManager
    runtime = types.ModuleType("winrt.runtime")
    runtime.ApartmentType = types.SimpleNamespace(MULTI_THREADED=1)
    runtime.init_apartment = lambda kind: None
    runtime.uninit_apartment = lambda: None
    for name in ("winrt", "winrt.windows", "winrt.windows.media"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "winrt.runtime", runtime)
    monkeypatch.setitem(sys.modules, "winrt.windows.media.control", control)
    return FakeManager


def _until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_the_watcher_waits_for_windows_to_say_the_song_changed(np_mod, windows):
    spotify = FakeSession("Spotify.exe", "First", "Band")
    windows.the = FakeManager([spotify])
    heard = []
    watcher = np_mod.MediaWatcher(heard.append)
    watcher.start()
    try:
        assert _until(lambda: heard and heard[-1] is not None and heard[-1].title == "First")
        time.sleep(0.3)
        assert len(heard) == 1  # nothing changed: nothing more (and nothing asked meanwhile)
        spotify.title = "Second"
        spotify.fire()
        assert _until(lambda: heard[-1].title == "Second")
        spotify.playing = False
        spotify.fire("playback_info_changed")
        assert _until(lambda: not heard[-1].playing)
        browser = FakeSession("Chrome", "Video")
        windows.the.sessions.append(browser)  # a new app: listened to as well
        windows.the.fire("sessions_changed")
        assert _until(lambda: heard[-1].title == "Video")
        assert "media_properties_changed" in browser.handlers
        windows.the.sessions[:] = []
        windows.the.current = None
        windows.the.fire("sessions_changed")
        assert _until(lambda: heard[-1] is None)
    finally:
        watcher.stop()
    assert watcher._thread is None
    assert _until(lambda: not any(t.name == "now-playing" for t in threading.enumerate()))
    assert spotify.handlers.get("media_properties_changed") is None  # unhooked from Windows


def test_the_watcher_says_when_windows_has_no_media_sessions(np_mod, monkeypatch):
    monkeypatch.setitem(sys.modules, "winrt.runtime", None)  # importing it fails, as on an old Windows
    problems = []
    watcher = np_mod.MediaWatcher(lambda track: None, on_problem=problems.append)
    watcher.start()
    assert _until(lambda: problems == [np_mod.NEEDS])
    watcher.stop()


def test_stopping_straight_after_starting_never_leaves_it_running(np_mod, windows):
    windows.the = FakeManager([FakeSession("Spotify.exe", "Song")])
    for _ in range(20):
        watcher = np_mod.MediaWatcher(lambda track: None)
        watcher.start()
        watcher.stop()
    assert _until(lambda: not any(t.name == "now-playing" for t in threading.enumerate()))


def test_a_long_song_shows_whole(np_mod):
    """PC test 2026-11-17: "Shy Guy - Live - Sp..." was cut off at any size. A title gets smaller to fit, then
    takes two lines; the artist gets smaller; nothing usual is cut short."""
    from PySide6.QtGui import QFont

    room = 960  # a card with no cover
    lines, _ = np_mod.fit_title("Shy Guy - Live - Spotify Singles", QFont(), room, True)
    assert lines == ["Shy Guy - Live - Spotify Singles"]
    long = 'Lose Yourself (From "8 Mile" Soundtrack) - Remastered 2022 Extended Version'
    lines, _ = np_mod.fit_title(long, QFont(), room, True)
    assert len(lines) == 2 and " ".join(lines) == long and "…" not in "".join(lines)
    under, _ = np_mod.fit_line("Eminem, Dr. Dre, 50 Cent, Obie Trice", QFont(), room)
    assert under == "Eminem, Dr. Dre, 50 Cent, Obie Trice"
    card = np_mod.paint(np_mod.Track(long, "Eminem"), dict(np_mod.NowPlayingSource.defaults))
    assert (card.width(), card.height()) == (np_mod.W, np_mod.H)


def test_added_through_the_api_it_starts_readable(np_mod):
    """Put in a bloom with item.add (or OBS's CreateInput), it lands like it does from the + menu: a card a
    seventh of the short side tall, in a free corner, on both shapes; a second one takes another corner."""
    from sprigcast.control.api import ControlApi
    from sprigcast.core.scene import Canvas

    col = _collection()
    col.canvases = [Canvas(id="horizontal", width=1920, height=1080), Canvas(id="vertical", width=1080, height=1920)]
    api = NativeApi(Host(col))
    api.run("source.new", {"name": "Song", "kind": "now_playing"})
    api.run("item.add", {"bloom": "BRB", "source": "Song"})
    api.run("source.new", {"name": "Song 2", "kind": "now_playing"})
    api.run("item.add", {"bloom": "BRB", "source": "Song 2"})
    sprig = next(s for s in col.scenes if s.kind == "part" and s.name == "BRB" and s.shape_of is None)
    for shape in col.shapes(sprig):
        canvas = col.canvas(shape.canvas)
        first, second = shape.items
        assert first.scale == pytest.approx(1080 * 0.14 / np_mod.H)
        assert first.x < canvas.width / 2 and first.y > canvas.height / 2  # bottom left
        w, h = first.drawn_size((np_mod.W, np_mod.H))
        assert first.x + w <= canvas.width and first.y + h <= canvas.height
        a = (first.x, first.y, first.x + w, first.y + h)
        b = (second.x, second.y, second.x + w, second.y + h)
        assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])  # not on top of each other
    obs = ControlApi(Host(col))
    obs.handle("CreateInput", {"sceneName": "Shorts", "inputName": "Song 3", "inputKind": "now_playing"})
    item = col.bloom_sprig(next(s for s in col.scenes if s.id == "solo-v")).items[-1]  # in the bloom's sprig
    assert item.scale == pytest.approx(1080 * 0.14 / np_mod.H) and item.y > 960


def test_text_added_through_the_api_is_the_same_share_of_the_canvas():
    from sprigcast.control.native import NativeApi as Native
    from sprigcast.core.scene import Canvas
    from test_control_api import FakeHost

    col = _collection()
    col.canvases = [Canvas(id="horizontal", width=1280, height=720), Canvas(id="vertical", width=720, height=1280)]
    api = Native(FakeHost(col))
    api.run("source.new", {"name": "Hello", "kind": "text"})
    api.run("item.add", {"bloom": "BRB", "source": "Hello"})
    sprig = next(s for s in col.scenes if s.kind == "part" and s.name == "BRB" and s.shape_of is None)
    assert [shape.items[0].scale for shape in col.shapes(sprig)] == pytest.approx([720 / 1080] * 2)
    api.run("source.new", {"name": "Cam 2", "kind": "color"})
    api.run("item.add", {"bloom": "BRB", "source": "Cam 2"})
    assert all(shape.items[1].scale == 1 and shape.items[1].x == 0 for shape in col.shapes(sprig))  # as in OBS
