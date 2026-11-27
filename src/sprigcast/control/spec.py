"""The SprigCast API described for programs: protocol.json, and the reference page made from it.

Every action already says what it does and lists its fields in words (@action in native.py). This module adds
what a program needs to call them without guessing: each field's type, whether it's needed, its default, its
range or the words it takes; every event with its group and fields; every error code, error reason and close
code. protocol() puts it all in one JSON object: SprigCast answers it as app.protocol and at /api/protocol.json,
and docs/api/protocol.json and docs/api/reference.md are written from it (python -m sprigcast.control.spec).

The keys in the description itself are camelCase, the style the API is settling on (2026-11-27). The fields it
describes keep the spelling they have today.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from sprigcast.core.scene import ALIGNS, MOVE_EASINGS, MOVE_UNMATCHED, TRANSITION_KINDS

SHAPES = ("both", "horizontal", "vertical")

# Each field name's type wherever it's used. "ref" is text naming something by its name or its id.
FIELD_TYPES: dict[str, dict[str, Any]] = {
    **{name: {"type": "string", "accepts": "name or id"} for name in (
        "bloom", "sprig", "source", "item", "sound", "media", "filter", "in")},
    **{name: {"type": "string", "accepts": "name"} for name in ("garden", "path", "copy")},
    "place": {"type": "string", "accepts": "id, name or service"},
    "hotkey": {"type": "string", "accepts": "hotkey action id"},
    "name": {"type": "string"},
    "kind": {"type": "string"},
    "state": {"type": ["string", "null"]},
    "expression": {"type": "string"},
    "how": {"type": "string", "values": ["pixelate", "blackout"]},
    "direction": {"type": "string"},
    "style": {"type": "string", "values": ["bars", "wave", "circle", "line"]},
    "color": {"type": "string"},
    "peak_color": {"type": "string"},
    "shape": {"type": "string", "values": list(SHAPES), "default": "both"},
    "to": {"type": "string", "values": ["top", "bottom", "up", "down"]},
    "bounds": {"type": "string", "values": ["off", "fit", "fill", "stretch"]},
    "align": {"type": "string", "values": list(ALIGNS)},
    "bounds_align": {"type": "string", "values": list(ALIGNS)},
    "easing": {"type": "string", "values": list(MOVE_EASINGS)},
    "unmatched": {"type": "string", "values": list(MOVE_UNMATCHED)},
    **{name: {"type": "number"} for name in (
        "balance", "change", "db", "degrees", "dx", "dy", "rotation", "scale", "scale_x", "scale_y", "seconds",
        "x", "y", "bounds_width", "bounds_height", "speed", "sensitivity", "minutes")},
    **{name: {"type": "integer"} for name in (
        "bitrateKbps", "index", "ms", "width", "amount", "count", "size", "turns")},
    **{name: {"type": "boolean"} for name in (
        "bandwidth_test", "flat", "force", "locked", "muted", "on", "overlaps", "save", "visible")},
    "data": {"type": "boolean"},
    "crop": {"type": "object", "keys": ["left", "top", "right", "bottom"]},
    "settings": {"type": "object"},
    "places": {"type": "array", "items": "string"},
}

# Where one action uses a field name differently from the rest.
ACTION_FIELD_TYPES: dict[tuple[str, str], dict[str, Any]] = {
    ("app.broadcast", "data"): {"type": "object"},
    ("transition.set", "kind"): {"type": "string", "values": list(TRANSITION_KINDS)},
    ("bloom.new", "shape"): {"type": "string", "values": list(SHAPES), "default": "both"},
}

_RANGE = re.compile(r"(-?\d+(?:\.\d+)?) to (-?\d+(?:\.\d+)?)")


def field_spec(action: str, name: str, words: str) -> dict[str, Any]:
    """One field of an action, from the words @action gives it ("?" first: optional) and FIELD_TYPES."""
    required = not words.startswith("?")
    about = words.lstrip("?").strip()
    spec: dict[str, Any] = {"name": name, "required": required, "about": about}
    if "_" in name:  # also answers to its camelCase spelling (scale_x: scaleX)
        head, *rest = name.split("_")
        spec["camelCase"] = head + "".join(word.title() for word in rest)
    known = ACTION_FIELD_TYPES.get((action, name)) or FIELD_TYPES.get(name)
    spec.update(known or {"type": "any"})
    if spec["type"] in ("number", "integer"):
        found = _RANGE.search(about)
        if found:
            low, high = (float(v) for v in found.groups())
            spec["min"], spec["max"] = (int(low), int(high)) if spec["type"] == "integer" else (low, high)
    if spec["type"] == "boolean" and "default" not in spec:
        if re.search(r"\btrue \(default\)", about):
            spec["default"] = True
        elif re.search(r"\bfalse \(default\)", about):
            spec["default"] = False
    if required:
        spec.pop("default", None)
    return spec


def action_group(name: str) -> str:
    """The noun an action belongs to: blooms.list -> bloom, plugins.list -> app."""
    head = name.split(".", 1)[0]
    if head in ("plugins", "viewers"):
        return {"plugins": "app", "viewers": "viewers"}[head]
    return head[:-1] if head.endswith("s") else head


# Every event SprigCast sends: name -> group and the fields it carries. A plugin's events are in group "plugin".
EVENTS: dict[str, dict[str, Any]] = {
    "app.closing": {"group": None, "about": "SprigCast is closing or turning its API off; every app gets it. "
                    "The connection closes next: look again in a second or two, it's often a restart.",
                    "fields": []},
    "bloom.shown": {"group": "bloom", "about": "A bloom went on air.", "fields": ["bloom", "bloomId", "shape"]},
    "blooms.changed": {"group": "bloom", "about": "Blooms were added, removed or renamed.", "fields": ["blooms"]},
    "item.changed": {"group": "item", "about": "Something in a bloom or sprig was shown or hidden.",
                     "fields": ["bloom", "sprig", "shape", "item", "itemId", "visible", "blooms"]},
    "items.changed": {"group": "item", "about": "Something was added to or taken out of a bloom or sprig.",
                      "fields": ["bloom", "sprig", "shape", "items"]},
    "item.moved": {"group": "item", "about": "Something was moved, sized, cropped or turned, by hand or by an app.",
                   "fields": ["bloom", "sprig", "shape", "item", "itemId", "x", "y", "scale_x", "scale_y", "rotation",
                              "align", "crop", "bounds"]},
    "sprig.state": {"group": "sprig", "about": "A sprig switched Live state.", "fields": ["sprig", "sprigId", "state"]},
    "sound.changed": {"group": "sound", "about": "A sound's mute, volume or balance changed.",
                      "fields": ["id", "name", "muted", "volumeDb", "balance"]},
    "filter.changed": {"group": "filter", "about": "A filter was added, switched or changed.",
                       "fields": ["source", "id", "name", "kind", "on", "settings", "added"]},
    "filter.removed": {"group": "filter", "about": "A filter was removed.", "fields": ["source", "id", "name"]},
    "effect.ended": {"group": "filter", "about": "A timed effect (Hide it, Barrel Roll, Freeze) ended.",
                     "fields": ["source", "sourceId", "effect", "reason"]},
    "source.changed": {"group": "source", "about": "A source's settings changed.",
                       "fields": ["source", "sourceId", "settings"]},
    "sources.changed": {"group": "source", "about": "Sources were added, removed or renamed.",
                        "fields": ["sources", "added", "removed", "renamed"]},
    "stream.changed": {"group": "output", "about": "The stream started or ended.", "fields": ["on"]},
    "record.changed": {"group": "output", "about": "Recording started or stopped.", "fields": ["on"]},
    "record.paused": {"group": "output", "about": "The recording was paused or carried on.", "fields": ["paused"]},
    "clips.changed": {"group": "output", "about": "Clips were made ready or stopped.", "fields": ["on"]},
    "clip.saved": {"group": "output", "about": "A clip's files are written.", "fields": ["files"]},
    "camera.changed": {"group": "output", "about": "SprigCast Camera was turned on or off.", "fields": ["on"]},
    "place.changed": {"group": "output", "about": "One place started, dropped, is trying again, came back or stopped.",
                      "fields": ["place", "name", "service", "shape", "state", "was", "reconnects", "dropped"]},
    "transition.changed": {"group": "transition", "about": "The usual transition changed.", "fields": ["kind", "ms"]},
    "media.changed": {"group": "media", "about": "A video or sound file played, paused, stopped or ended.",
                      "fields": ["media", "id", "file", "loop", "state", "position", "duration"]},
    "garden.changed": {"group": "garden", "about": "Another garden is in use.", "fields": ["garden"]},
    "gardens.changed": {"group": "garden", "about": "Gardens were added, removed or renamed.", "fields": ["gardens"]},
    "path.changed": {"group": "path", "about": "Another path is in use.", "fields": ["path"]},
    "paths.changed": {"group": "path", "about": "Paths were added, removed or renamed.", "fields": ["paths"]},
    "custom": {"group": "custom", "about": "Sent by another app with app.broadcast.", "fields": ["data"]},
    "plugins.changed": {"group": "plugin", "about": "A plugin was added or removed.",
                        "fields": ["plugins", "added", "removed"]},
    "meters": {"group": "meters", "about": "Sound levels, about 15 a second; only when asked for by name.",
               "fields": ["levels"]},
    # Store plugins' own events (group "plugin").
    "break.started": {"group": "plugin", "plugin": "Break & End Guard", "about": "A break started.",
                      "fields": ["onBreak", "breakLeft", "nextBreakIn", "endIn"]},
    "break.ended": {"group": "plugin", "plugin": "Break & End Guard", "about": "A break ended.",
                    "fields": ["onBreak", "breakLeft", "nextBreakIn", "endIn"]},
    "stream.limit": {"group": "plugin", "plugin": "Break & End Guard", "about": "The stream limit is close.",
                     "fields": []},
    "countdown.changed": {"group": "plugin", "plugin": "Countdown", "about": "A countdown changed.",
                          "fields": ["source", "seconds", "running", "done", "text"]},
    "countdown.done": {"group": "plugin", "plugin": "Countdown", "about": "A countdown reached 0 on screen.",
                       "fields": ["source"]},
    "deaths.changed": {"group": "plugin", "plugin": "Death count", "about": "The death count changed.",
                       "fields": ["count"]},
    "nowplaying.changed": {"group": "plugin", "plugin": "Now playing",
                           "about": "The song changed, paused or jumped while the card is on screen.",
                           "fields": ["playing", "title", "artist", "album", "app", "length", "position"]},
    "pngtuber.expression": {"group": "plugin", "plugin": "PNGtuber", "about": "The expression changed.",
                            "fields": ["expression"]},
    "replay.started": {"group": "plugin", "plugin": "Instant replay", "about": "A replay started playing.",
                       "fields": ["source", "sourceId", "seconds", "speed"]},
    "replay.ended": {"group": "plugin", "plugin": "Instant replay", "about": "A replay ended.",
                     "fields": ["source", "sourceId", "reason"]},
    "snap.taken": {"group": "plugin", "plugin": "Snap", "about": "A screenshot became a polaroid on stream.",
                   "fields": []},
    "timer.changed": {"group": "plugin", "plugin": "Stream timer", "about": "A stream timer changed.",
                      "fields": ["source", "mode", "seconds", "running", "done", "text"]},
    "timer.done": {"group": "plugin", "plugin": "Stream timer", "about": "A countdown reached 0 on screen.",
                   "fields": ["source"]},
}


def known_event(name: str) -> bool:
    """Whether an app may ask for this event by name: one SprigCast sends, or one from a plugin that's added
    (its first word is a plugin's actions' or events' first word)."""
    if name in EVENTS:
        return True
    from sprigcast.control import native

    head = name.split(".", 1)[0]
    return head in native._PLUGIN_EVENTS or any(a.split(".", 1)[0] == head for a in native._PLUGIN_ACTIONS)


ERROR_CODES: dict[str, str] = {
    "bad_request": "A field is missing or has the wrong type or value; field and reason say which and why.",
    "not_found": "No such bloom, sprig, item, sound, source, filter or place; what and name say what was asked for.",
    "not_allowed": "It can't be done here, e.g. putting a sprig on air.",
    "busy": "Already doing it (already live), or the name is taken (reason already_exists).",
    "unknown_action": "No such action.",
    "doctor_warnings": "Going live or recording would have asked first: nothing started; warnings lists why, "
                       "force: true goes ahead.",
    "places_differ": "stream.start was given places and the places switched on aren't exactly those: nothing "
                     "started; places lists the ones switched on.",
    "plugin_missing": "The action comes with a store plugin that isn't added; the message names it.",
    "failed": "Something went wrong inside SprigCast.",
}

ERROR_REASONS: dict[str, str] = {
    "missing": "A field that's needed wasn't sent.",
    "empty": "A field was sent empty.",
    "wrong_type": "A field has the wrong type (text where a number goes...).",
    "out_of_range": "A number is outside what the field takes.",
    "not_one_of": "A word the field doesn't take.",
    "already_exists": "Something already has that name.",
    "already_running": "It's already going (live, recording).",
    "conflict": "Two spellings of one field (bloom and bloomId) name different things.",
}

CLOSE_CODES: dict[int, str] = {
    1001: "SprigCast closed, or its API was turned off (app.closing came first).",
    4002: "A message wasn't a JSON object.",
    4009: "Wrong password or key.",
    4011: "Wrong or expired pairing code, or too many wrong codes (pairing locks for a minute).",
}


def _actions(include_plugins: bool) -> list[tuple[str, str, dict[str, str], str | None]]:
    from sprigcast.control import native

    found = [getattr(getattr(native.NativeApi, attr), "_action", None) for attr in dir(native.NativeApi)]
    out = [(name, about, fields, None) for name, about, fields in (f for f in found if f)]
    if include_plugins:
        built_in = {a[0] for a in out}
        out += [(a.name, a.about, a.fields, a.module.removeprefix("sprigcast_plugin_") or None)
                for a in native._PLUGIN_ACTIONS.values() if a.name not in built_in]
    return sorted(out, key=lambda a: a[0])


def protocol(include_plugins: bool = True, app_version: bool = True) -> dict[str, Any]:
    """Everything about the API in one object (docs/api/protocol.json is this without plugins or the version)."""
    from sprigcast.control import native

    out: dict[str, Any] = {"app": "SprigCast"}
    if app_version:
        from sprigcast import __version__

        out["version"] = __version__
    out.update({
        "api": native.API_VERSION, "preview": native.PREVIEW,
        "connect": {"path": "/sprigcast", "defaultPort": 4455, "webLinks": "/api/<action>"},
        "eventGroups": [{"group": g, "highVolume": g in native.HIGH_VOLUME}
                        for g in (*native.EVENT_GROUPS, *native.HIGH_VOLUME)],
        "events": [{"name": name, **{k: v for k, v in info.items() if k != "fields"}, "fields": info["fields"]}
                   for name, info in sorted(EVENTS.items())],
        "errorCodes": [{"code": c, "about": a} for c, a in ERROR_CODES.items()],
        "errorReasons": [{"reason": r, "about": a} for r, a in ERROR_REASONS.items()],
        "closeCodes": [{"code": c, "about": a} for c, a in sorted(CLOSE_CODES.items())],
        "features": list(native.FEATURES),
        "limits": {"steps": native.MAX_STEPS, "waitMs": native.MAX_WAIT_MS},
        "actionAliases": [{"name": new, "sameAs": old} for new, old in sorted(native.ACTION_ALIASES.items())],
        "idFields": [{"field": f, "for": base} for f, base in native.ID_FIELDS.items()],
        "actions": [],
    })
    for name, about, fields, plugin in _actions(include_plugins):
        entry: dict[str, Any] = {"name": name, "group": action_group(name), "about": about,
                                 "fields": [field_spec(name, f, words) for f, words in fields.items()],
                                 "looksOnly": name.endswith(native.LOOKING_SUFFIXES),
                                 "oneStep": name in native.BATCH_ACTIONS}
        if plugin:
            entry["plugin"] = plugin
        out["actions"].append(entry)
    return out


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _field_cell(spec: dict[str, Any]) -> str:
    kind = spec["type"] if isinstance(spec["type"], str) else " or ".join(spec["type"])
    bits = [kind]
    if "values" in spec:
        bits = [" \\| ".join(f"`{v}`" for v in spec["values"])]
    if "min" in spec:
        bits.append(f"{spec['min']} to {spec['max']}")
    if "default" in spec:
        bits.append(f"default `{json.dumps(spec['default'])}`")
    return ", ".join(bits)


def reference_markdown(spec: dict[str, Any]) -> str:
    """docs/api/reference.md: every action, event, error and close code, written from protocol()."""
    lines = [
        "# SprigCast API reference",
        "",
        "<!-- Written by `python -m sprigcast.control.spec` from the actions in the code. Don't edit by hand:",
        "     tests/test_control_spec.py fails when this page is behind. -->",
        "",
        "Every action, field, event and error, as SprigCast itself describes them. The same description, for "
        "programs, is [protocol.json](protocol.json); a running SprigCast also answers it as `app.protocol` and at "
        "`http://127.0.0.1:<port>/api/protocol.json`. How to connect and how messages look: "
        "[the control API](../control-api.md). Naming, versions and errors: [conventions](conventions.md).",
        "",
        f"API version {spec['api']}{' (preview)' if spec['preview'] else ''}. Store plugins' actions are listed in "
        "[the control API](../control-api.md#store-plugins).",
        "",
        "## Actions",
        "",
    ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in spec["actions"]:
        groups.setdefault(entry["group"], []).append(entry)
    for group in sorted(groups):
        lines += [f"### {group}", ""]
        for entry in groups[group]:
            tags = [t for t, on in (("looks only", entry["looksOnly"]), ("can be in one_step", entry["oneStep"])) if on]
            lines += [f"#### `{entry['name']}`", "", entry["about"] + (f" *({', '.join(tags)})*" if tags else ""), ""]
            if entry["fields"]:
                lines += ["| Field | Needed | Type | About |", "| --- | --- | --- | --- |"]
                for f in entry["fields"]:
                    about = f["about"] + (f" ({f['accepts']})" if "accepts" in f and f["accepts"] not in f["about"]
                                          else "")
                    named = f"`{f['name']}`" + (f" or `{f['camelCase']}`" if "camelCase" in f else "")
                    lines.append(f"| {named} | {'yes' if f['required'] else 'no'} | {_field_cell(f)} | "
                                 f"{_cell(about)} |")
                lines.append("")
            example = {"id": "1", "do": entry["name"]}
            example.update({f["name"]: _example(f) for f in entry["fields"] if f["required"]})
            lines += ["```json", json.dumps(example), "```", ""]
    lines += ["## Other names", "", "Newer names for older actions (both work):", ""]
    lines += [f"- `{a['name']}` is `{a['sameAs']}`" for a in spec["actionAliases"]]
    lines += ["", "Fields that name exactly one thing by its id, never by a name:", ""]
    lines += [f"- `{f['field']}` in place of `{f['for']}`" for f in spec["idFields"]]
    lines += ["", "## Events", "", "| Event | Group | Fields | About |", "| --- | --- | --- | --- |"]
    for event in spec["events"]:
        group = event["group"] or "every app"
        fields = ", ".join(f"`{f}`" for f in event["fields"]) or "none"
        about = event["about"] + (f" ({event['plugin']} plugin)" if "plugin" in event else "")
        lines.append(f"| `{event['name']}` | {group} | {fields} | {_cell(about)} |")
    lines += ["", "## Error codes", "", "| Code | About |", "| --- | --- |"]
    lines += [f"| `{e['code']}` | {_cell(e['about'])} |" for e in spec["errorCodes"]]
    lines += ["", "## Error reasons", "", "| Reason | About |", "| --- | --- |"]
    lines += [f"| `{e['reason']}` | {_cell(e['about'])} |" for e in spec["errorReasons"]]
    lines += ["", "## Close codes", "", "| Code | About |", "| --- | --- |"]
    lines += [f"| `{e['code']}` | {_cell(e['about'])} |" for e in spec["closeCodes"]]
    return "\n".join(lines) + "\n"


def _example(spec: dict[str, Any]) -> Any:
    if "values" in spec:
        return spec["values"][0]
    kind = spec["type"] if isinstance(spec["type"], str) else spec["type"][0]
    if kind in ("number", "integer"):
        return spec.get("min", 1) if spec.get("min", 0) > 0 else 1
    return {"string": "Name", "boolean": True, "object": {}, "array": []}.get(kind, "Name")


DOCS = Path(__file__).resolve().parents[3] / "docs" / "api"


def written_files() -> dict[Path, str]:
    """The files `python -m sprigcast.control.spec` writes, and what goes in them."""
    spec = protocol(include_plugins=False, app_version=False)
    return {DOCS / "protocol.json": json.dumps(spec, indent=2) + "\n", DOCS / "reference.md": reference_markdown(spec)}


if __name__ == "__main__":
    for path, text in written_files().items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path}", file=sys.stderr)
