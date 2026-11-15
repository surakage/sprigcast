"""Scene collections from OBS, Streamlabs and Meld turned into gardens: core/convert.py."""

import json
from importlib import resources

import pytest

from sprigcast.core.convert import (ConvertError, convert, detect, divider_name, from_obs, obs_color, read_json,
                                    scene_collection_files, strip_vertical)
from sprigcast.core.scene import PART_KIND, SceneCollection

MAIN = "6c69626f-6273-4c00-9d88-c5136d61696e"
VERT = "a4820b7e-6dfd-4d4f-93a7-e8ba539d6245"


def _like() -> SceneCollection:
    text = resources.files("sprigcast.data").joinpath("default_scenes.json").read_text(encoding="utf-8")
    col = SceneCollection.from_json(text)
    col.wrap_blooms()
    return col


def _item(uuid, x=0, y=0, sx=1.0, sy=1.0, align=5, **more):
    return {"name": "", "source_uuid": uuid, "visible": True, "locked": False, "rot": 0.0, "align": align,
            "bounds_type": 0, "bounds_align": 0, "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0,
            "pos": {"x": x, "y": y}, "scale": {"x": sx, "y": sy}, "bounds": {"x": 0, "y": 0}, **more}


def _scene(name, uuid, items, canvas=MAIN, **more):
    return {"id": "scene", "name": name, "uuid": uuid, "canvas_uuid": canvas, "settings": {"items": items}, **more}


def obs_file():
    """An OBS 31 collection: dividers, a main scene, a nested overlay, a group and an Aitum vertical canvas."""
    sources = [
        {"id": "color_source_v3", "name": "Backdrop", "uuid": "c1", "settings": {"color": 0xFF3A5C0F, "width": 1920, "height": 1080}},
        {"id": "browser_source", "name": "Chat", "uuid": "b1", "volume": 0.5,
         "settings": {"url": "https://example.com/chat", "width": 400, "height": 600, "shutdown": True}},
        {"id": "image_source", "name": "Logo", "uuid": "i1", "settings": {"file": "C:/gone/logo.png"}},
        {"id": "dshow_input", "name": "Cam", "uuid": "d1",
         "settings": {"video_device_id": "Elgato Facecam MK.2:\\\\?\\usb#vid_0fd9", "res_type": 1, "resolution": "1280x720"}},
        {"id": "wasapi_input_capture", "name": "[AIC] Mic", "uuid": "m1", "volume": 0.1, "muted": True,
         "settings": {"device_id": "{0.0.1}.{abc}"},
         "filters": [{"id": "compressor_filter", "name": "Comp", "settings": {"ratio": 4, "threshold": -20}},
                     {"id": "obs_composite_blur", "name": "Fancy blur", "settings": {}}]},
        {"id": "game_capture", "name": "Game", "uuid": "g1", "settings": {"capture_mode": "window", "window": "Elden Ring:Cls:eldenring.exe"}},
        {"id": "vlc_source", "name": "Playlist", "uuid": "v1", "settings": {}},
        _scene("--- Main Scenes ---", "div1", []),
        _scene("Just Chatting", "s1", [_item("c1"), _item("o1"), _item("grp"), _item("d1", 960, 540, 0.5, 0.5, align=0)],
               private_settings={"transition": "Leaf Stinger", "transition_duration": 900}),
        _scene("BRB", "s2", [_item("c1"), _item("i1", 100, 100, align=0)]),
        _scene("--- Overlays ---", "div2", []),
        _scene("Overlay", "o1", [_item("b1", 1500, 400, bounds_type=2, bounds={"x": 300, "y": 500})]),
        _scene("Spare overlay", "o2", [_item("b1")]),
        _scene("Just Chatting (Vertical)", "s1v", [_item("o1v"), _item("c1", scale_ref={"x": 1080, "y": 1920})], canvas=VERT),
        _scene("Overlay (Vertical)", "o1v", [_item("b1", 100, 100)], canvas=VERT),
    ]
    group = {"id": "group", "name": "Corner", "uuid": "grp",
             "settings": {"cx": 400, "cy": 200, "items": [_item("g1", 0, 0, 0.2, 0.2)]}}
    return {"name": "Streaming", "sources": sources, "groups": [group],
            "scene_order": [{"name": n} for n in ("--- Main Scenes ---", "Just Chatting", "BRB", "--- Overlays ---",
                                                  "Overlay", "Spare overlay")],
            "current_scene": "BRB", "current_program_scene": "BRB", "resolution": {"x": 1920, "y": 1080},
            "canvases": [{"info": {"name": "Aitum Vertical", "uuid": VERT}}],
            "current_transition": "Fade", "transition_duration": 400,
            "transitions": [{"id": "obs_stinger_transition", "name": "Leaf Stinger",
                             "settings": {"path": "C:/stingers/leaf.webm", "transition_point": 650}}]}


