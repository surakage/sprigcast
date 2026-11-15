from sprigcast.sources.camera import OWN_CAMERA, CameraDevice, is_virtual, real_camera


def test_virtual_cameras_are_spotted():
    assert is_virtual("EƖgato Virtual Camera") and is_virtual("OBS Virtual Camera")
    assert not is_virtual("Elgato Facecam MK.2") and not is_virtual(OWN_CAMERA)


def test_real_camera_prefers_a_cam_of_the_same_brand_never_a_capture_card():
    cams = [CameraDevice(0, "EƖgato Virtual Camera", "v"), CameraDevice(1, "Elgato HD60 X", "c"),
            CameraDevice(2, "Phone Link camera", "p"), CameraDevice(3, "Elgato Facecam MK.2", "f"),
            CameraDevice(4, OWN_CAMERA, "o")]
    assert real_camera(cams, "EƖgato Virtual Camera").name == "Elgato Facecam MK.2"
    assert real_camera(cams[:3], "EƖgato Virtual Camera").name == "Phone Link camera"
    assert real_camera(cams[:2], "EƖgato Virtual Camera") is None
