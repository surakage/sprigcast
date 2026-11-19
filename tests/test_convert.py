"""Scene collections from OBS, Streamlabs and Meld turned into gardens: core/convert.py."""

import json
from importlib import resources

import pytest

from sprigcast.core.convert import (
    ConvertError,
    convert,
    detect,
    divider_name,
    from_obs,
    obs_color,
    read_json,
    scene_collection_files,
    strip_vertical,
)
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
                     {"id": "shader_filter", "name": "Fancy shader", "settings": {}}]},
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
    assert detect(meld_file()) == "meld"
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
    assert any("Playlist" in n for n in notes) and any("Fancy shader" in n for n in notes)
    assert any("[AIC] Mic: uses your" in n for n in notes)  # OBS's only mic, its device not here: your Mic
    assert not any(c.name == "[AIC] Mic" for c in col.sources)
    mic = next(c for c in col.sources if c.id in col.global_device_ids() and c.kind == "audio_input")
    assert mic.settings["muted"] is True and [f.kind for f in mic.filters] == ["compressor"]
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
    data["baseResolutions"] = {"horizontal": {"baseWidth": 1920, "baseHeight": 1080},
                               "vertical": {"baseWidth": 720, "baseHeight": 1280}}
    for relative, expected in ((None, (7.5, 9.0, 1.5)), (True, (5, 6, 1))):
        # Newer Streamlabs (relativeCoordinates) keeps a 720x1280 canvas's items as if it were 1080x1920.
        data["relativeCoordinates"] = relative
        col, _ = convert(data, _like(), size_of=lambda kind, s: (100, 100))
        item = next(s for s in col.scenes if s.kind == "part" and s.canvas == "vertical").items[0]
        assert (item.x, item.y, item.scale) == expected


SPARK = {"pluginName": "MeldStudio", "sceneObjectSourceName": "MeldStudio/Spark Element"}


def _layer(name, x, y, w, h, **more):
    return {"id": f"L-{name}-{x}", "name": name, "x": x, "y": y, "width": w, "height": h, "visible": True,
            "opacity": 1.0, "rotation": 0.0, "scaleX": 1.0, "scaleY": 1.0, "childItems": [], "meta": SPARK, **more}


def meld_file():
    """A Meld Studio Session.json, cut down from a real one: pages, a text placeholder in a group, tracks."""
    alert = "https://elements.meldstudio.co/x/stream-alert.html"
    text = _layer("Replace", 653, 522, 203, 36, text="Game capture\n", pixelSize=32, fontFamily="Arial",
                  foregroundColor="#ff00ff00", meta={"sceneObjectSourceName": "MeldStudio/Text"})
    group = _layer("Replace with: Game", 653, 522, 203, 36, expanded=False, childItems=[text],
                   meta={"sceneObjectSourceName": "MeldStudio/Group"})
    return {"session": {
        "currentScene": "G1",
        "scenes": [
            {"id": "S1", "name": "Starting Soon", "width": 1920, "height": 1080, "childItems": [
                _layer("Stream Alert", 723, 12, 531, 225, url=alert,
                       audioTrack={"enabled": True, "gain": 0.5, "muted": False}),
                _layer("Background", 0, 0, 1920, 1080, url="https://elements.meldstudio.co/x/bg.html")]},
            {"id": "G1", "name": "Gaming", "width": 1920, "height": 1080, "childItems": [
                _layer("Stream Alert", 723, 12, 531, 225, url=alert), group,
                _layer("Logo", 10, 10, 100, 100, meta={"sceneObjectSourceName": "MeldStudio/Hologram"})]}],
        "verticalScenes": [
            {"id": "V1", "name": "Gaming", "linkedSceneId": "G1", "width": 1080, "height": 1920, "childItems": [
                _layer("Stream Alert", 242, 43, 595, 197, url=alert)]}],
        "verticalCanvasEnabled": True,
        "audioTracks": [
            {"id": "MIC", "name": "Mic", "type": 1, "isInputDevice": True, "uniqueId": "default", "gain": 1.0,
             "deviceName": "Default Input Device ( Microphone (Voicemod) )",
             "associatedAudioNodes": [{"name": "Voice Clarity", "nodeType": 10, "bypass": False}]},
            {"id": "MUS", "name": "Music", "type": 0, "gain": 1.0, "associatedAudioNodes": [
                {"name": "Compressor", "nodeType": 4, "ratio": 12.0, "threshold": -24.0, "attack": 0.003,
                 "release": 0.25, "sidechainTrackId": "MIC", "bypass": False}]}],
        "sceneTransition": {"duration": 1000.0, "effects": [{"enabled": False, "type": "Move"}]}}}


