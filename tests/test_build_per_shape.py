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
