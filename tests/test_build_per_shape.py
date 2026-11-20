"""Build lists only the picked shape's blooms and sprigs (user, 2026-11-19): editing vertical shows nothing
made for horizontal, and the other way round. A sprig used on both shows on both."""

from sprigcast.core.scene import PART_KIND, Canvas, Scene, SceneCollection, SceneFolder, SceneItem, SourceConfig

H, V = "horizontal", "vertical"


def _col() -> SceneCollection:
    P = lambda i, part: SourceConfig(id=i, kind=PART_KIND, name=i, settings={"scene": part})  # noqa: E731
    return SceneCollection(
        canvases=[Canvas(id=H, width=1920, height=1080), Canvas(id=V, width=1080, height=1920)],
        sources=[P("p-game", "game"), P("p-tall", "tall"), P("p-shared", "shared"), P("p-in-h", "inner")],
        scenes=[
            Scene(id="h1", name="Gaming", canvas=H, items=[SceneItem(source="p-game"), SceneItem(source="p-shared")]),
            Scene(id="v1", name="Tall", canvas=V, items=[SceneItem(source="p-tall"), SceneItem(source="p-shared")]),
            Scene(id="game", name="Game", canvas=H, kind="part", folder="desk", items=[SceneItem(source="p-in-h")]),
            Scene(id="inner", name="Inner", canvas=H, kind="part", folder="desk"),
            Scene(id="tall", name="Tall chat", canvas=V, kind="part", folder="phone"),
            Scene(id="shared", name="Alerts", canvas=H, kind="part"),
            Scene(id="unused-v", name="New tall", canvas=V, kind="part"),
        ],
        folders=[SceneFolder(id="desk", name="Desk", canvas=H, holds="part"),
                 SceneFolder(id="phone", name="Phone", canvas=H, holds="part"),
                 SceneFolder(id="empty", name="Just made", canvas=H, holds="part")],
    )


def _names(col, canvas):
    return {(f.name if f else None): sorted(p.name for p in parts) for f, parts in col.grouped_parts_on(canvas)}


def test_each_shape_lists_its_own_sprigs():
    col = _col()
    assert _names(col, V) == {None: ["Alerts", "New tall"], "Phone": ["Tall chat"], "Just made": []}
    assert _names(col, H) == {None: ["Alerts"], "Desk": ["Game", "Inner"], "Just made": []}  # Inner: inside Game


def test_a_sprig_shared_by_a_vertical_bloom_shows_there_too():
    col = _col()
    shared = col.scene("shared")
    assert col.part_on(shared, H) and col.part_on(shared, V)
    assert not col.part_on(col.scene("game"), V) and not col.part_on(col.scene("tall"), H)


def test_a_sprig_reads_as_what_it_shows():
    """Sprigs get the icon of what they hold (user, 2026-11-19), so a renamed one still reads at a glance."""
    S = lambda i, kind: SourceConfig(id=i, kind=kind, name=i)  # noqa: E731
    P = lambda i, part: SourceConfig(id=i, kind=PART_KIND, name=i, settings={"scene": part})  # noqa: E731
    col = SceneCollection(
        canvases=[Canvas(id=H, width=1920, height=1080)],
        sources=[S("cam", "camera"), S("mic", "audio_input"), S("bg", "color"), S("chat", "chat"),
                 S("game", "game_capture"), P("p-cam", "cam-sprig")],
        scenes=[Scene(id="cam-sprig", name="Me", canvas=H, kind="part",
                      items=[SceneItem(source="bg"), SceneItem(source="cam"), SceneItem(source="mic")]),
                Scene(id="wrap", name="Wrapped", canvas=H, kind="part", items=[SceneItem(source="p-cam")]),
                Scene(id="mix", name="Mixed", canvas=H, kind="part",
                      items=[SceneItem(source="chat"), SceneItem(source="game")]),
                Scene(id="colour", name="Wash", canvas=H, kind="part", items=[SceneItem(source="bg")]),
                Scene(id="empty", name="Empty", canvas=H, kind="part")])
    kinds = {s.id: col.sprig_kind(s, H) for s in col.parts()}
    assert kinds == {"cam-sprig": "camera", "wrap": "camera", "mix": "scene", "colour": "color", "empty": "empty"}


