@echo off
rem Run Greycell Achievements on this computer, with your real profile.
rem
rem   run.bat           open your library in the browser; while this window is open
rem                     it also watches running games, saves and Steam
rem   run.bat <args>    run any other command, e.g.
rem                       run.bat catalog list portal
rem                       run.bat catalog install 400
rem                       run.bat save check
rem
rem Your profile lives in %USERPROFILE%\OpenAchievements (or %OPENACHIEVEMENTS_HOME%)
rem and this machine's settings in %APPDATA%\OpenAchievements. For a throwaway
rem sandbox instead, use dev.bat. Packages live in .venv\ (from requirements\server.lock).
setlocal
cd /d "%~dp0"
set "OA=.venv\Scripts\openachievements.exe"
if defined OPENACHIEVEMENTS_HOME (set "OA_PROFILES=%OPENACHIEVEMENTS_HOME%\profiles") else (set "OA_PROFILES=%USERPROFILE%\OpenAchievements\profiles")

if not exist "%OA%" (
  echo Setting up .venv, one time only...
  py -3 -m venv .venv || python -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r requirements\server.lock || goto :fail
  .venv\Scripts\python -m pip install -q --disable-pip-version-check --no-deps --no-build-isolation -e . || goto :fail
)

if not exist "%OA_PROFILES%" (
  echo No profile on this computer yet.
  set "OA_NAME="
  set /p "OA_NAME=Your name for the profile: "
  call :create
  if errorlevel 1 goto :fail
)

if not "%~1"=="" (
  "%OA%" %*
  goto :eof
)

rem A newer release, if one is out: installed now, before the app starts.
"%OA%" update install >nul 2>&1
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8788"
echo Library: http://127.0.0.1:8788
echo It also watches in the background (games, saves, Steam). Close this window to stop everything.
"%OA%" serve
goto :eof

:create
if "%OA_NAME%"=="" set "OA_NAME=%USERNAME%"
"%OA%" profile create "%OA_NAME%"
exit /b %errorlevel%

:fail
echo Something failed. Delete the .venv folder and run run.bat again.
exit /b 1
