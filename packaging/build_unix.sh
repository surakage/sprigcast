#!/usr/bin/env bash
# Builds the SprigCast test download for Linux or macOS, the way build_windows.ps1 does for Windows: a private
# copy of Python with SprigCast installed into it. No freezing step, so plugins and lazy imports work as they
# do from source. SprigCast draws with the Python engine on these systems (the C++ engine is Windows-only).
#
# Needs: uv on PATH. Output, in packaging/out:
#   Linux:  SprigCast-<version>-linux-<arch>.tar.gz    (unpack, run SprigCast/SprigCast)
#   macOS:  SprigCast-<version>-macos-<arch>.zip       (unpack, open SprigCast.app; not signed: a test build)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
root="$(dirname "$here")"
build="$here/build"
out="$here/out"
rm -rf "$build" "$out"
mkdir -p "$build" "$out"

case "$(uname -s)" in
  Linux) system=linux ;;
  Darwin) system=macos ;;
  *) echo "build_unix.sh builds on Linux and macOS only" >&2; exit 1 ;;
esac
arch="$(uname -m)"
[ "$arch" = "aarch64" ] && arch=arm64

# 1. A standalone Python 3.12 (the same builds uv uses), moved to build/python.
uv python install 3.12 --install-dir "$build/pythons"
home312="$(find "$build/pythons" -maxdepth 1 -type d -name 'cpython-3.12.*' | head -n 1)"
[ -x "$home312/bin/python3.12" ] || { echo "uv didn't install a standalone Python 3.12" >&2; exit 1; }
mv "$home312" "$build/python"
python="$build/python/bin/python3.12"

# 2. SprigCast and its locked dependencies into that Python.
(cd "$root" && uv export --frozen --no-dev --no-hashes --no-emit-project -o "$build/requirements.txt")
uv pip install --python "$python" --break-system-packages -r "$build/requirements.txt"
uv pip install --python "$python" --break-system-packages --no-deps "$root"
site="$("$python" -c "import sprigcast, os; print(os.path.dirname(os.path.dirname(sprigcast.__file__)))")"

# 3. Leave out what never runs on a user's PC: pip, Tk, tests, headers.
"$python" -m pip uninstall -y pip >/dev/null 2>&1 || true
rm -rf "$build/python/include" "$build/python/share" "$build/python/lib/python3.12/test" \
       "$build/python/lib/python3.12/tkinter" "$build/python/lib/python3.12/idlelib" \
       "$build/python/lib/python3.12/ensurepip" "$build/python/lib"/libtcl* "$build/python/lib"/libtk* \
       "$build/python/lib"/tcl* "$build/python/lib"/tk* "$build/python/lib"/itcl* "$build/python/lib"/thread*
