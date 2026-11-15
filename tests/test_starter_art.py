"""Starter blooms get their sloth picture, which ships with SprigCast ("art:" paths)."""

from sprigcast.core.scene import Canvas, SceneCollection
from sprigcast.sources.image import ART_FOLDER, ImageSource, resolve


def test_art_paths_resolve_inside_the_art_folder():
    assert resolve("art:sloth-ending.png") == ART_FOLDER / "sloth-ending.png"
    assert resolve("art:../../secret.txt") == ART_FOLDER / "secret.txt"  # never outside the folder
    assert str(resolve("C:/pics/a.png")).replace("\\", "/").endswith("pics/a.png")


def test_every_starter_picture_ships():
    from sprigcast.app.starter import ART

    for name in ART.values():
        assert (ART_FOLDER / name).is_file(), name
    for name in ("sloth-trouble.png", "sloth-happy.png"):
        assert (ART_FOLDER / name).is_file(), name


def test_starter_blooms_get_one_sharp_sloth_each():
    from sprigcast.app.starter import ART, StarterChoice, build_starter

    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080),
                                    Canvas(id="vertical", width=1080, height=1920, enabled=True)],
                          sources=[], scenes=[])
    build_starter(col, StarterChoice(mic=None, desktop=None))
    pictures = {c.id: c for c in col.sources if c.kind == "image"}
    assert sorted(c.settings["path"] for c in pictures.values()) == sorted("art:" + n for n in ART.values())
    for scene in col.scenes:
        if scene.kind != "show":
            continue
        canvas = next(c for c in col.canvases if c.id == scene.canvas)
        arts = [i for i in col.shape_for(col.bloom_sprig(scene), scene.canvas).items if i.source in pictures]
        base = scene.name.replace(" (vertical)", "")
        assert len(arts) == (1 if base in ART else 0), scene.name
        for item in arts:
            source = ImageSource("x", pictures[item.source].settings)
            w, h = source.size
            assert item.scale <= 1.0  # never blown up past the picture
            assert 0 <= item.x and item.x + w * item.scale <= canvas.width
            assert 0 <= item.y and item.y + h * item.scale <= canvas.height
