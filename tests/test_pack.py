"""Garden packs: a garden with its files, moved to another PC (core/pack.py)."""

import zipfile
from importlib import resources
from pathlib import Path

import pytest

from sprigcast.core import pack
from sprigcast.core.gardens import Shelf, ShelfError, missing_files
from sprigcast.core.scene import SceneCollection


def _starter() -> SceneCollection:
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    col = SceneCollection.from_json(text)
    col.wrap_blooms()
    return col


def _garden_with_files(home):
    col = _starter()
    (home / "art").mkdir()
    logo = home / "art" / "logo.png"
    logo.write_bytes(b"png" * 100)
    page = home / "alerts" / "alerts.html"
    page.parent.mkdir()
    page.write_text("<img src='pic/a.png'>", encoding="utf-8")
    (page.parent / "pic").mkdir()
    (page.parent / "pic" / "a.png").write_bytes(b"a")
    slides = home / "slides"
    slides.mkdir()
    for name in ("1.png", "2.jpg"):
        (slides / name).write_bytes(b"s")
    sources = col.sources
    sources[0].kind, sources[0].settings = "image", {"path": str(logo)}
    sources[1].kind, sources[1].settings = "browser", {"file": str(page), "url": ""}
    sources[2].kind, sources[2].settings = "slide_show", {"folder": str(slides)}
    sources[3].kind, sources[3].settings = "image", {"path": "art:sloth-happy.png"}
    col.transition.stinger.path = str(logo)  # the same file twice: packed once
    col.transition.wipe_image = str(home / "gone.png")
    return col


def test_pack_moves_a_garden_with_its_files(tmp_path):
    old, new, media = tmp_path / "old", tmp_path / "new", tmp_path / "Media"
    old.mkdir()
    new.mkdir()
    col = _garden_with_files(old)
    target = tmp_path / "out" / f"Gaming{pack.PACK_SUFFIX}"
    left_out = pack.export_pack(col, target)
    assert left_out == [str(old / "gone.png")]
    assert col.sources[0].settings["path"] == str(old / "art" / "logo.png")  # the garden on screen is untouched
    names = zipfile.ZipFile(target).namelist()
    assert sum(n.endswith("logo.png") for n in names) == 1 and any(n.endswith("pic/a.png") for n in names)

    garden = pack.read_pack(target, media / "Gaming")
    s = garden.sources
    assert Path(s[0].settings["path"]).read_bytes() == b"png" * 100
    page = s[1].settings["file"]
    assert page.endswith("alerts.html") and (media / "Gaming").resolve() in Path(page).parents
    assert (Path(page).parent / "pic" / "a.png").exists()
    assert sorted(p.name for p in Path(s[2].settings["folder"]).iterdir()) == ["1.png", "2.jpg"]
    assert s[3].settings["path"] == "art:sloth-happy.png"
    assert garden.transition.stinger.path == s[0].settings["path"]
    assert missing_files(garden) == [str(old / "gone.png")]


def test_unpacking_never_writes_outside_its_folder(tmp_path):
    bad = tmp_path / f"bad{pack.PACK_SUFFIX}"
    col = _starter()
    col.sources[0].kind, col.sources[0].settings = "image", {"path": "pack:1/../../../evil.png"}
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("garden.json", col.model_dump_json())
        zf.writestr("files/1/../../../evil.png", b"x")
        zf.writestr("/abs.png", b"x")
        zf.writestr("files/2/ok.png", b"x")
    garden = pack.read_pack(bad, tmp_path / "media" / "Bad")
    assert not (tmp_path / "evil.png").exists() and not list(tmp_path.glob("**/evil.png"))
    assert (tmp_path / "media" / "Bad" / "2" / "ok.png").exists()
    assert garden.sources[0].settings["path"] == ""


def test_broken_packs_are_refused(tmp_path):
    junk = tmp_path / f"junk{pack.PACK_SUFFIX}"
    junk.write_bytes(b"not a zip")
    with pytest.raises(pack.PackError):
        pack.read_pack(junk, tmp_path / "m")
    nogarden = tmp_path / f"empty{pack.PACK_SUFFIX}"
    with zipfile.ZipFile(nogarden, "w") as zf:
        zf.writestr("files/1/a.png", b"x")
    with pytest.raises(pack.PackError):
        pack.read_pack(nogarden, tmp_path / "m")


def test_shelf_packs_and_unpacks_into_a_new_folder(tmp_path):
    config, media = tmp_path / "config", tmp_path / "Media"
    config.mkdir()
    col = _garden_with_files(tmp_path)
    col.save(config / "scenes.json")
    shelf = Shelf(config)
    shelf.sync_path(col)
    with pytest.raises(ShelfError):
        shelf.export_pack("Main", config / f"x{pack.PACK_SUFFIX}", col)
    target = tmp_path / f"Gaming{pack.PACK_SUFFIX}"
    shelf.export_pack("Main", target, col)
    first = shelf.import_pack(target, media)
    second = shelf.import_pack(target, media)
    assert (first.name, second.name) == ("Gaming", "Gaming 2")
    assert (media / "Gaming").is_dir() and (media / "Gaming 2").is_dir()
    assert str(media / "Gaming 2") in shelf.load_garden(second).sources[0].settings["path"]
