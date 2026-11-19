"""Plugins: Python files that add new source kinds.

Put a `.py` file in the plugins folder (Tools → Plugin store → Your plugins → Open plugins folder). At startup SprigCast
imports every file there, and any installed package that declares a `sprigcast.plugins`
entry point. A plugin registers sources exactly like the built-in ones:

    from sprigcast.core.source import Source, register

    @register("my_kind")
    class MySource(Source):
        label = "My source"              # shown under Sources → + → Plugins
        defaults = {"text": "hello"}     # editable in the source's Settings dialog
        ...

A plugin that fails to import is skipped and listed in the status bar; it never stops the app. The sources a plugin
of your own adds are drawn in the plugin helper process (app/plugin_helper.py), so one that raises, hangs or
crashes can't take SprigCast with it; keep a source's work inside the source (it can't reach the window from there).
Plugins run with the same rights as SprigCast itself, so only install ones you trust.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "sprigcast.plugins"
_IN_HELPER = False  # set in the plugin helper process itself (app/plugin_helper.py serve), which runs them


@dataclass
class PluginReport:
    loaded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # name -> error


def load_plugins(folder: Path | None) -> PluginReport:
    import sprigcast.sources  # noqa: F401  built-in kinds first, so a plugin can't silently replace them unnoticed

    report = PluginReport()
    if folder is not None and folder.is_dir():
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            error = load_plugin_file(path)
            if error is None:
                report.loaded.append(path.name)
            else:
                report.failed[path.name] = error
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ep.load()
            report.loaded.append(ep.name)
        except Exception as exc:
            log.warning("plugin %s failed to load: %s", ep.name, exc)
            report.failed[ep.name] = f"{exc.__class__.__name__}: {exc}"
    return report


def load_plugin_file(path: Path) -> str | None:
    """Import one plugin file (again, if it was loaded before). Returns the error, or None."""
    import sprigcast.sources  # noqa: F401

    name = f"sprigcast_plugin_{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        log.warning("plugin %s failed to load: %s", path.name, exc)
        return f"{exc.__class__.__name__}: {exc}"
    if not _IN_HELPER:
        try:
            from sprigcast.app import plugin_helper

            plugin_helper.adopt(path, name)  # your own plugin's sources run in the helper process
        except Exception:  # noqa: BLE001 - they run here, as before
            log.warning("plugin %s: its sources run in SprigCast (the helper couldn't take them)", path.name,
                        exc_info=True)
    return None


EXAMPLE = '''"""Example SprigCast plugin: a countdown timer source. Edit or copy it to make your own.

Add it with Sources → + → Plugins → Example countdown. Double-click it to change the minutes and label.
(For a countdown on stream, add Countdown from Tools → Plugin store: this one shows how a plugin is made.)
"""

import time

from sprigcast.core.source import register
from sprigcast.sources.text import TextSource


@register("countdown")
class CountdownSource(TextSource):
    label = "Example countdown"
    defaults = {**TextSource.defaults, "text": "", "minutes": 5, "label": "Starting in "}

    def __init__(self, source_id, settings):
        super().__init__(source_id, settings)
        self._started = time.monotonic()

    def update(self, settings):
        if settings.get("minutes") != self.settings.get("minutes"):
            self._started = time.monotonic()  # changing the length restarts the countdown
        super().update(settings)

    def _current_text(self):
        left = max(0, int(float(self.settings.get("minutes", 5)) * 60 - (time.monotonic() - self._started)))
        return f"{self.settings.get('label', '')}{left // 60}:{left % 60:02d}"
'''


_NEW_DOC = 'Add it with Sources → + → Plugins → Example countdown. Double-click it to change the minutes and label.\n(For a countdown on stream, add Countdown from Tools → Plugin store: this one shows how a plugin is made.)'
_OLD_DOC = 'Add it with Sources → + → Plugins → Countdown. Double-click it to change the minutes and label.'
_NEW_LAB = '    label = "Example countdown"\n    defaults = {**TextSource.defaults, "text": "", "minutes": 5, "label": "Starting in "}'
_OLD_LAB = '    label = "Countdown"\n    defaults = {**TextSource.defaults, "text": "", "minutes": 5, "label": "Starting in "}'
# The example as it was before the Countdown store plugin: listed as "Countdown" too, so an untouched copy is
# brought up to date (it keeps its kind, so a countdown made from it carries on).
OLD_EXAMPLE = EXAMPLE.replace(_NEW_DOC, _OLD_DOC).replace(_NEW_LAB, _OLD_LAB)


def ensure_example(folder: Path) -> None:
    """Create the plugins folder with an example the first time (and bring an untouched old one up to date)."""
    folder.mkdir(parents=True, exist_ok=True)
    example = folder / "countdown_example.py"
    if not any(folder.glob("*.py")) and not example.exists():
        example.write_text(EXAMPLE, encoding="utf-8")
    elif example.exists():
        try:
            if example.read_text(encoding="utf-8") == OLD_EXAMPLE:
                example.write_text(EXAMPLE, encoding="utf-8")
        except (OSError, ValueError):
            pass
