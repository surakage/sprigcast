"""Download the official Qt SDK (MSVC, 64-bit) for one version straight from Qt's online repository: what aqt does,
for the few packages the page engine build needs (webengine-codecs.yml). aqt 3.3 couldn't read Qt 6.12's repository
(run 44657659498), so this reads its Updates.xml itself and checks each archive against the SHA-1 Qt publishes.

Usage: qt_sdk.py <version, e.g. 6.12.0> <out folder> [module ...]   (needs 7-Zip's 7z on PATH or in Program Files)
       qt_sdk.py source <version> <module, e.g. qtwebengine> <out folder>   (the module's source release, checked)
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


def hrefs(listing: str) -> list[str]:
    import re

    return re.findall(r'href="([^"?#]+)"', listing)


def download(url: str, target: Path, algos: tuple[str, ...]) -> dict[str, str]:
    """Stream url to target; returns {algo: hexdigest}. Three tries (run 44736478013 timed out once)."""
    for attempt in range(3):
        digests = {a: hashlib.new(a) for a in algos}
        try:
            with urllib.request.urlopen(url, timeout=120) as reply, open(target, "wb") as f:
                for block in iter(lambda: reply.read(1 << 20), b""):
                    f.write(block)
                    for d in digests.values():
                        d.update(block)
            return {a: d.hexdigest() for a, d in digests.items()}
        except OSError as exc:  # timeouts, resets (URLError is an OSError)
            print(f"  {url}: {exc}, trying again")
            time.sleep(10 * (attempt + 1))
    raise OSError(f"couldn't download {url}")


def listing(url: str) -> tuple[list[str], list[str]]:
    """(files, folders) named in a download.qt.io folder listing."""
    with urllib.request.urlopen(url, timeout=60) as reply:
        refs = hrefs(reply.read().decode("utf-8", "replace"))
    files, folders = [], []
    for ref in refs:
        if "://" in ref or ref.startswith(("..", "?", "mailto:")):
            continue
        name = ref.rstrip("/").rsplit("/", 1)[-1]
        if name:
            (folders if ref.endswith("/") else files).append(name)
    return files, folders


def source(version: str, module: str, out: Path) -> Path:
    """Find <module>'s source release <version> on download.qt.io (or a mirror) by reading folder listings,
    check it against the checksums Qt publishes next to it, and return the file. Since Qt 6.12 Qt WebEngine is a
    separate product with its own numbering (6.140.0 goes with Qt 6.12.0) and isn't in Qt's submodules/ or
    single/ (runs 44668442935, 44669973862), so a few roots are searched a few folders deep."""
    import re

    v = re.escape(version)
    parts = version.split(".")
    minor = ".".join(parts[:2])
    pattern = re.compile(rf"^{module}-everywhere-(?:opensource-)?src-{v}\.(?:tar\.xz|zip|7z)$")
    follow = {version, minor, module, "submodules", "src", "source", "sources"}
    out.mkdir(parents=True, exist_ok=True)
    tried: list[str] = []
    for base in MIRRORS:
        queue = [(f"{kind}/{root}", 0) for kind in ("official_releases", "archive")
                 for root in (f"{module}/", f"qt/{minor}/", "additional_libraries/")]
        seen: set[str] = set()
        while queue:
            folder, depth = queue.pop(0)
            if folder in seen:
                continue
            seen.add(folder)
            try:
                files, folders = listing(f"{base}/{folder}")
            except Exception as exc:  # noqa: BLE001 - next place
                tried.append(f"{base}/{folder}: {exc}")
                continue
            found = sorted(n for n in files if pattern.match(n))
            if not found:
                tried.append(f"{base}/{folder}: {', '.join(folders)[:300]}")
                if depth < 4:
                    queue += [(f"{folder}{d}/", depth + 1) for d in folders if d in follow or module in d]
                continue
            name = next((n for n in found if n.endswith(".tar.xz")), found[0])
            try:
                return fetch_checked(base, folder, name, files, out)
            except OSError as exc:  # this mirror is having a bad moment: the next one
                tried.append(f"{base}/{folder}{name}: {exc}")
    raise RuntimeError(f"no {module} {version} source release found:\n  " + "\n  ".join(tried))


def fetch_checked(base: str, folder: str, name: str, files: list[str], out: Path) -> Path:
    """Download name from base/folder, checked against the checksum Qt publishes next to it."""
    check = None
    for algo, sums in (("sha256", f"{name}.sha256"), ("md5", "md5sums.txt"), ("sha1", f"{name}.sha1")):
        if sums not in files:
            continue
        text = urllib.request.urlopen(f"{base}/{folder}{sums}", timeout=60).read().decode()
        lines = [ln.split() for ln in text.splitlines() if ln.split()]
        line = next((ln for ln in lines if name in ln[-1]), None) if sums == "md5sums.txt" else lines[0]
        if line:
            check = (algo, line[0].lower())
            break
    if check is None:
        raise OSError("no checksum published next to it")
    target = out / name
    url = f"{base}/{folder}{name}"
    got = download(url, target, (check[0], "sha256"))
    if got[check[0]] != check[1]:
        target.unlink()
        raise RuntimeError(f"{name}: {check[0]} {got[check[0]]} isn't Qt's {check[1]}")
    print(f"source {url} ({target.stat().st_size / 1e6:.0f} MB, {check[0]} matches), sha256 {got['sha256']}")
    return target


def main(argv: list[str]) -> int:
    if argv and argv[0] == "source":
        found = source(argv[1], argv[2], Path(argv[3]))
        (Path(argv[3]) / "source.txt").write_text(str(found), "utf-8")
        return 0
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
