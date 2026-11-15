from sprigcast.core.device_makers import is_effects, label, maker


def test_makers_and_labels():
    assert maker("EƖgato Virtual Camera") == "Camera Hub"
    assert label("EƖgato Virtual Camera") == "EƖgato Virtual Camera · Camera Hub"
    assert label("Wave Link Stream (Elgato Virtual Audio)") == "Wave Link Stream (Elgato Virtual Audio)"
    assert label("Game (Elgato Virtual Audio)") == "Game (Elgato Virtual Audio) · Wave Link"
    assert label("Camera (NVIDIA Broadcast)") == "Camera (NVIDIA Broadcast)"
    assert maker("Microphone (Voicemod Virtual Audio Device (WDM))") == "Voicemod"
    assert maker("Streamfog Virtual Camera") == "Streamfog"
    assert maker("Elgato Facecam MK.2") is None
    assert maker("SprigCast Camera") is None
    assert label("Elgato Facecam MK.2") == "Elgato Facecam MK.2"


def test_effects():
    assert is_effects("Camera (NVIDIA Broadcast)") and is_effects("Streamfog Virtual Camera")
    assert not is_effects("EƖgato Virtual Camera") and not is_effects("OBS Virtual Camera")