def test_detects_each_app():
    assert detect(obs_file()) == "obs"
    assert detect(_like().model_dump(mode="json")) == "sprigcast"
    assert detect({"nodeType": "RootNode", "sources": {"nodeType": "SourcesNode", "items": []}}) == "streamlabs"
    assert detect({"items": {"a1": {"type": "scene", "name": "S"}}}) == "meld"
    assert detect({"hello": 1}) == "" and detect([1, 2]) == ""
    with pytest.raises(ConvertError):
        convert({"hello": 1}, _like())


def test_obs_blooms_sprigs_folders_and_vertical_twins():
    col, notes = from_obs(obs_file(), _like(), size_of=lambda kind, s: (200, 100))
    shows = {(s.name, s.canvas): s for s in col.scenes if s.kind == "show"}
    assert set(shows) == {("Just Chatting", "horizontal"), ("BRB", "horizontal"), ("Just Chatting", "vertical")}
    assert shows[("Just Chatting", "vertical")].linked_to == shows[("Just Chatting", "horizontal")].id
    sprigs = {s.name for s in col.parts()}
    assert {"Overlay", "Spare overlay", "Overlay (Vertical)", "Corner"} <= sprigs  # spare ones follow their folder
    folders = {(f.name, f.holds) for f in col.folders}
    assert ("Main Scenes", "show") in folders and ("Overlays", "part") in folders
    assert all(i.source and col.source(i.source).kind == PART_KIND for s in shows.values() for i in s.items)
    assert col.scene(col.active_scenes["horizontal"]).name == "BRB"
    rule = col.transition_rules[0]
    assert col.scene(rule.to_scene).name == "Just Chatting" and rule.transition.stinger.cut_ms == 650
    assert col.transition.kind == "fade" and col.transition.duration_ms == 400
    assert any("Playlist" in n for n in notes) and any("Fancy blur" in n for n in notes)
    assert any("Mic" in n and "sound device" in n for n in notes)
    SceneCollection.model_validate(col.model_dump())


def test_obs_positions_from_the_anchor_point():
    col, _ = from_obs(obs_file(), _like(), size_of=lambda kind, s: (200, 100))
    overlay = next(s for s in col.parts() if s.name == "Overlay")
    chat = overlay.items[0]
    assert (chat.x, chat.y, chat.bounds, chat.bounds_size) == (1500, 400, "fit", (300, 500))
    jc = next(s for s in col.parts() if s.name == "Just Chatting")  # the bloom's own sprig
    cam = next(i for i in jc.items if col.source(i.source).kind == "camera")
    assert cam.align == "center" and (cam.x, cam.y) == (960 - 320, 540 - 180)  # 1280x720 at half size
    brb = next(s for s in col.parts() if s.name == "BRB")
    logo = next(i for i in brb.items if col.source(i.source).kind == "image")
    assert (logo.x, logo.y) == (0, 50)  # 200x100 centred on 100,100


def test_obs_sources_and_sound():
    col, _ = from_obs(obs_file(), _like(), audio_devices=["Mic (Elgato Wave:3)"])
    by_name = {s.name: s for s in col.sources}
    assert by_name["Backdrop"].settings["color"] == "#0f5c3aff"
    assert by_name["Chat"].settings["keep_running"] is False and by_name["Chat"].settings["volume_db"] == -6.0
    assert by_name["Cam"].settings["name"] == "Elgato Facecam MK.2"
    assert by_name["Game"].settings == {"mode": "window", "title": "Elden Ring", "process": "eldenring.exe"}
    mic = by_name["[AIC] Mic"]
    assert mic.settings["device"] == "Mic (Elgato Wave:3)" and mic.settings["muted"] is True
    assert mic.settings["volume_db"] == -20.0 and [f.kind for f in mic.filters] == ["compressor"]


