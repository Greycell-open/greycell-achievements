# End to end on Windows: install 1.0.0, update to a test 1.0.1 through the
# dashboard's Install update, uninstall. Everything runs in a scratch folder
# with a scratch profile on port 8799, popups off, so a real copy on 8788 is
# left alone; this user's Start with Windows value is saved and restored.
# Needs, in the work folder: GreycellAchievementsSetup-1.0.0.exe, and in
# srv\ the 1.0.1 installer plus a latest.json pointing at
# http://127.0.0.1:8798/ (scripts\build-setup.ps1 -BaseUrl http://127.0.0.1:8798).
param([string]$Work = "$env:TEMP\ga-e2e", [string]$Python = "py")
$ErrorActionPreference = "Continue"
$T = $Work
$failed = 0
function Check([string]$what, [bool]$ok, [string]$detail = "") {
    if ($ok) { "PASS  $what $detail" } else { "FAIL  $what $detail"; $script:failed++ }
}
function Pulse { try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8799/v1/local/pulse).Content | ConvertFrom-Json } catch { $null } }
function Entry { Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall | Where-Object { $_.PSChildName -like "{6E0C2C1B*" } }

Remove-Item -Recurse -Force "$T\config", "$T\home", "$env:TEMP\GreycellAchievements-update" -ErrorAction SilentlyContinue
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$saved = (Get-ItemProperty $runKey -Name GreycellAchievements -ErrorAction SilentlyContinue).GreycellAchievements
"saved Run value: $saved"
$env:GREYCELL_ACHIEVEMENTS_PORT = "8799"; $env:GREYCELL_ACHIEVEMENTS_NO_BROWSER = "1"
$env:GREYCELL_ACHIEVEMENTS_NO_UPDATE_PROMPT = "1"
$env:OPENACHIEVEMENTS_HOME = "$T\home"; $env:OPENACHIEVEMENTS_CONFIG = "$T\config"
New-Item -ItemType Directory -Force "$T\config" | Out-Null
'{"update": {"manifest": "http://127.0.0.1:8798/latest.json"}, "notify": {"enabled": false}}' | Set-Content -Encoding ascii "$T\config\machine.json"
$srv = $null
try {
    "--- install 1.0.0"
    $p = Start-Process "$T\GreycellAchievementsSetup-1.0.0.exe" -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/DIR=$T\app" -PassThru
    $p.WaitForExit()
    Check "installer exit code 0" ($p.ExitCode -eq 0) "($($p.ExitCode))"
    foreach ($i in 1..40) { $x = Pulse; if ($x) { break }; Start-Sleep 1 }
    Check "installed app runs 1.0.0" ($x.version -eq "1.0.0") "($($x.version))"
    $un = Entry
    Check "Windows lists it" ([bool]$un -and (Get-ItemProperty $un.PSPath).DisplayVersion -eq "1.0.0")
    Check "Start menu entry" (Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Greycell Achievements.lnk")

    "--- update through the dashboard"
    $srv = Start-Process $Python -ArgumentList "-m","http.server","8798","--bind","127.0.0.1","--directory","$T\srv" -WindowStyle Hidden -PassThru
    Start-Sleep 2
    $html = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/).Content
    $token = ([regex]'oa-local-token" content="([^"]+)"').Match($html).Groups[1].Value
    $u = (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8799/v1/local/update).Content | ConvertFrom-Json
    Check "1.0.1 offered and installable" ($u.available.version -eq "1.0.1" -and $u.installable)
    $r = Invoke-WebRequest -UseBasicParsing -Method Post -Headers @{ "X-OA-Token" = $token } http://127.0.0.1:8799/v1/local/update/install
    Check "Install update accepted" (($r.Content | ConvertFrom-Json).started)
    $went = $false; $back = $null
    foreach ($i in 1..120) { $x = Pulse; if (-not $x) { $went = $true } elseif ($x.version -eq "1.0.1") { $back = $x; break }; Start-Sleep 1 }
    Check "old app went away" $went
    Check "app came back on 1.0.1 by itself" ($back.version -eq "1.0.1") "(after about $i s)"
    Check "Windows lists 1.0.1" ((Get-ItemProperty (Entry).PSPath).DisplayVersion -eq "1.0.1")
    Check "downloaded installer kept for this version only" (@(Get-ChildItem "$env:TEMP\GreycellAchievements-update").Name -join "," -eq "GreycellAchievementsSetup-1.0.1.exe")

    "--- uninstall"
    Set-ItemProperty $runKey -Name GreycellAchievements -Value "`"$T\app\GreycellAchievements.exe`" --background"
    $q = Start-Process "$T\app\unins000.exe" -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES" -PassThru; $q.WaitForExit()
    foreach ($i in 1..20) { if (-not (Test-Path "$T\app\GreycellAchievements.exe") -and -not (Entry)) { break }; Start-Sleep 1 }
    Check "app stopped" (-not (Pulse))
    Check "app folder removed" (-not (Test-Path "$T\app\GreycellAchievements.exe"))
    Check "Windows entry removed" (-not (Entry))
    Check "its Start with Windows value removed" (-not (Get-ItemProperty $runKey -Name GreycellAchievements -ErrorAction SilentlyContinue))
    Check "achievements kept" (Test-Path "$T\home\profiles")
}
finally {
    if ($srv) { Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue }
    if (Test-Path "$T\app\unins000.exe") {                  # a failed run still cleans up after itself
        $q = Start-Process "$T\app\unins000.exe" -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES" -PassThru; $q.WaitForExit(); Start-Sleep 5
    }
    if ($saved) { Set-ItemProperty $runKey -Name GreycellAchievements -Value $saved }
    else { Remove-ItemProperty $runKey -Name GreycellAchievements -ErrorAction SilentlyContinue }
}
$now = (Get-ItemProperty $runKey -Name GreycellAchievements -ErrorAction SilentlyContinue).GreycellAchievements
Check "this user's Start with Windows value restored" ($now -eq $saved)
"copy on 8788 (if any): $(try { (Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8788/v1/local/pulse).Content } catch { 'none answering' })"
if ($failed) { "RESULT: $failed check(s) failed"; exit 1 } else { "RESULT: all checks passed"; exit 0 }
