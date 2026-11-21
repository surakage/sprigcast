# Builds the SprigCast installer on Windows: a private copy of Python with SprigCast installed into it,
# wrapped by Inno Setup. No freezing (PyInstaller) step, so plugins, lazy imports and "Start with
# Windows" (which runs pythonw.exe -m sprigcast.app.main) work exactly as they do from source.
#
# Needs: uv and Inno Setup 6.7+ (iscc) on PATH. Output: packaging\out\SprigCast-Setup-<version>.exe
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # a failing uv or iscc stops the build too
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $PSScriptRoot "build"
$out = Join-Path $PSScriptRoot "out"
Remove-Item -Recurse -Force $build, $out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $build, $out | Out-Null

# 1. A standalone Python 3.12 (the same builds uv uses), copied to build\python.
$pyRoot = Join-Path $build "pythons"
uv python install 3.12 --install-dir $pyRoot
# uv also makes a "cpython-3.12-..." junction pointing at the real "cpython-3.12.N-..." folder: take the folder.
$home312 = Get-ChildItem $pyRoot -Directory |
    Where-Object { $_.Name -like "cpython-3.12.*" -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
    Select-Object -First 1
if (-not $home312 -or -not (Test-Path (Join-Path $home312.FullName "python.exe"))) {
    throw "No standalone Python 3.12 found in $pyRoot"
}
Move-Item $home312.FullName (Join-Path $build "python")
Remove-Item -Recurse -Force $pyRoot
$python = Join-Path $build "python\python.exe"

# 2. SprigCast and its locked dependencies into that Python.
Push-Location $root
uv export --frozen --no-dev --no-hashes --no-emit-project -o (Join-Path $build "requirements.txt")
Pop-Location
uv pip install --python $python --break-system-packages -r (Join-Path $build "requirements.txt")
uv pip install --python $python --break-system-packages --no-deps $root

# 2-. Qt WebEngine with H.264/AAC laid over PySide6's (web page sources play MP4 clips; webengine_codecs.py). Its
#     zip is pinned with the Qt version it was built for: a different PySide6 needs a new build first.
$pin = Join-Path $PSScriptRoot "webengine_codecs.json"
if (Test-Path $pin) {
    $codecs = Get-Content $pin -Raw | ConvertFrom-Json
    $pyside = (& $python -c "import PySide6, os; print(PySide6.__version__ + '|' + os.path.dirname(PySide6.__file__))").Trim().Split("|")
    if ($pyside[0] -ne $codecs.qt) {
        throw "PySide6 is $($pyside[0]) but the page engine with H.264/AAC is built for $($codecs.qt): run the 'Page engine with H.264/AAC' workflow for $($pyside[0]) and pin it in webengine_codecs.json"
    }
    $zip = Join-Path $build "qtwebengine-codecs.zip"
    Invoke-WebRequest $codecs.url -OutFile $zip
    & $python (Join-Path $PSScriptRoot "webengine_codecs.py") overlay $zip $pyside[1] $codecs.sha256
    Remove-Item $zip
} else {
    Write-Warning "No page engine with H.264/AAC pinned (webengine_codecs.json): web pages can't play MP4 video"
}

# 2a. The command-line tools (pyproject [project.scripts]). uv's .exe launchers hold this build folder's path to
#     python.exe, so on a user's PC they fail with "uv trampoline failed to canonicalize script path" (PC test,
#     2026-11-17). Small .cmd files run the Python next to them instead, wherever SprigCast is installed.
$scripts = Join-Path $build "python\Scripts"
Remove-Item (Join-Path $scripts "sprigcast*.exe") -Force -ErrorAction SilentlyContinue
$tools = @{
    "sprigcast-check" = "sprigcast.tools.check"
    "sprigcast-teststream" = "sprigcast.tools.teststream"
    "sprigcast-memcheck" = "sprigcast.tools.memcheck"
}
foreach ($tool in $tools.Keys) {
    $line = "@`"%~dp0..\python.exe`" -c `"import sys; from $($tools[$tool]) import main; sys.exit(main())`" %*"
    Set-Content -Path (Join-Path $scripts "$tool.cmd") -Value $line -Encoding ascii
}

# 2b. The virtual camera (a DirectShow camera plus a Windows 11 media source) next to the sprigcast
#     package, 64-bit and 32-bit (32-bit apps only see 32-bit cameras).
cmake -S (Join-Path $root "native\camera") -B (Join-Path $build "camera") -A x64
cmake --build (Join-Path $build "camera") --config Release
cmake -S (Join-Path $root "native\camera") -B (Join-Path $build "camera32") -A Win32
cmake --build (Join-Path $build "camera32") --config Release
$camDir = Join-Path $build "python\Lib\site-packages\sprigcast\camera"
New-Item -ItemType Directory -Force $camDir | Out-Null
Copy-Item (Join-Path $build "camera\Release\sprigcast_camera.dll") $camDir
Copy-Item (Join-Path $build "camera32\Release\sprigcast_camera32.dll") $camDir

# 2b2. The native engine (native/core, docs/native-engine.md) next to the sprigcast package. The default;
#      SPRIGCAST_ENGINE=python or safe mode starts the old Python engine instead.
cmake -S (Join-Path $root "native\core") -B (Join-Path $build "core") -A x64
cmake --build (Join-Path $build "core") --config Release
& ctest --test-dir (Join-Path $build "core") -C Release --output-on-failure
if ($LASTEXITCODE -ne 0) { throw "The native engine's checks failed" }
$engineDir = Join-Path $build "python\Lib\site-packages\sprigcast\engine"
New-Item -ItemType Directory -Force $engineDir | Out-Null
Copy-Item (Join-Path $build "core\Release\sprigcore.dll") $engineDir
Copy-Item (Join-Path $build "core\Release\SprigCastCapture.exe") $engineDir

# 2c. SprigCast.exe next to python312.dll: the app runs as SprigCast.exe, so other tools can recognise it.
#     Built against the bundled Python's headers, before step 3 removes them.
& $python (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $build "sprigcast.ico")
cmake -S (Join-Path $root "native\launcher") -B (Join-Path $build "launcher") -A x64 `
    "-DPY_HOME=$((Join-Path $build 'python') -replace '\\', '/')" `
    "-DSPRIGCAST_ICON=$((Join-Path $build 'sprigcast.ico') -replace '\\', '/')"
cmake --build (Join-Path $build "launcher") --config Release
Copy-Item (Join-Path $build "launcher\Release\SprigCast.exe") (Join-Path $build "python")

# 3. Leave out what never runs on a user's PC (tests, caches, headers, pip itself).
$py = Join-Path $build "python"
# (Libraries' test folders and type hints go in prune.py. Compiled __pycache__ files stay: Program Files
# can't be written to, so without them Python would recompile every module on every start.)
Remove-Item -Recurse -Force (Join-Path $py "include") -ErrorAction SilentlyContinue
# Unused Qt modules, plugins and tools, WebEngine's debug resources and other languages, Tk, pip and
# OpenCV's FFmpeg (prune.py says why each is safe). The build checks the app still starts afterwards.
uv run --no-project --with pefile python (Join-Path $PSScriptRoot "prune.py") $py
# SprigCast's license and every bundled part's license (notices.py): LICENSE.txt and THIRD-PARTY-NOTICES.txt in
# the installed folder.
& $python (Join-Path $PSScriptRoot "notices.py") $py $root
# A test build (any build that isn't from a v* tag) says so, with its run number and commit, in the version
# SprigCast shows and logs (sprigcast.build_label), so reports and logs can tell builds apart.
if ($env:GITHUB_RUN_ID -and -not ($env:GITHUB_REF -like "refs/tags/v*")) {
    $stamp = @{ kind = "test"; run = $env:GITHUB_RUN_ID; commit = $env:GITHUB_SHA.Substring(0, 7) } | ConvertTo-Json -Compress
    Set-Content -Path (Join-Path $py "Lib\site-packages\sprigcast\build.json") -Value $stamp -Encoding utf8
}
# Every file this version installs: the next update deletes what it no longer ships (manifest.py).
& $python (Join-Path $PSScriptRoot "manifest.py") $py

# 4. The wizard's brand art, then the installer.
& $python (Join-Path $PSScriptRoot "make_wizard_images.py") (Join-Path $build "wizard")
$version = (& $python -c "import importlib.metadata as m; print(m.version('sprigcast'))").Trim()
iscc "/DAppVersion=$version" "/DBuildDir=$build" "/O$out" (Join-Path $PSScriptRoot "sprigcast.iss")
Get-ChildItem $out
