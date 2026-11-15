"""Cameras and sound devices that other apps make (NVIDIA Broadcast, Streamfog, Voicemod, Camera Hub,
Wave Link...). Their effects run in those apps, so picking one costs SprigCast nothing extra.

maker() says which app a device comes from, so pickers can show it ("Elgato Virtual Camera · Camera Hub")
and the camera tips can tell an effects camera from a plain pass-through one.
"""

from __future__ import annotations

# (words in the device's name, lower case; the app; whether its point is effects you chose on purpose)
_MAKERS: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (("nvidia broadcast",), "NVIDIA Broadcast", True),
    (("streamfog",), "Streamfog", True),
    (("voicemod",), "Voicemod", True),
    (("snap camera",), "Snap Camera", True),
    (("xsplit vcam",), "XSplit VCam", True),
    (("elgato virtual camera", "camera hub"), "Camera Hub", False),
    (("elgato virtual audio", "wave link"), "Wave Link", False),
    (("obs virtual camera", "obs-camera"), "OBS", False),
    (("tiktok live studio",), "TikTok LIVE Studio", False),
    (("camo",), "Camo", False),
    (("phone link",), "Phone Link", False),
    (("vb-audio", "voicemeeter", "cable output", "cable input"), "VB-Audio", False),
)


def _plain(name: str) -> str:
    return name.replace("Ɩ", "l").replace("ı", "i").lower()  # Elgato spells its virtual camera "EƖgato"


def maker(name: str) -> str | None:
    """The app that makes this camera or sound device, or None for hardware (and SprigCast's own)."""
    plain = _plain(name)
    if "sprigcast" in plain:
        return None
    for words, app, _effects in _MAKERS:
        if any(word in plain for word in words):
            return app
    return None


def is_effects(name: str) -> bool:
    """A device people pick for its effects (background removal, AR lenses, voice changing)."""
    plain = _plain(name)
    return any(effects and any(word in plain for word in words) for words, _app, effects in _MAKERS)


def label(name: str) -> str:
    """The device's name, plus the app that makes it when the name doesn't already say so."""
    app = maker(name)
    if app is None or _plain(app) in _plain(name):
        return name
    return f"{name} · {app}"