def test_empty_and_nested_sprigs_get_their_own_icon():
    """A blank sprig shows an empty icon until something goes in; two or more sprigs inside show a nested one
    (user, 2026-11-19)."""
    S = lambda i, kind: SourceConfig(id=i, kind=kind, name=i)  # noqa: E731
    P = lambda i, part: SourceConfig(id=i, kind=PART_KIND, name=i, settings={"scene": part})  # noqa: E731
    col = SceneCollection(
        canvases=[Canvas(id=H, width=1920, height=1080)],
        sources=[S("cam", "camera"), S("chat", "chat"), P("p-cam", "cams"), P("p-chat", "chats"), P("p-none", "blank")],
        scenes=[Scene(id="cams", name="Cam", canvas=H, kind="part", items=[SceneItem(source="cam")]),
                Scene(id="chats", name="Chat", canvas=H, kind="part", items=[SceneItem(source="chat")]),
                Scene(id="blank", name="Blank", canvas=H, kind="part"),
                Scene(id="nest", name="Nest", canvas=H, kind="part",
                      items=[SceneItem(source="p-cam"), SceneItem(source="p-chat")]),
                Scene(id="one", name="One", canvas=H, kind="part", items=[SceneItem(source="p-none")]),
                Scene(id="nest-and-cam", name="Mix", canvas=H, kind="part",
                      items=[SceneItem(source="p-chat"), SceneItem(source="p-cam"), SceneItem(source="cam")])])
    kinds = {s.id: col.sprig_kind(s, H) for s in col.parts()}
    assert kinds["blank"] == "empty" and kinds["one"] == "empty"  # an empty sprig inside shows nothing
    assert kinds["nest"] == "nested"
    assert kinds["nest-and-cam"] == "scene"  # sprigs plus other things: the plain sprig leaf
    col.scene("blank").items.append(SceneItem(source="chat"))
    assert col.sprig_kind(col.scene("blank"), H) == "chat"  # something added: the icon changes


def test_leaves_tags_are_icons():
    from sprigcast.app import theme
    from sprigcast.app.build_stage import tag_badges

    assert tag_badges("") == []
    assert tag_badges("2 hidden here · filters · problem") == [
        ("eye-off", theme.MUTED), ("sliders", theme.MUTED), ("warning", theme.BAD)]
    assert tag_badges("waiting") == [("clock", theme.MUTED)]