def test_meld_session():
    col, notes = convert(meld_file(), _like())
    shows = {(s.name, s.canvas) for s in col.scenes if s.kind == "show"}
    assert shows == {("Starting Soon", "horizontal"), ("Gaming", "horizontal"), ("Gaming", "vertical")}
    gaming = next(s for s in col.parts() if s.name == "Gaming" and s.canvas == "horizontal")
    names = [(col.source(i.source).name if col.source(i.source) else col.scene(i.source).name) for i in gaming.items]
    assert names == ["Replace with: Game", "Stream Alert"]  # Meld's top layer is first; OBS's last
    alert = gaming.items[1]
    assert (alert.x, alert.y, alert.align) == (723, 12, "top_left")
    page = col.source(alert.source)
    assert page.kind == "browser" and (page.settings["width"], page.settings["height"]) == (531, 225)
    assert page.settings["audio_to_mixer"] is True and page.settings["volume_db"] == -6.0
    soon = next(s for s in col.parts() if s.name == "Starting Soon")
    assert soon.items[1].source == alert.source  # the same page at the same size is one source
    vertical = next(s for s in col.scenes if s.kind == "part" and s.name == "Gaming" and s.canvas == "vertical")
    assert vertical.shape_of == gaming.id
    assert col.source(vertical.items[0].source).name == "Stream Alert (Vertical)"
    group = next(s for s in col.parts() if s.name == "Replace with: Game")
    text = col.source(group.items[0].source)
    assert text.kind == "text" and text.settings["text"] == "Game capture" and text.settings["color"] == "#00ff00ff"
    mic = next(s for s in col.sources if s.name == "Mic")
    assert [f.kind for f in mic.filters] == ["noise_suppression"]
    music = next(s for s in col.sources if s.name == "Music")
    duck = music.filters[0]
    assert duck.kind == "ducking" and duck.settings["sound_id"] == mic.id and duck.settings["threshold_db"] == -24
    assert col.transition.kind == "fade" and col.transition.duration_ms == 1000
    assert any("Hologram" in n for n in notes) and any("placeholder" in n for n in notes)


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


def test_scenes_without_dividers_are_sorted_by_what_they_hold():
    data = obs_file()
    data["scene_order"] = [o for o in data["scene_order"] if not o["name"].startswith("---")]
    data["sources"] = [s for s in data["sources"] if not s["name"].startswith("---")]
    data["sources"].append(_scene("Webcam", "w1", [_item("d1")]))
    data["sources"].append(_scene("Music", "mu", [_item("m1")]))
    data["sources"].append(_scene("Live", "lv", [_item("w1"), _item("mu")]))
    col, _ = from_obs(data, _like(), size_of=lambda kind, s: (200, 100))
    where = {s.name: col.folder(s.folder).name for s in col.scenes if s.folder and s.shape_of is None}
    assert where["Overlay"] == "Browser Scenes" and where["Webcam"] == "Capture Scenes"
    assert where["Music"] == "Audio Scenes" and where["Corner"] == "Capture Scenes"
    shows = [s for s in col.scenes if s.kind == "show" and s.shape_of is None]
    assert all(col.folder(s.folder).name == "Main Scenes" for s in shows)


