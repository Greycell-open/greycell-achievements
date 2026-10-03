# Build GreycellAchievements.exe: the app and its own Python in one file.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-exe.ps1
#
# Uses the same locked packages as run.bat (requirements\server.lock, hashes
# required) in a throwaway environment under .build\, plus PyInstaller.
# Writes GreycellAchievements.exe in the project folder; it is attached to
# GitHub releases and never committed. Unsigned, so SmartScreen may ask once.
param([string]$DistPath = ".",   # another folder when a copy is running from this one
      [string]$Python = "")        # a python.exe to build with; else the py launcher's newest Python 3
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$venv = ".build\venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    if ($Python) { & $Python -m venv $venv } else { py -3 -m venv $venv }
    & "$venv\Scripts\python" -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r requirements\server.lock
    & "$venv\Scripts\python" -m pip install -q --disable-pip-version-check "pyinstaller==6.16.0"
}
& "$venv\Scripts\python" -m pip install -q --disable-pip-version-check --no-deps --force-reinstall --no-build-isolation .
& "$venv\Scripts\python" -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name GreycellAchievements `
    --icon "$PWD\src\openachievements\web\favicon.ico" `
    --collect-data openachievements `
    --collect-submodules openachievements `
    --collect-submodules uvicorn `
    --distpath $DistPath --workpath .build\work --specpath .build `
    "$PWD\scripts\exe_entry.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Get-Item (Join-Path $DistPath GreycellAchievements.exe) | Select-Object Name, @{n="MB";e={[math]::Round($_.Length/1MB)}}