def test_a_capture_card_is_called_one(tmp_path, monkeypatch):
    """Adding the Elgato HD60 X from Video capture: titled Video capture, full HD, filling the canvas, its own
    icon (PC, 2026-11-19)."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("CaptureCardTest")
    QSettings().clear()
    from sprigcast.app.build_stage import ROLE_KIND
    from sprigcast.app.camera_dialog import CameraDialog
    from sprigcast.app.main_window import MainWindow
    from sprigcast.sources.camera import CameraDevice

    card, cam = CameraDevice(0, "Elgato HD60 X", "card"), CameraDevice(1, "Facecam MK.2", "cam")
    dialog = CameraDialog([card], {"name": card.name, "path": card.path})
    assert dialog.heading.text() == "Video capture" and dialog.settings()["width"] == 1920
    dialog.deleteLater()
    dialog = CameraDialog([cam], {"name": cam.name, "path": cam.path})
    assert dialog.heading.text() == "Camera" and dialog.settings()["width"] == 1280
    dialog.deleteLater()

    col = SceneCollection(canvases=[Canvas(id=H, width=1920, height=1080)],
                          scenes=[Scene(id="b", name="Main", canvas=H)])
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._pick_bloom("b")
        w._add_camera(card)
        item = w._item()  # in the bloom's sprig
        assert w._kind_and_swatch(col.source(item.source), H)[0] == "capture_card"
        w._place_new_sources({item.source: (1920, 1080)})
        assert (item.x, item.y, round(item.scale * 1920)) == (320, 180, 1280)  # 1280 x 720, centred
    finally:
        w.close()
        QSettings().clear()


def test_new_sources_start_at_720p_but_text(tmp_path, monkeypatch):
    """Every new source starts 1280 x 720 in the middle of a 1080p canvas, kept inside a vertical one; text
    keeps the size its words make (user, 2026-11-19)."""
    import keyring
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    monkeypatch.setattr(keyring, "get_password", lambda *a: None)
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("StartSizeTest")
    QSettings().clear()
    from sprigcast.app.main_window import MainWindow

    col = SceneCollection(canvases=[Canvas(id=H, width=1920, height=1080), Canvas(id=V, width=1080, height=1920)],
                          scenes=[Scene(id="b", name="Main", canvas=H), Scene(id="v", name="Tall", canvas=V)])
    w = MainWindow(col, tmp_path / "scenes.json")
    try:
        w._pick_bloom("b")
        w._add_item(SourceConfig(id="screen", kind="display_capture", name="Screen"))
        w._add_item(SourceConfig(id="words", kind="text", name="Words"))
        w._add_item(SourceConfig(id="mic", kind="audio_input", name="Mic"))
        assert set(w._start_sizes) == {"screen"}
        w._place_new_sources({"screen": (2560, 1440)})
        screen = next(i for s in col.scenes for i in s.items if i.source == "screen")
        assert (screen.x, screen.y, round(2560 * screen.scale)) == (320, 180, 1280) and not w._start_sizes
        w._pick_bloom("v")
        w._add_item(SourceConfig(id="pic", kind="image", name="Pic"))
        w._place_new_sources({"pic": (1920, 1080)})
        pic = next(i for s in col.scenes for i in s.items if i.source == "pic")
        assert round(1920 * pic.scale) == 1080 and pic.x == 0  # as wide as the tall canvas, not past it
    finally:
        w.close()
        QSettings().clear()


def test_leaves_drops_land_anywhere_on_a_tall_sprig_row():
    """A sprig's Leaves row is tall (its contents under it): a drop over its lower half goes after it, its upper
    half before it, not only Qt's few pixels at the edges (PC, 2026-11-20)."""
    from PySide6.QtWidgets import QApplication, QListWidgetItem

    QApplication.instance() or QApplication([])
    from sprigcast.app.build_stage import ROLE_CHILDREN, ROLE_NAME, InsideList

    view = InsideList()
    view.resize(320, 600)
    for name, children in (("Cam", []), ("Sprig", [(0, "a", False), (0, "b", False), (0, "c", False)]), ("Chat", [])):
        row = QListWidgetItem(name)
        row.setData(ROLE_NAME, name)
        row.setData(ROLE_CHILDREN, children)
        view.addItem(row)
    view.show()
    QApplication.processEvents()
    tall = view.visualItemRect(view.item(1))
    assert tall.height() > 60
    assert view._slot_at(tall.top() + tall.height() * 0.3) == 1
    assert view._slot_at(tall.top() + tall.height() * 0.7) == 2
    assert view._slot_at(view.visualItemRect(view.item(2)).bottom() + 20) == 3
    moved = []
    view.moved.connect(lambda a, b: moved.append((a, b)))
    view.setCurrentRow(0)

    class Drop:  # what dropEvent reads
        def __init__(self, y):
            from PySide6.QtCore import QMimeData, QPointF
            self._pos, self._mime = QPointF(10, y), QMimeData()

        def mimeData(self):
            return self._mime

        def position(self):
            return self._pos

        def setDropAction(self, _a):
            pass

        def accept(self):
            pass

    view.dropEvent(Drop(tall.top() + tall.height() * 0.7))  # Cam dropped on the lower half of the sprig
    assert moved == [(0, 1)]
