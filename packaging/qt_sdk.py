"""Download the official Qt SDK (MSVC, 64-bit) for one version straight from Qt's online repository: what aqt does,
for the few packages the page engine build needs (webengine-codecs.yml). aqt 3.3 couldn't read Qt 6.12's repository
(run 44657659498), so this reads its Updates.xml itself and checks each archive against the SHA-1 Qt publishes.

Usage: qt_sdk.py <version, e.g. 6.12.0> <out folder> [module ...]   (needs 7-Zip's 7z on PATH or in Program Files)
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

MIRRORS = ("https://download.qt.io", "https://qt.mirror.constant.com", "https://mirrors.ocf.berkeley.edu/qt",
           "https://ftp.fau.de/qtproject")
REPO = "online/qtsdkrepository/windows_x86/desktop"


def fetch(path: str) -> bytes:
    last = None
    for base in MIRRORS:
        for attempt in range(3):
            try:
                with urllib.request.urlopen(f"{base}/{path}", timeout=120) as reply:
                    return reply.read()
            except Exception as exc:  # noqa: BLE001 - next try, next mirror
                last = exc
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"couldn't download {path}: {last}")


def seven_zip() -> str:
    found = shutil.which("7z") or next((p for p in (r"C:\Program Files\7-Zip\7z.exe",) if Path(p).is_file()), None)
    if not found:
        raise RuntimeError("7-Zip isn't here")
    return found


def wanted(name: str, short: str, modules: list[str]) -> bool:
    """qt.qt6.6120.win64_msvc2022_64 (base) and qt.qt6.6120.addons.<module>.win64_msvc2022_64."""
    if not name.startswith(f"qt.qt6.{short}.") or "msvc20" not in name or "arm64" in name or not name.endswith("_64"):
        return False
    middle = name[len(f"qt.qt6.{short}."):]
    if "debug_information" in middle:
        return False
    return "." not in middle or any(middle.startswith(f"addons.{m}.") for m in modules)


def extract_targets(package: ET.Element, out: Path) -> dict[str, Path]:
    """Archive -> folder, from the package's Extract operations (@TargetDir@/6.12.0/msvc2022_64 in Qt 6.8+)."""
    targets: dict[str, Path] = {}
    for op in package.iter("Operation"):
        if op.get("name") != "Extract":
            continue
        args = [a.text or "" for a in op.iter("Argument")]
        if len(args) >= 2:
            where = args[0].replace("@TargetDir@", str(out)).replace("@ApplicationsDir@", str(out))
            targets[args[1].strip()] = Path(where)
    return targets


def main(argv: list[str]) -> int:
    version, out, modules = argv[0], Path(argv[1]), argv[2:]
    short = version.replace(".", "")
    # Qt 6.8+ splits the repository into sub-folders (the base, its add-ons...): read every Updates.xml there.
    import re

    listing = fetch(f"{REPO}/qt6_{short}/").decode("utf-8", "replace")
    folders = sorted({m for m in re.findall(r'href="([^"?/][^"]*)/"', listing) if m.startswith(f"qt6_{short}")
                      and not re.search(r"wasm|src|doc|debug|arm|android|ios", m)})
    packages = []
    for sub in folders or [f"qt6_{short}"]:
        folder = f"qt6_{short}/{sub}"
        try:
            xml = fetch(f"{REPO}/{folder}/Updates.xml")
        except RuntimeError as exc:
            print(exc)
            continue
        packages += [(folder, p) for p in ET.fromstring(xml).iter("PackageUpdate")
                     if wanted(p.findtext("Name", ""), short, modules)]
    if not packages:
        raise RuntimeError(f"Qt {version}: no MSVC 64-bit packages in {folders}")
    archs = {p.findtext("Name", "").rsplit(".", 1)[-1] for _, p in packages}
    arch = max(archs)  # the newest MSVC
    packages = [(f, p) for f, p in packages if p.findtext("Name", "").endswith("." + arch)]
    print(f"Qt {version} {arch}: {', '.join(p.findtext('Name') for _, p in packages)}")
    missing = [m for m in modules if not any(f".addons.{m}." in p.findtext("Name", "") for _, p in packages)]
    if missing:
        raise RuntimeError(f"Qt {version}: no {', '.join(missing)} for {arch}")
    seven = seven_zip()
    out.mkdir(parents=True, exist_ok=True)
    for folder, package in packages:
        name, ver = package.findtext("Name"), package.findtext("Version")
        archives = [a.strip() for a in (package.findtext("DownloadableArchives") or "").split(",") if a.strip()]
        targets = extract_targets(package, out)
        for archive in archives:
            if "debug-symbols" in archive:
                continue
            path = f"{REPO}/{folder}/{name}/{ver}{archive}"
            data = fetch(path)
            want = fetch(path + ".sha1").decode().split()[0].lower()
            got = hashlib.sha1(data).hexdigest()
            if got != want:
                raise RuntimeError(f"{archive}: SHA-1 {got} isn't Qt's {want}")
            local = out / archive
            local.write_bytes(data)
            target = targets.get(archive, out)
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run([seven, "x", "-y", "-bso0", "-bsp0", f"-o{target}", str(local)], check=True)
            local.unlink()
            print(f"  {archive} ({len(data) / 1e6:.0f} MB) -> {target}")
    found = sorted(out.rglob("qt-configure-module.bat"), key=lambda f: len(f.parts))
    prefix = found[0].parent.parent if found else None
    if prefix is None:
        for d in sorted(out.rglob("*")):
            if d.is_dir() and len(d.relative_to(out).parts) <= 3:
                print("   ", d.relative_to(out))
        raise RuntimeError(f"Qt didn't unpack where expected under {out} (no qt-configure-module.bat)")
    (out / "prefix.txt").write_text(str(prefix), "utf-8")
    print(f"QTDIR {prefix}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
