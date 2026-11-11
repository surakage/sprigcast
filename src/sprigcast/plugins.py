"""Plugins: Python files that add new source kinds.

Put a `.py` file in the plugins folder (File → Open plugins folder). At startup SprigCast
imports every file there, and any installed package that declares a `sprigcast.plugins`
entry point. A plugin registers sources exactly like the built-in ones:

    from sprigcast.core.source import Source, register

    @register("my_kind")
    class MySource(Source):
        label = "My source"              # shown in the "+ More" menu
        defaults = {"text": "hello"}     # editable in the source's Settings dialog
        ...

A plugin that fails to import is skipped and listed in the status bar; it never stops the app.
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
            name = f"sprigcast_plugin_{path.stem}"
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                report.loaded.append(path.name)
            except Exception as exc:
                sys.modules.pop(name, None)
                log.warning("plugin %s failed to load: %s", path.name, exc)
                report.failed[path.name] = f"{exc.__class__.__name__}: {exc}"
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            ep.load()
            report.loaded.append(ep.name)
        except Exception as exc:
            log.warning("plugin %s failed to load: %s", ep.name, exc)
            report.failed[ep.name] = f"{exc.__class__.__name__}: {exc}"
    return report


EXAMPLE = '''"""Example SprigCast plugin: a countdown timer source. Edit or copy it to make your own.

Add it with Sources → + More → Countdown. Its settings (minutes, label) appear in Settings….
"""

import time

from sprigcast.core.source import register
from sprigcast.sources.text import TextSource


@register("countdown")
class CountdownSource(TextSource):
    label = "Countdown"
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


def ensure_example(folder: Path) -> None:
    """Create the plugins folder with an example the first time."""
    folder.mkdir(parents=True, exist_ok=True)
    example = folder / "countdown_example.py"
    if not any(folder.glob("*.py")) and not example.exists():
        example.write_text(EXAMPLE, encoding="utf-8")
