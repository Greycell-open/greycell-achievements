# Build the Windows installer: dist\GreycellAchievements.exe, then
# dist\GreycellAchievementsSetup.exe around it, then dist\latest.json, the
# manifest the installed app reads to update itself.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build-setup.ps1
#
# Needs Inno Setup 6 (winget install JRSoftware.InnoSetup). The manifest's
# address is where the installer will be served from; publish both together.
param([string]$BaseUrl = "https://greycell.app/downloads/greycell-achievements",
      [string]$Notes = "",
      [string]$NotesFile = "")   # what's new, shown in the update popup; a file keeps line breaks
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$version = (Select-String -Path pyproject.toml -Pattern '^version = "(.+)"').Matches[0].Groups[1].Value
New-Item -ItemType Directory -Force dist | Out-Null
& powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-exe.ps1 -DistPath dist
if ($LASTEXITCODE -ne 0) { throw "exe build failed" }
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found: winget install JRSoftware.InnoSetup" }
& $iscc /Q "/DAppVersion=$version" installer\GreycellAchievements.iss
if ($LASTEXITCODE -ne 0) { throw "installer build failed" }
if ($NotesFile) { $Notes = [System.IO.File]::ReadAllText((Resolve-Path $NotesFile)).Trim() }
$setup = Get-Item dist\GreycellAchievementsSetup.exe
$hash = (Get-FileHash $setup.FullName -Algorithm SHA256).Hash.ToLower()
$manifest = [ordered]@{ version = $version; setup = "$BaseUrl/GreycellAchievementsSetup-$version.exe";
                        sha256 = $hash; size = $setup.Length; notes = $Notes;
                        released = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd") }
$json = $manifest | ConvertTo-Json
[System.IO.File]::WriteAllText("$PWD\dist\latest.json", $json, (New-Object System.Text.UTF8Encoding $false))
Copy-Item $setup.FullName "dist\GreycellAchievementsSetup-$version.exe" -Force
Get-ChildItem dist | Select-Object Name, @{n="MB";e={[math]::Round($_.Length/1MB, 1)}}
