"""Gardens and paths: whole sets of blooms, and whole sets of output settings, to switch between.

A garden is OBS's scene collection: every bloom, sprig, source, transition and hotkey you built for
one kind of show (a gaming garden, a just-chatting garden, an event garden). A path is OBS's profile:
where and how the show goes out (Twitch only, multistream, recording only): places to stream to,
the stream encoder and the recording settings.

The two are independent: any path works with any garden. The garden on screen is still one
SceneCollection; the active path's settings live inside it (outputs, recording, stream_encoder), so
the rest of SprigCast doesn't need to know about paths. Switching path swaps those three fields;
switching garden loads another collection and carries the current path's fields into it.

On disk, next to scenes.json:

    gardens.json            which gardens and paths exist, their names, which are in use
    scenes.json             the first garden (the setup from before gardens existed)
    gardens/<id>.json       other gardens (a whole SceneCollection each)
    paths/<id>.json         paths (PathSettings)
    removed/                removed gardens and paths, in case one was removed by mistake

The garden on screen is saved constantly, so its embedded path settings are the newest copy of the
active path: `sync_path` writes them to the path's own file (at start, before a switch, on close).

Nothing here knows about Qt.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from sprigcast.core.looks import FROM_FILE
from sprigcast.core.scene import (
    PART_KIND,
    EncoderSettings,
    Output,
    RecordingSettings,
    SceneCollection,
)

INDEX = "gardens.json"
FIRST_GARDEN_FILE = "scenes.json"
DEFAULT_GARDEN = "Main"
DEFAULT_PATH = "Main"
NAME_MAX = 40


class PathSettings(BaseModel):
    """One path: where the show goes and how it's encoded and recorded."""

    outputs: list[Output] = Field(default_factory=list)
    recording: RecordingSettings = Field(default_factory=RecordingSettings)
    stream_encoder: EncoderSettings = Field(default_factory=EncoderSettings)

    @classmethod
    def of(cls, collection: SceneCollection) -> PathSettings:
        return cls(outputs=[o.model_copy(deep=True) for o in collection.outputs],
                   recording=collection.recording.model_copy(deep=True),
                   stream_encoder=collection.stream_encoder.model_copy(deep=True))

    def apply_to(self, collection: SceneCollection) -> None:
        """Put this path into a garden. Places on a canvas the garden lacks use its first canvas."""
        canvases = [c.id for c in collection.canvases]
        outputs = []
        for output in self.outputs:
            output = output.model_copy(deep=True)
            if output.canvas not in canvases:
                output.canvas = canvases[0]
            outputs.append(output)
        recording = self.recording.model_copy(deep=True)
        recording.canvases = [c for c in recording.canvases if c in canvases] or canvases[:1]
        collection.outputs = outputs
        collection.recording = recording
        collection.stream_encoder = self.stream_encoder.model_copy(deep=True)

    def save(self, path: Path) -> None:
        _write(path, self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> PathSettings:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class Entry(BaseModel):
    id: str
    name: str
    file: str  # relative to the config folder


class Index(BaseModel):
    garden: str = ""
    gardens: list[Entry] = Field(default_factory=list)
    path: str = ""
    paths: list[Entry] = Field(default_factory=list)


class ShelfError(Exception):
    """Something the user asked for can't be done; the message says why, in plain words."""


def _write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


MAX_IMPORT_BYTES = 64 * 1024 * 1024
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def file_name(name: str) -> str:
    """A name that's safe as a Windows file name: no path parts, no reserved names or characters."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().strip(".")
    if not name or name.split(".")[0].casefold() in _RESERVED:
        name = f"SprigCast {name}".strip()
    return name[:80]


def exports_folder(documents: Path) -> Path:
    """Where exports go unless you pick another place: Documents/SprigCast/Exports."""
    return Path(documents) / "SprigCast" / "Exports"


def fit_canvases(garden: SceneCollection, like: SceneCollection) -> SceneCollection:
    """Put a garden on this PC's canvases: each of its canvases goes to the one of the same shape (wide or
    tall) and is resized to it (positions and sizes scale along); a canvas it lacks gets an empty bloom."""
    from sprigcast.core.scene import Scene

    mine = {c.id: c for c in like.canvases}
    wide = next((c.id for c in like.canvases if c.width >= c.height), like.canvases[0].id)
    tall = next((c.id for c in like.canvases if c.height > c.width), None)
    rename: dict[str, str] = {}
    for canvas in garden.canvases:
        target = canvas.id if canvas.id in mine else (tall if canvas.height > canvas.width and tall else wide)
        if target in rename.values():
            raise ShelfError("That garden has two canvases of the same shape.")
        rename[canvas.id] = target
    sizes = {rename[c.id]: (c.width, c.height) for c in garden.canvases}
    placements = {c.id for c in garden.sources if c.kind == PART_KIND}
    for scene in garden.scenes:
        scene.canvas = rename[scene.canvas]
        old, new = sizes[scene.canvas], (mine[scene.canvas].width, mine[scene.canvas].height)
        if old != new:
            k = min(new[0] / old[0], new[1] / old[1])
            for item in scene.items:
                item.x, item.y = item.x * k, item.y * k
                if item.source not in placements:  # a sprig shrinks with its canvas by itself
                    item.scale = item.scale * k
                    item.scale_y = None if item.scale_y is None else item.scale_y * k
                item.bounds_size = (item.bounds_size[0] * k, item.bounds_size[1] * k)
    for folder in garden.folders:
        folder.canvas = rename.get(folder.canvas, wide)
    garden.active_scenes = {rename[c]: s for c, s in garden.active_scenes.items() if c in rename}
    for output in garden.outputs:
        output.canvas = rename.get(output.canvas, wide)
    garden.recording.canvases = [rename[c] for c in garden.recording.canvases if c in rename] or [wide]
    garden.canvases = [c.model_copy(deep=True) for c in like.canvases]
    first = next((s for s in garden.scenes if s.kind == "show"), None)
    for canvas in garden.canvases:
        if canvas.id not in garden.active_scenes:
            bloom = Scene(id=garden.new_id("bloom"), name=first.name if first else "Bloom", canvas=canvas.id,
                          linked_to=first.id if first and first.canvas != canvas.id else None)
            garden.scenes.append(bloom)
            garden.active_scenes[canvas.id] = bloom.id
    try:
        return SceneCollection.model_validate(garden.model_dump())
    except ValueError as exc:
        raise ShelfError("That garden can't be used here.") from exc


def clean_name(name: str) -> str:
    name = " ".join(str(name).split())
    if not name:
        raise ShelfError("Give it a name.")
    return name[:NAME_MAX]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "x"


def blank_garden(like: SceneCollection) -> SceneCollection:
    """A new, empty garden: the same canvases, mic and desktop sound, and one bloom per canvas."""
    from sprigcast.core.scene import Scene

    keep = set(like.global_device_ids())
    sources = [s.model_copy(deep=True) for s in like.sources if s.id in keep]
    col = SceneCollection(canvases=[c.model_copy(deep=True) for c in like.canvases], sources=sources,
                          audio=like.audio.model_copy(deep=True))
    first = None
    for canvas in col.canvases:
        scene = Scene(id=f"bloom-{canvas.id}", name="Bloom", canvas=canvas.id,
                      linked_to=None if first is None else first.id)
        col.scenes.append(scene)
        col.active_scenes[canvas.id] = scene.id
        first = first or scene
    PathSettings.of(like).apply_to(col)
    return col


# Copies SprigCast keeps beside a garden or path: an unreadable one set aside, the one from before sprigs.
_SPARE_FILES = (".tmp", ".broken.json", ".before-sprigs.json", ".bak.json")  # .bak: backups.last_good


class Shelf:
    """The list of gardens and paths in one config folder (see the module notes)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.index_path = self.folder / INDEX
        self.index = Index()
        if self.index_path.exists():
            try:
                self.index = Index.model_validate_json(self.index_path.read_text(encoding="utf-8"))
            except ValueError:
                broken = self.index_path.with_suffix(".broken.json")
                self.index_path.replace(broken)
        self._repair()

    # Setup ---------------------------------------------------------------------------------------

    def _repair(self) -> None:
        """First run with gardens, or a hand-edited index: make sure there's one of each in use."""
        changed = False
        for kind in ("garden", "path"):  # a backup listed as one of them by an older version ("Gaming 2.bak")
            entries = self.index.gardens if kind == "garden" else self.index.paths
            spares = [e for e in entries if e.file.endswith(_SPARE_FILES)
                      and e.id != (self.index.garden if kind == "garden" else self.index.path)]
            for entry in spares:
                entries.remove(entry)
                changed = True
        for kind in ("garden", "path"):  # files the index doesn't list (it was lost or hand-edited) come back
            entries = self.index.gardens if kind == "garden" else self.index.paths
            known = {e.file for e in entries}
            for file in sorted((self.folder / f"{kind}s").glob("*.json")):
                rel = f"{kind}s/{file.name}"
                if rel not in known and not file.name.endswith(_SPARE_FILES):
                    name = file.stem.removeprefix(f"{kind}-").replace("-", " ").strip().capitalize() or kind
                    taken = {e.name.casefold() for e in entries}
                    base, n = name[:NAME_MAX], 2
                    while name.casefold() in taken:
                        name, n = f"{base} {n}", n + 1
                    entries.append(Entry(id=file.stem, name=name, file=rel))
                    changed = True
        if self.index.gardens and not any(g.file == FIRST_GARDEN_FILE for g in self.index.gardens) \
                and (self.folder / FIRST_GARDEN_FILE).exists() and not self.index_path.exists():
            self.index.gardens.insert(0, Entry(id="garden-1", name=DEFAULT_GARDEN, file=FIRST_GARDEN_FILE))
        gardens = [g for g in self.index.gardens if (self.folder / g.file).exists() or g.file == FIRST_GARDEN_FILE]
        if gardens != self.index.gardens:
            self.index.gardens, changed = gardens, True
        if not self.index.gardens:
            self.index.gardens = [Entry(id="garden-1", name=DEFAULT_GARDEN, file=FIRST_GARDEN_FILE)]
            changed = True
        if self.index.garden not in {g.id for g in self.index.gardens}:
            self.index.garden, changed = self.index.gardens[0].id, True
        if not self.index.paths:
            self.index.paths = [Entry(id="path-1", name=DEFAULT_PATH, file="paths/path-1.json")]
            changed = True
        if self.index.path not in {p.id for p in self.index.paths}:
            self.index.path, changed = self.index.paths[0].id, True
        if changed:
            self.save()

    def save(self) -> None:
        _write(self.index_path, self.index.model_dump_json(indent=2))

    # Lookups -------------------------------------------------------------------------------------

    @staticmethod
    def _sorted(entries: list[Entry]) -> list[Entry]:
        return sorted(entries, key=lambda e: e.name.casefold())

    def gardens(self) -> list[Entry]:
        return self._sorted(self.index.gardens)

    def paths(self) -> list[Entry]:
        return self._sorted(self.index.paths)

    def garden(self) -> Entry:
        return next(g for g in self.index.gardens if g.id == self.index.garden)

    def path(self) -> Entry:
        return next(p for p in self.index.paths if p.id == self.index.path)

    def garden_file(self, entry: Entry | None = None) -> Path:
        return self.folder / (entry or self.garden()).file

    def path_file(self, entry: Entry | None = None) -> Path:
        return self.folder / (entry or self.path()).file

    def find(self, kind: str, wanted: str) -> Entry:
        """A garden or path by name (any case) or id."""
        entries = self.index.gardens if kind == "garden" else self.index.paths
        found = next((e for e in entries if e.id == wanted), None) or \
            next((e for e in entries if e.name.casefold() == str(wanted).strip().casefold()), None)
        if found is None:
            raise ShelfError(f"No {kind} called {wanted!r}.")
        return found

    def _check_name(self, kind: str, name: str, skip: str | None = None) -> str:
        name = clean_name(name)
        entries = self.index.gardens if kind == "garden" else self.index.paths
        if any(e.name.casefold() == name.casefold() and e.id != skip for e in entries):
            raise ShelfError(f"There's already a {kind} called {name}.")
        return name

    def _new_entry(self, kind: str, name: str) -> Entry:
        entries = self.index.gardens if kind == "garden" else self.index.paths
        taken = {e.id for e in entries}
        base = f"{kind}-{_slug(name)}"
        entry_id, n = base, 2
        while entry_id in taken or (self.folder / f"{kind}s" / f"{entry_id}.json").exists():
            entry_id, n = f"{base}-{n}", n + 1
        return Entry(id=entry_id, name=name, file=f"{kind}s/{entry_id}.json")

    # Paths ---------------------------------------------------------------------------------------

    def sync_path(self, collection: SceneCollection) -> None:
        """The garden on screen holds the newest copy of the active path: save it to the path's file."""
        PathSettings.of(collection).save(self.path_file())

    def load_path(self, entry: Entry) -> PathSettings:
        file = self.path_file(entry)
        return PathSettings.load(file) if file.exists() else PathSettings()

    def use_path(self, wanted: str, collection: SceneCollection) -> Entry:
        """Switch path: the current one is saved from the collection, the new one put into it."""
        entry = self.find("path", wanted)
        self.sync_path(collection)
        self.load_path(entry).apply_to(collection)
        self.index.path = entry.id
        self.save()
        return entry

    def new_path(self, name: str, collection: SceneCollection, copy: Entry | None = None) -> Entry:
        """A new path: a copy of `copy` (synced first if it's the active one), or one with nothing set."""
        name = self._check_name("path", name)
        entry = self._new_entry("path", name)
        if copy is not None:
            if copy.id == self.index.path:
                self.sync_path(collection)
            settings = self.load_path(copy)
        else:
            settings = PathSettings()
        settings.save(self.path_file(entry))
        self.index.paths.append(entry)
        self.save()
        return entry

    def _other_outputs(self) -> list[Output]:
        out: list[Output] = []
        for entry in self.index.paths:
            if entry.id == self.index.path:
                continue
            try:
                out += self.load_path(entry).outputs
            except (OSError, ValueError):
                pass
        return out

    def taken_output_ids(self) -> set[str]:
        """Place ids used by any path that isn't on screen (each id names its stream key), so a place
        added here never takes another path's key. Places copied into a new path share their keys."""
        return {o.id for o in self._other_outputs()}

    def other_key_refs(self) -> set[str]:
        """Stream keys other paths still use: removing a place here must leave them."""
        return {o.key_ref for o in self._other_outputs() if o.key_ref}

    # Gardens -------------------------------------------------------------------------------------

    def load_garden(self, entry: Entry) -> SceneCollection:
        collection = SceneCollection.load(self.garden_file(entry))
        collection.wrap_blooms()
        return collection

    def new_garden(self, name: str, collection: SceneCollection, copy: Entry | None = None) -> Entry:
        """A new garden: a copy of `copy` (the one on screen is passed in, so it's taken as it is now),
        or an empty one with the same canvases and sound devices."""
        name = self._check_name("garden", name)
        entry = self._new_entry("garden", name)
        if copy is None:
            garden = blank_garden(collection)
        elif copy.id == self.index.garden:
            garden = collection.model_copy(deep=True)
        else:
            garden = self.load_garden(copy)
        garden.save(self.garden_file(entry))
        self.index.gardens.append(entry)
        self.save()
        return entry

    def use_garden(self, wanted: str, collection: SceneCollection) -> tuple[Entry, SceneCollection]:
        """Switch garden: returns the garden to show, with the current path and canvases carried in.
        The caller saves the old collection first and the new one after (it may tidy it more)."""
        entry = self.find("garden", wanted)
        incoming = fit_canvases(self.load_garden(entry), collection)
        PathSettings.of(collection).apply_to(incoming)
        SceneCollection.model_validate(incoming.model_dump())  # still valid with the carried-in settings
        self.index.garden = entry.id
        self.save()
        return entry, incoming

    # Both ----------------------------------------------------------------------------------------

    def rename(self, kind: str, wanted: str, name: str) -> Entry:
        entry = self.find(kind, wanted)
        entry.name = self._check_name(kind, name, skip=entry.id)
        self.save()
        return entry

    def remove(self, kind: str, wanted: str) -> Entry:
        """Remove one that isn't in use; its file moves to removed/ (not deleted)."""
        entry = self.find(kind, wanted)
        entries = self.index.gardens if kind == "garden" else self.index.paths
        if len(entries) == 1:
            raise ShelfError(f"Keep at least one {kind}.")
        if entry.id == (self.index.garden if kind == "garden" else self.index.path):
            raise ShelfError(f"Switch to another {kind} first.")
        file = self.folder / entry.file
        if file.exists():
            bin_ = self.folder / "removed"
            bin_.mkdir(parents=True, exist_ok=True)
            target, n = bin_ / f"{kind}-{_slug(entry.name)}.json", 2
            while target.exists():
                target, n = bin_ / f"{kind}-{_slug(entry.name)}-{n}.json", n + 1
            shutil.move(str(file), str(target))
        self._tidy_after(file)
        entries.remove(entry)
        self.save()
        return entry

    @staticmethod
    def _tidy_after(file: Path) -> None:
        """A removed garden's leftovers beside it: its last-good copy, its dated copies and its bloom pictures
        (the garden itself is kept in removed/). Never raises: leftovers are only clutter."""
        from sprigcast.core import backups

        for spare in (backups.last_good(file), *backups.copies(file)):
            try:
                spare.unlink()
            except OSError:
                pass
        thumbs = file.parent / "thumbs" / file.stem  # main_window._thumbs_dir for a garden in gardens/
        if thumbs.is_dir():
            shutil.rmtree(thumbs, ignore_errors=True)

    def export(self, kind: str, wanted: str, target: Path, collection: SceneCollection) -> None:
        """Write a garden or path to a file of your choosing (the one on screen as it is now)."""
        entry = self.find(kind, wanted)
        target = Path(target)
        self._outside(target)
        if kind == "garden":
            text = (collection if entry.id == self.index.garden else self.load_garden(entry)).model_dump_json(indent=2)
        else:
            if entry.id == self.index.path:
                self.sync_path(collection)
            text = self.load_path(entry).model_dump_json(indent=2)
        _write(Path(target), text)

    def _outside(self, target: Path) -> None:
        try:
            inside = Path(target).resolve().is_relative_to(self.folder.resolve())
        except OSError:
            inside = False
        if inside:
            raise ShelfError("Pick a folder outside SprigCast's own settings folder.")

    def export_pack(self, wanted: str, target: Path, collection: SceneCollection, progress=None) -> list[str]:
        """Write a garden and the files it uses to one .sprigpack file. Returns files that weren't found."""
        from sprigcast.core import pack

        entry = self.find("garden", wanted)
        self._outside(target)
        garden = collection if entry.id == self.index.garden else self.load_garden(entry)
        try:
            return pack.export_pack(garden, Path(target), progress)
        except pack.PackError as exc:
            raise ShelfError(str(exc)) from exc

    def import_pack(self, source: Path, media: Path, progress=None) -> Entry:
        """Add a garden from a .sprigpack: its files are unpacked to media/<name> (a new folder)."""
        from sprigcast.core import pack

        source = Path(source)
        folder = pack.free_folder(media, clean_name(source.stem))
        try:
            garden = pack.read_pack(source, folder, progress)
        except pack.PackError as exc:
            raise ShelfError(str(exc)) from exc
        return self._add("garden", source.stem, garden)

    def import_file(self, kind: str, source: Path) -> Entry:
        """Add a garden or path from an exported file; named after the file (with a number if taken)."""
        source = Path(source)
        try:
            if source.stat().st_size > MAX_IMPORT_BYTES:
                raise ShelfError(f"{source.name} is too big to be a SprigCast {kind}.")
            text = source.read_text(encoding="utf-8-sig")
            if kind == "garden":
                data: BaseModel = SceneCollection.model_validate_json(text)
            else:
                data = PathSettings.model_validate_json(text)
        except (OSError, ValueError) as exc:
            raise ShelfError(f"{source.name} isn't a SprigCast {kind}.") from exc
        return self._add(kind, source.stem, data)

    def add_garden(self, name: str, garden: SceneCollection) -> Entry:
        """Add a garden made elsewhere (converted from OBS, Streamlabs or Meld); the name gets a number if taken."""
        return self._add("garden", name, garden)

    def _add(self, kind: str, name: str, data: BaseModel) -> Entry:
        base = clean_name(name)
        name, n = base, 2
        entries = self.index.gardens if kind == "garden" else self.index.paths
        while any(e.name.casefold() == name.casefold() for e in entries):
            name, n = f"{base[:NAME_MAX - 4]} {n}", n + 1
        entry = self._new_entry(kind, name)
        _write(self.folder / entry.file, data.model_dump_json(indent=2))
        entries.append(entry)
        self.save()
        return entry


