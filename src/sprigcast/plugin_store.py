"""The plugin store: optional plugins you can add to SprigCast from Settings → Plugins.

Store plugins ship with SprigCast in sprigcast/store/ but stay switched off until you install one:
installing copies its file into your plugins folder and loads it straight away, so nothing in the
core app pays for plugins you don't use. Removing deletes the copy (what it added leaves at the next
start).

Each store plugin starts with a PLUGIN dict, read without running the file:

    PLUGIN = {"id": "slide_show", "name": "Slide show", "version": 1,
              "about": "Plays every picture in a folder, one after another.", "adds": "Source",
              "new": "What changed in this version, in a line (shown when it updates)."}
"""

from __future__ import annotations

import ast
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from sprigcast.plugins import PluginReport, load_plugin_file

log = logging.getLogger(__name__)

STORE_DIR = Path(__file__).parent / "store"


@dataclass(frozen=True)
class StorePlugin:
    id: str
    name: str
    version: int
    about: str
    adds: str
    file: Path
    new: str = ""  # what changed in this version, said when it updates


# Store plugins brought up to date at this start (refresh_installed), for the "updated" note once the window is up.
updated_at_start: list[str] = []
# Updates asked for while live or recording, held until both stop (Settings → Plugins).
waiting: dict[str, StorePlugin] = {}


_headers: dict[str, tuple[tuple[int, int], dict | None]] = {}  # path -> ((mtime, size), header), see read_header


def read_header(path: Path) -> dict | None:
    """The PLUGIN dict at the top of a plugin file, or None. Never runs the file.

    Only the PLUGIN lines are parsed, and the answer is kept until the file changes: parsing whole store
    plugins every time the list was read froze the window 336 ms at start on the PC (2026-11-16)."""
    try:
        stat = path.stat()
    except OSError:
        return None
    key, stamp = str(path), (stat.st_mtime_ns, stat.st_size)
    known = _headers.get(key)
    if known is not None and known[0] == stamp:
        return dict(known[1]) if known[1] is not None else None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    head = _parse_header(text, str(path))
    _headers[key] = (stamp, head)
    return dict(head) if head is not None else None


def _parse_header(text: str, filename: str) -> dict | None:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("PLUGIN") and "=" in line), None)
    if start is not None:  # the assignment alone: a few lines, however big the plugin
        for end in range(start + 1, min(len(lines), start + 200) + 1):
            try:
                tree = ast.parse("\n".join(lines[start:end]), filename=filename)
            except SyntaxError:
                continue
            return _header_in(tree)
    try:
        return _header_in(ast.parse(text, filename=filename))  # written some other way: the whole file
    except (SyntaxError, ValueError):
        return None


def _header_in(tree) -> dict | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PLUGIN" for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, dict) and value.get("id") else None
    return None


def catalog(store_dir: Path = STORE_DIR) -> list[StorePlugin]:
    out = []
    for path in sorted(store_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        head = read_header(path)
        if head is None:
            continue
        out.append(StorePlugin(id=str(head["id"]), name=str(head.get("name", head["id"])),
                               version=int(head.get("version", 1)), about=str(head.get("about", "")),
                               adds=str(head.get("adds", "")), file=path, new=str(head.get("new", ""))))
    return sorted(out, key=lambda p: p.name.lower())


def installed_path(plugins_dir: Path, plugin_id: str) -> Path:
    return plugins_dir / f"{plugin_id}.py"


def installed_version(plugins_dir: Path | None, plugin_id: str) -> int | None:
    if plugins_dir is None:
        return None
    path = installed_path(plugins_dir, plugin_id)
    if not path.exists():
        return None
    head = read_header(path) or {}
    return int(head.get("version", 0))


def is_store_file(path: Path) -> bool:
    """A plugin in your folder that came from the store (so the store's list shows it, not "your plugins")."""
    head = read_header(path)
    return head is not None and any(p.id == head["id"] for p in catalog())


def install(plugins_dir: Path, plugin: StorePlugin, report: PluginReport | None = None) -> str | None:
    """Copy the plugin into the plugins folder and load it now. Returns an error, or None when it worked."""
    plugins_dir.mkdir(parents=True, exist_ok=True)
    target = installed_path(plugins_dir, plugin.id)
    updating = target.exists()
    shutil.copyfile(plugin.file, target)
    log.info("plugin store: %s %s v%d", "updated" if updating else "installed", plugin.id, plugin.version)
    error = load_plugin_file(target)
    if report is not None:
        report.failed.pop(target.name, None)
        if error is None:
            if target.name not in report.loaded:
                report.loaded.append(target.name)
        else:
            report.failed[target.name] = error
    return error


# Store plugins taken out of SprigCast (see docs/REVISIT.md): an added copy is deleted at start, so it never loads.
RETIRED = ("background_remover",)


def refresh_installed(plugins_dir: Path | None) -> list[str]:
    """Bring store plugins you added up to the version that ships with this SprigCast (before they
    load, at start), and delete retired ones. Your own plugins and ones you never added are left alone.
    Returns the ids updated."""
    if plugins_dir is None or not plugins_dir.is_dir():
        return []
    for plugin_id in RETIRED:
        target = installed_path(plugins_dir, plugin_id)
        if target.exists():
            try:
                target.unlink()
                log.info("plugin store: removed %s (retired from SprigCast)", plugin_id)
            except OSError as exc:
                log.warning("plugin store: couldn't remove retired %s: %s", plugin_id, exc)
    updated = []
    for plugin in catalog():
        have = installed_version(plugins_dir, plugin.id)
        if have is not None and have < plugin.version:
            shutil.copyfile(plugin.file, installed_path(plugins_dir, plugin.id))
            log.info("plugin store: updated %s v%d -> v%d", plugin.id, have, plugin.version)
            updated.append(plugin.id)
    updated_at_start[:] = updated
    return updated


def update_note(plugin_ids: list[str]) -> tuple[str, str] | None:
    """The note after store plugins updated: ("Death count updated", what's new), or a count with the names."""
    plugins = {p.id: p for p in catalog()}
    found = [plugins[i] for i in plugin_ids if i in plugins]
    if not found:
        return None
    if len(found) == 1:
        return f"{found[0].name} updated", found[0].new
    names = sorted(p.name for p in found)
    return f"{len(found)} plugins updated", ", ".join(names[:-1]) + " and " + names[-1]


def remove(plugins_dir: Path, plugin_id: str, report: PluginReport | None = None) -> None:
    target = installed_path(plugins_dir, plugin_id)
    if target.exists():
        target.unlink()
        log.info("plugin store: removed %s", plugin_id)
    from sprigcast.core.source import unregister_module

    unregister_module(f"sprigcast_plugin_{plugin_id}")  # leaves the + → Plugins menu now
    import sys

    sys.modules.pop(f"sprigcast_plugin_{plugin_id}", None)  # its hotkeys and menu entries go quiet too
    from sprigcast.core.filters import unregister_filters

    unregister_filters(f"sprigcast_plugin_{plugin_id}")
    from sprigcast.control.native import unregister_actions

    unregister_actions(f"sprigcast_plugin_{plugin_id}")
    if report is not None:
        if target.name in report.loaded:
            report.loaded.remove(target.name)
        report.failed.pop(target.name, None)
