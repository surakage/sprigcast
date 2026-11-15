"""The plugin store: optional plugins you can add to SprigCast from Settings → Plugins.

Store plugins ship with SprigCast in sprigcast/store/ but stay switched off until you install one:
installing copies its file into your plugins folder and loads it straight away, so nothing in the
core app pays for plugins you don't use. Removing deletes the copy (what it added leaves at the next
start).

Each store plugin starts with a PLUGIN dict, read without running the file:

    PLUGIN = {"id": "slide_show", "name": "Slide show", "version": 1,
              "about": "Plays every picture in a folder, one after another.", "adds": "Source"}
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


def read_header(path: Path) -> dict | None:
    """The PLUGIN dict at the top of a plugin file, or None. Never runs the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, ValueError):
        return None
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
                               adds=str(head.get("adds", "")), file=path))
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


def refresh_installed(plugins_dir: Path | None) -> list[str]:
    """Bring store plugins you added up to the version that ships with this SprigCast (before they
    load, at start). Your own plugins and ones you never added are left alone. Returns the ids updated."""
    if plugins_dir is None or not plugins_dir.is_dir():
        return []
    updated = []
    for plugin in catalog():
        have = installed_version(plugins_dir, plugin.id)
        if have is not None and have < plugin.version:
            shutil.copyfile(plugin.file, installed_path(plugins_dir, plugin.id))
            log.info("plugin store: updated %s v%d -> v%d", plugin.id, have, plugin.version)
            updated.append(plugin.id)
    return updated


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
