# Builds the SprigCast installer on Windows: a private copy of Python with SprigCast installed into it,
# wrapped by Inno Setup. No freezing (PyInstaller) step, so plugins, lazy imports and "Start with
# Windows" (which runs pythonw.exe -m sprigcast.app.main) work exactly as they do from source.
#
# Needs: uv and Inno Setup 6 (iscc) on PATH. Output: packaging\out\SprigCast-Setup-<version>.exe
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

# 3. Leave out what never runs on a user's PC (tests, caches, headers, pip itself).
$py = Join-Path $build "python"
Get-ChildItem $py -Recurse -Directory -Include "__pycache__", "tests", "test" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch "sprigcast" } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force (Join-Path $py "include") -ErrorAction SilentlyContinue

# 4. Icon, then the installer.
& $python (Join-Path $PSScriptRoot "make_icon.py") (Join-Path $build "sprigcast.ico")
$version = (& $python -c "import importlib.metadata as m; print(m.version('sprigcast'))").Trim()
iscc "/DAppVersion=$version" "/DBuildDir=$build" "/O$out" (Join-Path $PSScriptRoot "sprigcast.iss")
Get-ChildItem $out
