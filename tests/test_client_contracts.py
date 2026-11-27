"""What the apps built on SprigCast call must keep working: the Streamer.bot pack (docs/streamerbot/SprigCast.cs,
read here) and THSV StreamBridge (its list of requests, events and features, from its own code as of 2026-11-22
plus the viewer counts it reads since 2026-11-26). A rename or a dropped field fails here first.
The Stream Deck plugin has its own contract test (test_streamdeck_contract.py)."""

import re
from pathlib import Path

from sprigcast.control import native, spec

ROOT = Path(__file__).parent.parent
PACK = (ROOT / "docs" / "streamerbot" / "SprigCast.cs").read_text(encoding="utf-8")


def _built_in() -> dict[str, dict[str, str]]:
    found = (getattr(getattr(native.NativeApi, attr), "_action", None) for attr in dir(native.NativeApi))
    return {name: fields for name, _about, fields in (f for f in found if f)}


def _store() -> set[str]:
    return set(native._store_action_plugins())


def _check(calls: dict[str, set[str]]) -> list[str]:
    built_in, store = _built_in(), _store()
    wrong = []
    for name, fields in calls.items():
        if name in built_in:
            wrong += [f"{name}: no field {f}" for f in sorted(fields) if f not in built_in[name]]
        elif name not in store:
            wrong.append(f"{name}: no such action")
    return wrong


def test_the_streamerbot_pack_calls_only_what_exists():
    calls: dict[str, set[str]] = {}
    for line in PACK.splitlines():
        found = re.search(r'Call\("([a-zA-Z_.]+)"', line)
        if not found:
            continue
        fields = set()
        for group in re.findall(r"Fields\(([^)]*)\)", line):
            fields |= set(re.findall(r'"([a-zA-Z_]+)"', group))
        fields |= set(re.findall(r'\\"([a-zA-Z_]+)\\":', line.split('\\"settings\\": ')[0]))
        if '\\"settings\\": ' in line:
            fields.add("settings")
        calls.setdefault(found.group(1), set()).update(fields)
    assert len(calls) >= 30, "the pack's calls weren't found: has SprigCast.cs changed shape?"
    wrong = _check(calls)
    assert not wrong, f"the Streamer.bot pack would break: {'; '.join(wrong)}"


STREAMBRIDGE_CALLS = {
    "app.info": set(), "plugins.list": set(),
    "blooms.list": set(), "bloom.show": {"bloom", "shape"}, "bloom.new": {"name", "shape"}, "bloom.remove": {"bloom"},
    "sprigs.list": set(), "sources.list": set(), "source.get": {"source"},
    "source.new": {"name", "kind", "settings"}, "source.set": {"source", "settings"},
    "items.list": {"bloom", "sprig"}, "item.add": {"source", "bloom", "sprig", "shape", "visible"},
    "item.place": {"item", "bloom", "sprig", "shape", "x", "y", "scale", "scale_x", "scale_y", "crop", "bounds",
                   "bounds_width", "bounds_height", "bounds_align", "align"},
    "item.order": {"item", "bloom", "to"}, "item.show": {"item", "bloom"}, "item.hide": {"item", "bloom"},
    "item.remove": {"item", "bloom"}, "layout.get": {"bloom", "sprig", "shape", "flat", "overlaps"},
    "edit.undo": set(), "filter.set": {"source", "filter", "on", "settings"},
    "hide.blackout": set(), "hide.pixelate": set(), "roll.spin": set(),
    "stream.status": set(), "stream.stop": set(), "record.status": set(),
    "camera.status": set(), "camera.set": {"on"},
    "media.status": {"media"}, "media.restart": {"media"}, "media.stop": {"media"},
    "sound.mute": {"sound", "muted"}, "sound.volume": {"sound", "db", "change"},
    "screenshot.take": {"shape", "save", "data", "bloom", "source"},
    "youtube.viewers": set(), "viewers.counts": set(),
}
STREAMBRIDGE_EVENTS = {"bloom.shown", "blooms.changed", "garden.changed", "stream.changed", "record.changed",
                       "app.closing", "place.changed"}
STREAMBRIDGE_FEATURES = {"bloom.new", "places.status", "item.place_full", "item.scale_xy", "layout.get", "edit.undo",
                         "screenshot.source", "youtube.viewers", "viewers.counts", "events.place_changed"}


def test_what_streambridge_calls_still_exists():
    wrong = _check(STREAMBRIDGE_CALLS)
    assert not wrong, f"StreamBridge would break: {'; '.join(wrong)}"
    assert STREAMBRIDGE_EVENTS <= set(spec.EVENTS)
    assert STREAMBRIDGE_FEATURES <= set(native.FEATURES)
    assert {g for g in ("bloom", "garden", "output")} <= set(native.EVENT_GROUPS)
