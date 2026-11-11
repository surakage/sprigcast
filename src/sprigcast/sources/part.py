"""A part placed inside another scene. The compositor draws the part itself; this source only reports its size."""

from __future__ import annotations

from sprigcast.core.scene import PART_KIND
from sprigcast.core.source import Source, register


@register(PART_KIND)
class PartSource(Source):
    label = "Part"
    defaults = {"scene": ""}

    def __init__(self, source_id, settings):
        super().__init__(source_id, settings)
        self.part_size: tuple[int, int] = (0, 0)  # set by the engine from the part's canvas

    @property
    def size(self) -> tuple[int, int]:
        return self.part_size