def test_sounds_are_not_doubled_and_play_in_their_blooms():
    data = obs_file()
    data["sources"] += [
        {"id": "wasapi_output_capture", "name": "Desktop", "uuid": "a1", "settings": {"device_id": "default"}},
        {"id": "wasapi_output_capture", "name": "[AOC] Voice Chat", "uuid": "a2", "settings": {"device_id": "{x}"}},
        {"id": "wasapi_output_capture", "name": "[AOC] Music", "uuid": "a3", "settings": {"device_id": "{y}"}},
    ]
    brb = next(s for s in data["sources"] if s.get("name") == "BRB")
    brb["settings"]["items"].append(_item("a3"))
    like = _like()
    col, notes = from_obs(data, like, audio_devices=["Voice Chat (Elgato Virtual Audio)"])
    names = {c.name: c for c in col.sources}
    assert "Desktop" not in names  # OBS's default desktop sound is your Desktop audio
    assert names["[AOC] Voice Chat"].settings["device"] == "Voice Chat (Elgato Virtual Audio)"
    assert names["[AOC] Music"].settings["muted"] is True  # its device isn't here: quiet, not doubled
    bloom = next(s for s in col.scenes if s.kind == "show" and s.name == "BRB")
    assert names["[AOC] Music"].settings["blooms"] == [bloom.id]
    assert "blooms" not in names["[AOC] Voice Chat"].settings  # in no scene: plays everywhere


def test_obs_hotkeys_and_text():
    data = obs_file()
    brb = next(s for s in data["sources"] if s.get("name") == "BRB")
    brb["hotkeys"] = {"OBSBasic.SelectScene": [{"key": "OBS_KEY_F2", "control": True, "alt": True}]}
    data["sources"].append({"id": "text_gdiplus_v3", "name": "Title", "uuid": "t1",
                            "settings": {"text": "Hello", "font": {"face": "Segoe UI", "size": 48}}})
    brb["settings"]["items"].append(_item("t1", 960, 540, align=0))
    col, _ = from_obs(data, _like())
    bloom = next(s for s in col.scenes if s.kind == "show" and s.name == "BRB")
    assert col.hotkeys[f"scene:{bloom.id}"] == "Ctrl+Alt+F2"
    title = next(c for c in col.sources if c.name == "Title")
    assert title.settings["font"] == "segoeui.ttf"
    sprig = next(s for s in col.parts() if s.name == "BRB")
    item = next(i for i in sprig.items if i.source == title.id)
    assert item.x < 960 and item.y < 540  # the text's real size was used to centre it


def test_composite_blur_pixelate_comes_over():
    data = obs_file()
    cam = next(s for s in data["sources"] if s["name"] == "Cam")
    cam["filters"] = [{"id": "obs_composite_blur", "name": "THSV Pixelate", "enabled": False,
                       "settings": {"blur_algorithm": 4, "radius": 40}},
                      {"id": "obs_composite_blur", "name": "Soft", "settings": {"blur_algorithm": 0, "radius": 90}}]
    garden, notes = convert(data, _like())
    cam = next(s for s in garden.sources if s.name == "Cam")
    assert [(f.kind, f.settings, f.enabled) for f in cam.filters] == [
        ("blur", {"mode": "pixelate", "size": 40.0}, False), ("blur", {"mode": "blur", "size": 64.0}, True)]
    assert not any("THSV" in n for n in notes)


def _slot(name, kind, x=0.0, y=0.0, s=1.0, display="horizontal", **content):
    return {"id": f"id-{name}-{display}", "sceneNodeType": "item", "name": name, "x": x, "y": y,
            "scaleX": s / 1920, "scaleY": s / 1080, "crop": {"top": 0, "bottom": 0, "left": 0, "right": 0},
            "rotation": 0, "visible": True, "display": display, "locked": False,
            "content": {"nodeType": kind, **content}}


