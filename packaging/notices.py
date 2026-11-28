"""Writes SprigCast's license and the licenses of everything the installer bundles.

Run by build_windows.ps1 with the bundled Python, after prune.py, so the list matches what is shipped:

    python notices.py <build\\python> <repo root>

It writes, inside the installed folder:
- LICENSE.txt: SprigCast's own license (GPL 3.0 or later).
- THIRD-PARTY-NOTICES.txt: every bundled Python package with its version, license and license texts (read from
  its installed metadata), plus the parts that aren't Python packages: Python itself, FFmpeg (inside PyAV),
  Qt and Chromium (inside PySide6) and the H.264/AAC page engine.

SprigCast's About box links to THIRD-PARTY-NOTICES.txt.
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys
from pathlib import Path

NOTICES = "THIRD-PARTY-NOTICES.txt"
LICENSE = "LICENSE.txt"
LICENSE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT")
RULE = "=" * 78

# Parts that come inside a Python package or beside it, with where their source and terms live.
EXTRA = [
    ("Python", "Python Software Foundation License",
     "The Python runtime SprigCast runs on (python-build-standalone). Its license text follows when the "
     "build has one, and is at https://docs.python.org/3/license.html."),
    ("FFmpeg (inside PyAV)", "LGPL 2.1 or later, with GPL parts depending on the build",
     "Encoding, decoding and sending video. Shipped unchanged inside PyAV's av.libs folder. Source: "
     "https://ffmpeg.org/download.html and https://github.com/PyAV-Org/pyav-ffmpeg. SprigCast's native engine "
     "loads these same libraries at run time and doesn't ship a copy of its own."),
    ("Qt 6 (inside PySide6)", "LGPL 3.0",
     "The window, widgets and Qt WebEngine. Source: https://download.qt.io/official_releases/qt/ . Qt's own "
     "license files are in the PySide6 folder."),
    ("Chromium (inside Qt WebEngine)", "BSD-style and others",
     "Web page sources and web panels. The full list of Chromium's parts and licenses is at "
     "https://source.chromium.org/chromium/chromium/src/+/main:LICENSE and in Qt WebEngine's attributions "
     "(https://doc.qt.io/qt-6/qtwebengine-licensing.html)."),
    ("adb (Android platform-tools)", "Apache 2.0 / Android SDK terms",
     "Not shipped with SprigCast: downloaded from Google on request, for Android Screen, into SprigCast's app "
     "data with Google's NOTICE.txt beside it. It runs as a separate program. Terms: "
     "https://developer.android.com/studio/terms"),
    ("Qt WebEngine with H.264/AAC", "LGPL 3.0",
     "An unmodified Qt WebEngine source release built with proprietary_codecs on, so web pages can play MP4 "
     "clips (packaging/webengine_codecs.py says how). H.264 and AAC may be covered by patents in some "
     "countries."),
]


def _license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    text = (meta.get("License") or "").strip()
    if text and len(text) < 120 and "\n" not in text:
        return text
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    return "see the license text below" if text else "not stated in the package"


def _license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for file in dist.files or []:
        name = Path(str(file)).name.upper()
        if not name.startswith(LICENSE_NAMES):
            continue
        if "dist-info" not in str(file) and "licenses" not in str(file).lower() and len(Path(str(file)).parts) > 2:
            continue  # a LICENSE deep inside a package's own code: usually a vendored test fixture
        try:
            body = Path(dist.locate_file(file)).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if body:
            texts.append((str(file), body))
    if not texts:
        body = (dist.metadata.get("License") or "").strip()
        if "\n" in body:
            texts.append(("License (from the package's metadata)", body))
    return texts


def packages(site: Path) -> list[metadata.Distribution]:
    found = {}
    for dist in metadata.distributions(path=[str(site)]):
        name = (dist.metadata.get("Name") or "").strip()
        if name and name.lower() != "sprigcast":
            found.setdefault(name.lower(), dist)
    return [found[key] for key in sorted(found)]


def write(home: Path, root: Path) -> Path:
    site = home / "Lib" / "site-packages"
    if not site.is_dir():  # Linux and macOS builds (build_unix.sh): lib/python3.12/site-packages
        site = next(iter(sorted(home.glob("lib/python3.*/site-packages"))), site)
    ours = (root / "LICENSE").read_text(encoding="utf-8")
    # Python's own license sits in the same folder under the same name: read it before ours takes its place.
    python_text = ""
    for candidate in (home / "LICENSE.txt", home / "LICENSE"):
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            if text and text != ours.strip():
                python_text = text
                break
    (home / LICENSE).write_text(ours, encoding="utf-8")

    dists = packages(site)
    lines = [
        "SprigCast: third-party notices",
        "",
        "SprigCast is free software under the GNU General Public License 3.0 or later (LICENSE.txt, next to",
        "this file). Its source code is at https://sprigcast.slothbloom.com/source .",
        "",
        "SprigCast includes the following software from other people. Each keeps its own license, listed",
        "below with its full text. Where a license asks for the source code, the links below lead to it, or",
        "write to the address on https://sprigcast.slothbloom.com/source and it will be provided.",
        "",
        RULE,
        "Summary",
        RULE,
    ]
    for name, terms, _ in EXTRA:
        lines.append(f"- {name}: {terms}")
    for dist in dists:
        lines.append(f"- {dist.metadata['Name']} {dist.version}: {_license_of(dist)}")

    for name, terms, what in EXTRA:
        lines += ["", RULE, f"{name}", f"License: {terms}", RULE, what]
    if python_text:
        lines += ["", "Python's license:", "", python_text]

    for dist in dists:
        name = dist.metadata["Name"]
        home_page = dist.metadata.get("Home-page") or ""
        urls = [u.split(",", 1)[-1].strip() for u in dist.metadata.get_all("Project-URL") or []]
        link = home_page or (urls[0] if urls else "")
        lines += ["", RULE, f"{name} {dist.version}", f"License: {_license_of(dist)}"]
        if link:
            lines.append(f"Source: {link}")
        lines.append(RULE)
        for path, body in _license_texts(dist):
            lines += [f"--- {path} ---", body, ""]

    target = home / NOTICES
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: notices.py <bundled python folder> <repo root>")
        return 2
    target = write(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"wrote {target} ({target.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