# Missing files ------------------------------------------------------------------------------------

TUBER_PICTURES = ("idle_picture", "talk_picture", "half_picture", "yell_picture", "blink_picture", "sleep_picture")
FILE_KEYS = {"image": ("path",), "media": ("path",), "text": ("file",), "browser": ("file",),
             "pngtuber": TUBER_PICTURES}


def file_holders(source) -> list[tuple[dict, str]]:
    """Each file setting of a source as (the dict holding it, its key): a PNGtuber's own expressions keep
    their pictures one level in (expression_pictures: name -> {"idle_picture": ...})."""
    out = [(source.settings, key) for key in FILE_KEYS.get(source.kind, ())]
    if source.kind == "pngtuber":
        own = source.settings.get("expression_pictures")
        for pictures in (own.values() if isinstance(own, dict) else ()):
            if isinstance(pictures, dict):
                out += [(pictures, key) for key in TUBER_PICTURES if key in pictures]
    return out


def _usable(value: str) -> bool:
    value = value.strip()
    return bool(value) and not value.startswith("art:") and "://" not in value


def missing_files(collection: SceneCollection) -> list[str]:
    """Files the garden points at that aren't there any more (pictures, videos, pages, looks, stingers)."""
    return absent(wanted_files(collection))


def absent(wanted: list[str]) -> list[str]:
    """The usable paths in `wanted` that aren't there, once each. Reads the disk: a worker's job."""
    out: list[str] = []
    for value in wanted:
        if _usable(value) and value not in out and not Path(value).exists():
            out.append(value)
    return out


