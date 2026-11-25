"""Garden packs: a garden together with the files it uses, so it moves to another PC complete.

A pack is a zip file ending in .sprigpack:

    garden.json            the garden, with each file it uses written as "pack:<n>/<name>"
    files/<n>/<name>       the files themselves (a page's folder comes along whole, so its pictures and
                           scripts still load; a slide show's pictures come along as a folder)

Unpacking writes only under the folder it's given (Documents/SprigCast/Media/<garden>), never anywhere a
name in the zip points at, and stops at a size limit so a broken or hostile file can't fill the disk.
"""

from __future__ import annotations

import os
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from sprigcast.core.gardens import file_holders
from sprigcast.core.looks import FROM_FILE
from sprigcast.core.scene import SceneCollection

PACK_SUFFIX = ".sprigpack"
PACK_PREFIX = "pack:"
MAX_UNPACKED_BYTES = 8 * 1024 ** 3  # 8 GB: more than any sane set of overlays
MAX_FILES = 20_000
PAGE_FOLDER_BYTES = 200 * 1024 ** 2  # a page's folder comes along whole when it's smaller than this
PAGE_FOLDER_FILES = 2_000
STORED = {".mp4", ".mkv", ".mov", ".webm", ".mp3", ".ogg", ".m4a", ".aac", ".flac", ".png", ".jpg", ".jpeg",
          ".webp", ".gif", ".avif", ".zip", ".woff", ".woff2"}  # already squeezed: zipping again only costs time
FOLDER_KEYS = {"slide_show": ("folder",)}
MODEL_KEYS = {"pngtuber": ("folder",)}  # a folder whose subfolders come too (a PNGtuber's expressions)

Progress = Callable[[int, int], None]


class PackError(Exception):
    """The pack can't be written or read; the message says why in plain words."""


def _usable(value: str) -> bool:
    value = (value or "").strip()
    return bool(value) and not value.startswith(("art:", PACK_PREFIX)) and "://" not in value


def _slots(collection: SceneCollection) -> list[tuple[object, str, str]]:
    """Every file or folder setting as (holder, key, "file"|"page"|"folder"|"model"); holder is a dict or an object."""
    out: list[tuple[object, str, str]] = []
    for source in collection.sources:
        for holder, key in file_holders(source):
            out.append((holder, key, "page" if source.kind == "browser" else "file"))
        for key in FOLDER_KEYS.get(source.kind, ()):
            out.append((source.settings, key, "folder"))
        for key in MODEL_KEYS.get(source.kind, ()):
            out.append((source.settings, key, "model"))
        for flt in source.filters:
            if flt.kind == "lut" and flt.settings.get("look") == FROM_FILE:
                out.append((flt.settings, "file", "file"))
    for transition in [collection.transition, *(r.transition for r in collection.transition_rules)]:
        out += [(transition.stinger, "path", "file"), (transition.stinger, "vertical_path", "file"),
                (transition, "wipe_image", "file")]
    return out


def _get(holder: object, key: str) -> str:
    value = holder.get(key, "") if isinstance(holder, dict) else getattr(holder, key, "")
    return str(value or "")


def _set(holder: object, key: str, value: str) -> None:
    if isinstance(holder, dict):
        holder[key] = value
    else:
        setattr(holder, key, value)


def _safe_name(name: str) -> str:
    from sprigcast.core.gardens import file_name

    stem, suffix = os.path.splitext(name)
    return (file_name(stem) or "file")[:60] + "".join(c for c in suffix if c.isalnum() or c == ".")[:12]


def _page_files(page: Path) -> tuple[Path, list[Path]]:
    """The files a local page needs: its whole folder when that's small, else just the page."""
    folder = page.parent
    files: list[Path] = []
    total = 0
    if folder != Path(folder.anchor):  # never a whole drive
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for name in names:
                file = Path(root) / name
                try:
                    total += file.stat().st_size
                except OSError:
                    continue
                files.append(file)
                if total > PAGE_FOLDER_BYTES or len(files) > PAGE_FOLDER_FILES:
                    return folder, [page]
        if page in files:
            return folder, files
    return folder, [page]


def _folder_files(folder: Path, subfolders: bool = False) -> list[Path]:
    """The folder's files (and, asked, those of the folders right inside it)."""
    try:
        entries = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        return []
    files = [p for p in entries if p.is_file()]
    if subfolders:
        for sub in entries:
            if sub.is_dir() and not sub.name.startswith("."):
                files += _folder_files(sub)
    return files