def overlay_zip(path):
    """A Streamlabs .overlay: config.json (scenes of slots, shares of a 1920x1080 canvas) and its media."""
    import zipfile

    labels = {"schemaVersion": 1, "nodeType": "TextNode", "settings": {"text": "MagicalTech", "color": -1,
                                                                      "font": {"face": "Baloo Bhai", "size": 48}}}
    config = {"schemaVersion": 1, "nodeType": "RootNode",
              "scenes": {"nodeType": "ScenesNode", "items": [
                  {"name": "Live Scene", "sceneId": "scene_live", "slots": {"items": [
                      _slot("Game Capture", "GameCaptureNode", placeholderFile="2.png", width=1920, height=1080),
                      _slot("Video Capture Device", "WebcamNode", 0.025, 0.32, 0.35, width=0.3368, height=0.3368),
                      _slot("Frame", "VideoNode", -0.1, 0.2, 0.5, filename="9.webm", settings={"looping": True}),
                      {"id": "f1", "sceneNodeType": "folder", "name": "Webcam", "childrenIds": ["x"]},
                      _slot("Most Recent Subscriber", "StreamlabelNode", 0.07, 0.71, labelType="sub",
                            textSource=labels),
                      _slot("Alertbox", "WidgetNode", 0.33, 0, settings={"url": "", "width": 800, "height": 600},
                            type=0),
                      _slot("Socials", "SceneSourceNode", 0.1, 0.5, 0.5, sceneId="scene_social", width=1920,
                            height=1080),
                      _slot("Live Dual", "VideoNode", display="vertical", filename="3.webm", settings={}),
                      _slot("Icon", "IconLibraryNode", folder="7")]}},
                  {"name": "Social Media", "sceneId": "scene_social", "slots": {"items": [
                      _slot("Text 1", "TextNode", 0.27, 0.45, settings={"text": "USERNAME", "color": -1})]}}]},
              "transition": {"nodeType": "TransitionNode", "type": "obs_stinger_transition", "duration": 300,
                             "settings": {"path": "51.webm", "transition_point": 1200}}}
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("config.json", json.dumps(config))
        for name in ("9.webm", "3.webm", "51.webm", "../evil.webm"):
            zf.writestr(name, b"video")
    return path


def test_streamlabs_overlay(tmp_path):
    from sprigcast.core.convert import is_overlay_file, unpack_overlay

    file = overlay_zip(tmp_path / "streamlabs.overlay")
    assert is_overlay_file(file)
    data = unpack_overlay(file, tmp_path / "Media" / "streamlabs")
    assert detect(data) == "streamlabs" and not (tmp_path / "Media" / "evil.webm").exists()
    col, notes = convert(data, _like())
    live = next(s for s in col.parts() if s.name == "Live Scene")
    by_name = {(col.source(i.source) or col.scene(i.source)).name: i for i in live.items}
    assert list(by_name)[0] == "Game Capture"  # Streamlabs lists the bottom item first, as OBS does
    frame = by_name["Frame"]
    assert (round(frame.x), round(frame.y), frame.scale) == (-192, 216, 0.5)
    assert col.source(frame.source).settings["path"] == str((tmp_path / "Media" / "streamlabs" / "9.webm").resolve())
    cam = by_name["Camera"]
    assert cam.bounds == "fill" and round(cam.bounds_size[0]) == 647 and round(cam.bounds_size[1]) == 364
    socials = by_name["Social Media"]
    assert (round(socials.x), round(socials.y), socials.scale) == (192, 540, 0.5)
    assert col.source(by_name["Most Recent Subscriber"].source).settings["text"] == "MagicalTech"
    assert {s.name for s in col.scenes if s.kind == "show"} == {"Live Scene"}
    assert any(s.canvas == "vertical" and s.name == "Live Scene" for s in col.scenes if s.kind == "show")
    assert col.transition.kind == "stinger" and col.transition.stinger.path.endswith("51.webm")
    assert any("Alertbox" in n and "dashboard" in n for n in notes)
    assert any("Icon" in n and "pictures" in n for n in notes)


