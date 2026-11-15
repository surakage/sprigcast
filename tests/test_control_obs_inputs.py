"""The OBS door's input settings, filters, media and picture requests (what TikFinity, Streamer.bot
and Stream Deck's OBS actions send most)."""

import base64

from sprigcast.control.protocol import Status, Sub
from sprigcast.core.scene import Filter, SourceConfig
from tests.test_control_api import _api, _fails, _ok


def _with_extras():
    api, host = _api()
    col = host.collection
    col.sources += [
        SourceConfig(id="goal", kind="text", name="Goal text", settings={"text": "0 / 50", "size": 40}),
        SourceConfig(id="pic", kind="image", name="Logo", settings={"path": "a.png"}),
        SourceConfig(id="clip", kind="media", name="Hype", settings={"path": "a.mp4", "loop": False, "volume_db": -3}),
        SourceConfig(id="page", kind="browser", name="Alerts", settings={"url": "https://a"}),
    ]
    col.sources[2].filters = [Filter(id="filter-1", kind="blur", name="Blur", enabled=False, settings={"size": 4})]
    host.media["clip"] = {"state": "playing", "position": 2.5, "duration": 10.0}
    return api, host


def test_set_input_settings_text_and_image():
    api, host = _with_extras()
    _ok(api, "SetInputSettings", {"inputName": "Goal text", "inputSettings": {"text": "42 / 50"}})
    goal = next(s for s in host.collection.sources if s.id == "goal")
    assert goal.settings == {"text": "42 / 50", "size": 40}
    assert host.commits[-1] == "sources"
    _ok(api, "SetInputSettings", {"inputName": "Logo", "inputSettings": {"file": "b.png"}})
    assert next(s for s in host.collection.sources if s.id == "pic").settings["path"] == "b.png"


def test_set_input_settings_replace_keeps_mixer():
    api, host = _with_extras()
    _ok(api, "SetInputSettings", {"inputName": "Hype", "overlay": False,
                                  "inputSettings": {"local_file": "b.mp4", "looping": True}})
    clip = next(s for s in host.collection.sources if s.id == "clip")
    assert clip.settings == {"volume_db": -3, "path": "b.mp4", "loop": True}


def test_set_input_settings_errors():
    api, _ = _with_extras()
    assert _fails(api, "SetInputSettings", {"inputName": "Nope", "inputSettings": {}}) == Status.RESOURCE_NOT_FOUND
    assert _fails(api, "SetInputSettings", {"inputName": "Logo"}) == Status.MISSING_REQUEST_FIELD


def test_default_settings_and_rename():
    api, host = _with_extras()
    out = _ok(api, "GetInputDefaultSettings", {"inputKind": "text_gdiplus_v3"})
    assert "text" in out["defaultInputSettings"]
    _ok(api, "SetInputName", {"inputName": "Logo", "newInputName": "Big logo"})
    assert any(s.name == "Big logo" for s in host.collection.sources)
    assert _fails(api, "SetInputName", {"inputName": "Big logo", "newInputName": "Hype"}) == Status.RESOURCE_ALREADY_EXISTS


def test_filters():
    api, host = _with_extras()
    listed = _ok(api, "GetSourceFilterList", {"sourceName": "Cam"})["filters"]
    assert listed == [{"filterName": "Blur", "filterKind": "blur", "filterIndex": 0, "filterEnabled": False,
                       "filterSettings": {"size": 4}}]
    assert _ok(api, "GetSourceFilter", {"sourceName": "Cam", "filterName": "Blur"})["filterEnabled"] is False
    api.changes()
    _ok(api, "SetSourceFilterEnabled", {"sourceName": "Cam", "filterName": "Blur", "filterEnabled": True})
    cam = next(s for s in host.collection.sources if s.id == "cam")
    assert cam.filters[0].enabled and host.commits[-1] == "filters"
    assert ("SourceFilterEnableStateChanged", Sub.FILTERS,
            {"sourceName": "Cam", "filterName": "Blur", "filterEnabled": True}) in api.changes()
    _ok(api, "SetSourceFilterSettings", {"sourceName": "Cam", "filterName": "Blur", "filterSettings": {"size": 9}})
    assert cam.filters[0].settings == {"size": 9}
    _ok(api, "SetSourceFilterName", {"sourceName": "Cam", "filterName": "Blur", "newFilterName": "Soft"})
    assert cam.filters[0].name == "Soft"
    assert _fails(api, "SetSourceFilterEnabled", {"sourceName": "Cam", "filterName": "Blur",
                                                  "filterEnabled": True}) == Status.RESOURCE_NOT_FOUND


def test_media():
    api, host = _with_extras()
    status = _ok(api, "GetMediaInputStatus", {"inputName": "Hype"})
    assert status == {"mediaState": "OBS_MEDIA_STATE_PLAYING", "mediaDuration": 10000, "mediaCursor": 2500}
    _ok(api, "TriggerMediaInputAction", {"inputName": "Hype", "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PAUSE"})
    assert host.media["clip"]["state"] == "paused"
    _ok(api, "TriggerMediaInputAction", {"inputName": "Hype", "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_RESTART"})
    assert host.media["clip"] == {"state": "playing", "position": 0.0, "duration": 10.0}
    _ok(api, "SetMediaInputCursor", {"inputName": "Hype", "mediaCursor": 4000})
    assert host.media["clip"]["position"] == 4.0
    _ok(api, "OffsetMediaInputCursor", {"inputName": "Hype", "mediaCursorOffset": -1000})
    assert host.media["clip"]["position"] == 3.0
    assert _fails(api, "TriggerMediaInputAction", {"inputName": "Logo",
                                                   "mediaAction": "OBS_WEBSOCKET_MEDIA_INPUT_ACTION_PLAY"}) == Status.INVALID_RESOURCE_TYPE
    assert _fails(api, "TriggerMediaInputAction", {"inputName": "Hype", "mediaAction": "x"}) == Status.INVALID_REQUEST_FIELD


def test_browser_refresh():
    api, host = _with_extras()
    reloaded = []

    class Page:
        def reload(self):
            reloaded.append(True)

    host.source_instance = lambda sid: Page() if sid == "page" else None
    _ok(api, "PressInputPropertiesButton", {"inputName": "Alerts", "propertyName": "refreshnocache"})
    assert reloaded == [True]
    assert _fails(api, "PressInputPropertiesButton", {"inputName": "Logo", "propertyName": "refreshnocache"}) == Status.INVALID_REQUEST_FIELD


def test_source_screenshot(tmp_path):
    api, host = _with_extras()
    out = _ok(api, "GetSourceScreenshot", {"sourceName": "Chatting", "imageFormat": "png", "imageWidth": 640})
    assert out["imageData"] == "data:image/png;base64,iVBOR"
    assert host.shots[-1] == (["horizontal"], 640, False, True)
    target = tmp_path / "shot.png"
    host.screenshot = lambda shapes, width, save, as_data: [{"shape": shapes[0], "png": base64.b64encode(b"PNG").decode()}]
    _ok(api, "SaveSourceScreenshot", {"sourceName": "Chatting", "imageFormat": "png", "imageFilePath": str(target)})
    assert target.read_bytes() == b"PNG"
    assert _fails(api, "GetSourceScreenshot", {"sourceName": "BRB", "imageFormat": "png"}) == Status.INVALID_RESOURCE_STATE
    assert _fails(api, "GetSourceScreenshot", {"sourceName": "Chatting", "imageFormat": "jpg"}) == Status.INVALID_REQUEST_FIELD