def wanted_files(collection: SceneCollection) -> list[str]:
    """Every file the garden points at (no disk reads)."""
    wanted: list[str] = []
    for source in collection.sources:
        for holder, key in file_holders(source):
            wanted.append(str(holder.get(key, "") or ""))
        for flt in source.filters:
            if flt.kind == "lut" and flt.settings.get("look") == FROM_FILE:
                wanted.append(str(flt.settings.get("file", "") or ""))
    for transition in [collection.transition, *(r.transition for r in collection.transition_rules)]:
        wanted += [transition.stinger.path, transition.stinger.vertical_path, transition.wipe_image]
    return wanted


def replace_file(collection: SceneCollection, old: str, new: str) -> int:
    """Point everything that used `old` at `new`. Returns how many settings changed."""
    count = 0
    for source in collection.sources:
        for holder, key in file_holders(source):
            if holder.get(key) == old:
                holder[key] = new
                count += 1
        for flt in source.filters:
            if flt.kind == "lut" and flt.settings.get("file") == old:
                flt.settings["file"] = new
                count += 1
    for transition in [collection.transition, *(r.transition for r in collection.transition_rules)]:
        for obj, attr in ((transition.stinger, "path"), (transition.stinger, "vertical_path"), (transition, "wipe_image")):
            if getattr(obj, attr) == old:
                setattr(obj, attr, new)
                count += 1
    return count


def find_in_folder(missing: list[str], folder: Path, max_files: int = 50_000) -> dict[str, str]:
    """Look through a folder (and the folders inside it) for files with the same names as missing ones.

    Returns {missing path: found path}. A name found in more than one place takes the one whose folder
    names match the old path best. Stops after max_files files, so a whole drive can't hang SprigCast."""
    import os

    wanted: dict[str, list[str]] = {}
    for old in missing:
        name = Path(old.replace("\\", "/")).name.casefold()
        if name:
            wanted.setdefault(name, []).append(old)
    found: dict[str, list[Path]] = {}
    seen = 0
    for root, _dirs, files in os.walk(folder):
        for file in files:
            seen += 1
            key = file.casefold()
            if key in wanted:
                found.setdefault(key, []).append(Path(root) / file)
        if seen >= max_files:
            break

    def score(old: str, new: Path) -> int:
        old_parts = [p.casefold() for p in Path(old.replace("\\", "/")).parts[:-1]]
        return sum(1 for p in new.parts[:-1] if p.casefold() in old_parts)

    out: dict[str, str] = {}
    for key, olds in wanted.items():
        for old in olds:
            if key in found:
                out[old] = str(max(found[key], key=lambda new, o=old: score(o, new)))
    return out