def export_pack(collection: SceneCollection, target: Path, progress: Progress | None = None) -> list[str]:
    """Write the garden and every file it uses to `target`. Returns the files that weren't found (left out)."""
    garden = collection.model_copy(deep=True)
    plan: list[tuple[str, Path]] = []  # (name in the zip, file on disk)
    done: dict[tuple[str, str], str] = {}  # (kind, old value) -> new value
    missing: list[str] = []
    for holder, key, kind in _slots(garden):
        value = _get(holder, key)
        if not _usable(value):
            continue
        if (kind, value) in done:
            _set(holder, key, done[(kind, value)])
            continue
        n = len(done) + 1
        path = Path(value)
        if kind in ("folder", "model"):
            files = _folder_files(path, kind == "model") if path.is_dir() else []
            if not files:
                missing.append(value)
                continue
            top = _safe_name(path.name or 'folder')
            new = f"{PACK_PREFIX}{n}/{top}"
            plan += [(f"files/{n}/{top}/" + "/".join(_safe_name(p) for p in f.relative_to(path).parts), f)
                     for f in files]
        elif not path.is_file():
            missing.append(value)
            continue
        elif kind == "page":
            folder, files = _page_files(path)
            rel = path.relative_to(folder)
            new = f"{PACK_PREFIX}{n}/" + "/".join(_safe_name(p) for p in rel.parts)
            plan += [(f"files/{n}/" + "/".join(_safe_name(p) for p in f.relative_to(folder).parts), f) for f in files]
        else:
            new = f"{PACK_PREFIX}{n}/{_safe_name(path.name)}"
            plan.append((f"files/{n}/{_safe_name(path.name)}", path))
        done[(kind, value)] = new
        _set(holder, key, new)

    total = sum(_size(f) for _, f in plan)
    if total > MAX_UNPACKED_BYTES:
        raise PackError("The files in this garden add up to more than 8 GB, too big for one pack.")
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    sent = 0
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            zf.writestr("garden.json", garden.model_dump_json(indent=2))
            for arc, file in plan:
                kind = zipfile.ZIP_STORED if file.suffix.casefold() in STORED else zipfile.ZIP_DEFLATED
                try:
                    zf.write(file, arc, compress_type=kind)
                except OSError:
                    missing.append(str(file))
                sent += _size(file)
                if progress:
                    progress(sent, total)
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    return list(dict.fromkeys(missing))


def _size(file: Path) -> int:
    try:
        return file.stat().st_size
    except OSError:
        return 0


def is_pack(path: Path) -> bool:
    """A zip holding garden.json (whatever its name ends in), or anything named .sprigpack."""
    if Path(path).suffix.casefold() == PACK_SUFFIX:
        return True
    try:
        with zipfile.ZipFile(path) as zf:
            return "garden.json" in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def _member_path(name: str) -> PurePosixPath | None:
    """files/<n>/<parts> as a safe relative path, or None for anything else (absolute, .., drive, odd)."""
    p = PurePosixPath(name.replace("\\", "/"))
    parts = p.parts
    if len(parts) < 3 or parts[0] != "files" or not parts[1].isdigit():
        return None
    if any(part in ("", ".", "..") or ":" in part for part in parts) or name.startswith("/"):
        return None
    return PurePosixPath(*parts[1:])


def read_pack(source: Path, media: Path, progress: Progress | None = None) -> SceneCollection:
    """Unpack a garden pack: its files go under `media` (a new, empty folder) and the garden points at them."""
    source, media = Path(source), Path(media)
    try:
        zf = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackError(f"{source.name} isn't a SprigCast garden pack.") from exc
    with zf:
        try:
            garden = SceneCollection.model_validate_json(zf.read("garden.json"))
        except (KeyError, ValueError, zipfile.BadZipFile) as exc:
            raise PackError(f"{source.name} isn't a SprigCast garden pack.") from exc
        members = [(info, _member_path(info.filename)) for info in zf.infolist() if not info.is_dir()]
        members = [(info, rel) for info, rel in members if rel is not None]
        root = unzip(zf, members, media, source.name, progress)
    for holder, key, _kind in _slots(garden):
        value = _get(holder, key)
        if value.startswith(PACK_PREFIX):
            rel = _member_path("files/" + value[len(PACK_PREFIX):])
            _set(holder, key, str(root / Path(*rel.parts)) if rel else "")
    return garden


def unzip(zf: zipfile.ZipFile, members: list[tuple[zipfile.ZipInfo, PurePosixPath]], media: Path, label: str,
          progress: Progress | None = None) -> Path:
    """Write the chosen members under `media` (made here), each at its safe relative path. Checks the size
    and free space first, never writes outside `media`, and removes it again if anything goes wrong."""
    total = sum(info.file_size for info, _ in members)
    if total > MAX_UNPACKED_BYTES or len(members) > MAX_FILES:
        raise PackError(f"{label} is too big to unpack.")
    if shutil.disk_usage(_existing(media)).free < total + 256 * 1024 ** 2:
        raise PackError("There isn't enough free disk space to unpack it.")
    media.mkdir(parents=True, exist_ok=True)
    root = media.resolve()
    written = 0
    try:
        for info, rel in members:
            out = (root / Path(*rel.parts)).resolve()
            if not out.is_relative_to(root) or out == root:
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            left = info.file_size
            with zf.open(info) as src, open(out, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    left -= len(chunk)
                    if left < 0:  # the zip lied about the size
                        raise PackError(f"{label} is damaged.")
                    dst.write(chunk)
            written += info.file_size
            if progress:
                progress(written, total)
    except (OSError, zipfile.BadZipFile, EOFError) as exc:
        shutil.rmtree(media, ignore_errors=True)
        raise PackError(f"Couldn't unpack {label}: {getattr(exc, 'strerror', None) or exc}") from exc
    except PackError:
        shutil.rmtree(media, ignore_errors=True)
        raise
    return root


def safe_member(name: str) -> PurePosixPath | None:
    """A zip member name as a safe relative path, or None (absolute, .., drive letters, empty parts)."""
    if name.startswith(("/", "\\")):
        return None
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts or any(part in ("", ".", "..") or ":" in part for part in parts):
        return None
    return PurePosixPath(*parts)


def _existing(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def media_folder(documents: Path) -> Path:
    """Where unpacked files go: Documents/SprigCast/Media."""
    return Path(documents) / "SprigCast" / "Media"


def free_folder(parent: Path, name: str) -> Path:
    """parent/name, or parent/name 2, 3… when that's taken."""
    from sprigcast.core.gardens import file_name

    base = file_name(name) or "Garden"
    folder, n = Path(parent) / base, 2
    while folder.exists():
        folder, n = Path(parent) / f"{base} {n}", n + 1
    return folder
