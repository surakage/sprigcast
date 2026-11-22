"""Print one version's section of CHANGELOG.md, for its GitHub release (and the in-app update pop-up).

    python packaging/release_notes.py 0.2.1 notes.md   (or no file name: print it)

Fails if the CHANGELOG has no section for that version, so a release never goes out without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def _shown(version: str) -> str:
    """Same rule as sprigcast.display_version (kept here so this runs without the app on the path)."""
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return version
    major, minor, patch = (int(part) for part in parts)
    if major >= 1 and minor == 0 and patch == 0:
        return f"Version {major}"
    return f"Beta {major}.{minor}" + (f".{patch}" if patch else "")


def section(text: str, version: str) -> str | None:
    """The lines under "## <version>" or "## <shown name>" ("## Beta 0.3" for 0.3.0), with or without a note
    after it, up to the next "## " heading."""
    lines = text.splitlines()
    names = {version, _shown(version)}
    head = re.compile(r"^## (" + "|".join(re.escape(n) for n in names) + r")(\s|$)")
    for start, line in enumerate(lines):
        if head.match(line):
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            return "\n".join(lines[start + 1:end]).strip()
    return None


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 2
    notes = section(CHANGELOG.read_text(encoding="utf-8"), argv[1].removeprefix("v"))
    if not notes:
        print(f"CHANGELOG.md has no section for {argv[1]}", file=sys.stderr)
        return 1
    if len(argv) == 3:
        Path(argv[2]).write_text(notes + "\n", encoding="utf-8")
    else:
        print(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