def test_obs_bigger_canvas_scales_down():
    data = obs_file()
    data["resolution"] = {"x": 2560, "y": 1440}
    col, _ = from_obs(data, _like(), size_of=lambda kind, s: (200, 100))
    overlay = next(s for s in col.parts() if s.name == "Overlay")
    assert overlay.items[0].x == pytest.approx(1125) and overlay.items[0].bounds_size == pytest.approx((225, 375))


def test_streamlabs_collection():
    data = {"nodeType": "RootNode", "schemaVersion": 5, "baseResolution": {"baseWidth": 1920, "baseHeight": 1080},
            "sources": {"nodeType": "SourcesNode", "items": [
                {"id": "img", "name": "Frame", "type": "image_source", "settings": {"file": "C:/f.png"}, "volume": 1,
                 "filters": {"items": [{"type": "gain_filter", "name": "Boost", "settings": {"db": 3}}]}},
                {"id": "web", "name": "Alert box", "type": "browser_source", "propertiesManager": "widget",
                 "settings": {"url": "https://streamlabs.com/alert-box/x", "width": 800, "height": 600}}]},
            "scenes": {"nodeType": "ScenesNode", "items": [
                {"id": "sc1", "name": "Live", "active": True, "sceneItems": {"items": [
                    {"id": "a", "sceneNodeType": "item", "sourceId": "img", "x": 10, "y": 20, "scaleX": 2, "scaleY": 2},
                    {"id": "f", "sceneNodeType": "folder", "name": "Stuff", "childrenIds": ["b"]},
                    {"id": "b", "sceneNodeType": "item", "sourceId": "web", "x": 0, "y": 0, "scaleX": 1, "scaleY": 1},
                    {"id": "c", "sceneNodeType": "item", "sourceId": "web", "x": 5, "y": 6, "scaleX": 1, "scaleY": 1,
                     "display": "vertical"}]}}]}}
    col, notes = convert(data, _like(), size_of=lambda kind, s: (100, 100))
    shows = {(s.name, s.canvas) for s in col.scenes if s.kind == "show"}
    assert shows == {("Live", "horizontal"), ("Live", "vertical")}
    live = next(s for s in col.parts() if s.name == "Live")
    assert [(i.x, i.y, i.scale) for i in live.items][0] == (10, 20, 2)
    assert any("widget" in n for n in notes)


def test_meld_session():
    data = {"session": {"items": {
        "s1": {"type": "scene", "name": "Main", "index": 0, "current": True},
        "l1": {"type": "layer", "parent": "s1", "index": 0, "name": "Overlay", "x": 10, "y": 10, "width": 300,
               "height": 200, "url": "https://example.com"},
        "l2": {"type": "layer", "parent": "s1", "index": 1, "name": "Bg", "x": 0, "y": 0, "width": 1920,
               "height": 1080, "source": "C:/bg.png"},
        "l3": {"type": "layer", "parent": "s1", "index": 2, "name": "Webcam", "x": 0, "y": 0}}}}
    col, notes = convert(data, _like())
    main = next(s for s in col.parts() if s.name == "Main")
    assert [col.source(i.source).name for i in main.items] == ["Bg", "Overlay"]  # Meld's top layer is first
    assert main.items[1].bounds_size == (300, 200)
    assert any("Webcam" in n for n in notes)


def test_helpers(tmp_path):
    assert divider_name("🗄 Main Scenes 🗄") == "Main Scenes" and divider_name("--- Alerts ---") == "Alerts"
    assert divider_name("Gameplay") is None and divider_name("🔴 Gameplay") is None
    assert strip_vertical("Chat (Vertical)") == "Chat" and strip_vertical("Chat [vertical]") == "Chat"
    assert obs_color(0xFF0000FF) == "#ff0000ff"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConvertError):
        read_json(bad)
    good = tmp_path / "bom.json"
    good.write_bytes(b"\xef\xbb\xbf" + json.dumps(obs_file()).encode())
    assert detect(read_json(good)) == "obs"
    (tmp_path / "obs-studio" / "basic" / "scenes").mkdir(parents=True)
    (tmp_path / "obs-studio" / "basic" / "scenes" / "Streaming.json").write_text("{}", encoding="utf-8")
    (tmp_path / "obs-studio" / "basic" / "scenes" / "Streaming.json.bak").write_text("{}", encoding="utf-8")
    assert [p.name for p in scene_collection_files(tmp_path)["obs"]] == ["Streaming.json"]
