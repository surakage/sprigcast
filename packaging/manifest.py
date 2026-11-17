"""Write the list of every file this version installs, so the next update can delete what it no longer ships.

Usage: manifest.py <python home>   (writes <python home>/sprigcast-files.txt)

OBS's updater does the same with each release's removed_files: a file taken out of the app (a library dropped,
like onnxruntime in #434, or a retired plugin) is deleted on update instead of staying on the PC for good. The
installer (packaging/sprigcast.iss, CurStepChanged) reads the old list before copying and the new one after,
and deletes what's only in the old one. Paths are relative to the python folder, with backslashes.
"""
from __future__ import annotations

import sys
from pathlib import Path

NAME = "sprigcast-files.txt"


def files(home: Path) -> list[str]:
    out = []
    for path in home.rglob("*"):
        if path.is_file() and path.name != NAME and "__pycache__" not in path.parts:
            out.append(str(path.relative_to(home)).replace("/", "\\"))
    return sorted(out, key=str.lower)


def main(home: Path) -> None:
    names = files(home)
    (home / NAME).write_text("\n".join(names) + "\n", encoding="utf-8")
    print(f"{len(names)} files listed in {NAME}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