rm -rf "$site/pip" "$site"/pip-*.dist-info
# Qt's own texts in other languages and Chromium's (SprigCast is in US English), and Qt's developer tools.
qt="$site/PySide6"
rm -f "$qt"/Qt/translations/*.qm "$qt"/assistant "$qt"/designer "$qt"/linguist "$qt"/lrelease "$qt"/lupdate \
      "$qt"/qmlls "$qt"/qmlformat "$qt"/qmllint "$qt"/qmlcachegen "$qt"/qmltyperegistrar "$qt"/qmlimportscanner \
      "$qt"/balsam "$qt"/balsamui "$qt"/qsb "$qt"/svgtoqml
find "$qt" -path '*qtwebengine_locales*' -name '*.pak' ! -name 'en-US.pak' -delete 2>/dev/null || true
find "$build/python" -type d \( -name tests -o -name test \) -path '*site-packages*' -prune -exec rm -rf {} + 2>/dev/null || true

# SprigCast's license and every bundled part's license.
"$python" "$here/notices.py" "$build/python" "$root" || echo "notices.py: not written on this system yet"
[ -f "$build/python/LICENSE.txt" ] || cp "$root/LICENSE" "$build/python/LICENSE.txt"

# A test build says so, with its run number and commit, in the version SprigCast shows and logs
# (sprigcast.build_label). A tester build (TESTER_TAG test-0.5-3) also carries its number and its tag.
if [ -n "${GITHUB_RUN_ID:-}" ] && [[ "${GITHUB_REF:-}" != refs/tags/v* ]]; then
  TESTER_TAG="${TESTER_TAG:-}" "$python" - "$site/sprigcast/build.json" <<'PY'
import importlib.metadata as m, json, os, re, sys
stamp = {"kind": "test", "run": os.environ["GITHUB_RUN_ID"], "commit": os.environ.get("GITHUB_SHA", "")[:7]}
tag = os.environ.get("TESTER_TAG", "").strip()
match = re.fullmatch(r"test-(\d+)\.(\d+)-(\d+)", tag)
if match:
    own = ".".join(m.version("sprigcast").split(".")[:2])
    if f"{match[1]}.{match[2]}" != own:
        sys.exit(f"Tester tag {tag} doesn't match this version ({own}): use test-{own}-N")
    stamp["test"], stamp["tag"] = int(match[3]), tag
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(stamp))
PY
fi
version="$("$python" -c "import importlib.metadata as m; print(m.version('sprigcast'))")"
shown="$("$python" -c "import sprigcast; print(sprigcast.build_label())")"

# 4. The download.
if [ "$system" = linux ]; then
  app="$build/SprigCast"
  mkdir -p "$app"
  mv "$build/python" "$app/python"
  cat > "$app/SprigCast" <<'SH'
#!/bin/sh
# Starts SprigCast with the Python next to this file, wherever the folder was unpacked.
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
exec "$here/python/bin/python3.12" -m sprigcast.app.main "$@"
SH
  chmod +x "$app/SprigCast"
  cp "$app/python/LICENSE.txt" "$app/LICENSE.txt"
  cp "$here/unix/README-linux.txt" "$app/README.txt"
  name="SprigCast-$version-linux-$arch.tar.gz"
  tar -C "$build" -czf "$out/$name" SprigCast
  launcher="$app/SprigCast"
else
  app="$build/SprigCast.app"
  mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
  mv "$build/python" "$app/Contents/Resources/python"
  cat > "$app/Contents/MacOS/SprigCast" <<'SH'
#!/bin/sh
# Starts SprigCast with the Python inside this app.
here="$(cd "$(dirname "$0")/../Resources" && pwd)"
exec "$here/python/bin/python3.12" -m sprigcast.app.main "$@"
SH
  chmod +x "$app/Contents/MacOS/SprigCast"
  sed -e "s/@VERSION@/$version/g" -e "s/@SHOWN@/$shown/g" "$here/unix/Info.plist" > "$app/Contents/Info.plist"
  cp "$app/Contents/Resources/python/LICENSE.txt" "$app/Contents/Resources/LICENSE.txt"
  # Not signed by a developer account (a test build): signed "ad hoc" so Apple chips will run it at all.
  codesign --force --deep --sign - "$app" >/dev/null 2>&1 || echo "codesign: the app is left unsigned"
  name="SprigCast-$version-macos-$arch.zip"
  mkdir "$build/pack"
  mv "$app" "$build/pack/SprigCast.app"
  app="$build/pack/SprigCast.app"
  cp "$here/unix/README-macos.txt" "$build/pack/README.txt"
  ditto -c -k --sequesterRsrc "$build/pack" "$out/$name"  # ditto keeps the links and the signature a Mac app needs
  launcher="$app/Contents/MacOS/SprigCast"
fi

(cd "$out" && { command -v sha256sum >/dev/null && sha256sum "$name" || shasum -a 256 "$name"; } > "SHA256-$system-$arch.txt")
echo "Built $out/$name ($shown), $(du -h "$out/$name" | cut -f1)"
echo "$launcher" > "$build/launcher.txt"