def test_obs_sprigs_no_bloom_uses_go_in_unused_scenes():
    """OBS scenes nothing shows (a widget kit, 27 in a real garden, PC 2026-11-19) are kept but set aside in
    Unused Scenes with one note; sprigs reached only through other sprigs, and vertical twins, stay put."""
    from sprigcast.core import convert as convert_mod

    col, notes = from_obs(obs_file(), _like())
    folders = {f.id: f.name for f in col.folders}
    parts = {s.name: s for s in col.scenes if s.kind == "part"}
    assert folders[parts["Spare overlay"].folder] == convert_mod.UNUSED_FOLDER
    assert folders.get(parts["Overlay"].folder) != convert_mod.UNUSED_FOLDER
    assert [p.name for p in convert_mod.unused_parts(col)] == ["Spare overlay"]
    assert "1 unused sprig is in Unused Scenes." in notes
    data = obs_file()
    data["sources"] = [s for s in data["sources"] if s.get("uuid") != "o2"]
    _, notes = from_obs(data, _like())
    assert not any("unused sprig" in n for n in notes)


def test_what_did_not_come_over_is_noted_first():
    """The notes pop-up shows the first 40; a source with no match must not hide behind muted sounds."""
    data = obs_file()
    data["sources"] += [{"id": "wasapi_output_capture", "name": f"Out {i}", "uuid": f"w{i}",
                         "settings": {"device_id": f"gone-{i}"}} for i in range(45)]
    data["sources"].append({"id": "obs_glow_source", "name": "Glow", "uuid": "glow", "settings": {}})
    _, notes = from_obs(data, _like())
    glow = next(i for i, n in enumerate(notes) if n.startswith("Glow:"))
    assert glow < 5 and all("has no match" in n or "isn't supported" in n for n in notes[:glow + 1])


def test_stinger_finds_its_vertical_video_beside_it(tmp_path):
    """OBS's vertical canvas plugin keeps its own stinger outside the collection; the "-vertical" file beside
    the main one becomes the vertical canvas's video (PC, 2026-11-19)."""
    from sprigcast.core.convert import vertical_twin

    main = tmp_path / "stinger.webm"
    main.write_bytes(b"x")
    assert vertical_twin(str(main)) == ""
    (tmp_path / "stinger-vertical.webm").write_bytes(b"x")
    assert vertical_twin(str(main)) == str(tmp_path / "stinger-vertical.webm")
    data = obs_file()
    data["transitions"] = [{"name": "Sting", "id": "obs_stinger_transition",
                            "settings": {"path": str(main), "transition_point": 900}}]
    data["current_transition"] = "Sting"
    col, _ = from_obs(data, _like())
    assert col.transition.stinger.vertical_path == str(tmp_path / "stinger-vertical.webm")


def test_obs_scene_filters_and_monitoring():
    """A Move filter on a scene that only turns one thing becomes a Barrel roll (off) on it; other scene
    filters are noted, not dropped quietly; OBS's monitoring comes along (PC, 2026-11-19)."""
    data = obs_file()
    scene = next(s for s in data["sources"] if s["name"] == "Overlay")
    scene["filters"] = [
        {"id": "move_source_filter", "name": "Roll", "enabled": False,
         "settings": {"source": "Chat", "rot": 1800, "duration": 12000, "custom_duration": True,
                      "pos": {"x": 0, "y": 0}, "scale": {"x": 0, "y": 0}}},
        {"id": "some_scene_filter", "name": "Sparkle", "settings": {}}]
    chat = next(s for s in data["sources"] if s["name"] == "Chat")
    chat["monitoring_type"] = 2
    col, notes = from_obs(data, _like())
    page = next(s for s in col.sources if s.name == "Chat")
    roll = next(f for f in page.filters if f.kind == "barrel_roll")
    assert not roll.enabled and roll.settings == {"seconds": 12.0, "turns": 5, "direction": "clockwise"}
    assert page.settings["monitor"] is True
    assert "Overlay: the Sparkle filter has no match yet." in notes
    assert any(n.startswith("Roll: now a Barrel roll on Chat") for n in notes)
