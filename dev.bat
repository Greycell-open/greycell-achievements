@echo off
rem Try Greycell Achievements on this computer, in a dev sandbox.
rem
rem   dev.bat           open the web library (first run sets everything up)
rem   dev.bat sync-server   also start a local sync server on http://127.0.0.1:8787
rem   dev.bat reset     delete the dev profile and start over
rem   dev.bat <args>    run any other command, e.g.  dev.bat achievement list
rem
rem Everything lives in .venv\ (packages, from requirements\server.lock) and
rem %LOCALAPPDATA%\OpenAchievements-dev\ (the profile and this machine's settings).
rem A real profile in %USERPROFILE%\OpenAchievements and its settings in
rem %APPDATA%\OpenAchievements are never touched.
setlocal
cd /d "%~dp0"
set "OPENACHIEVEMENTS_HOME=%LOCALAPPDATA%\OpenAchievements-dev"
set "OPENACHIEVEMENTS_CONFIG=%LOCALAPPDATA%\OpenAchievements-dev\machine"
set "OA=.venv\Scripts\openachievements.exe"

if not exist "%OA%" (
  echo Setting up .venv, one time only...
  py -3 -m venv .venv || python -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -q --disable-pip-version-check --require-hashes --no-deps -r requirements\server.lock || goto :fail
  .venv\Scripts\python -m pip install -q --disable-pip-version-check --no-deps --no-build-isolation -e . || goto :fail
)

if /i "%~1"=="reset" (
  rmdir /s /q "%OPENACHIEVEMENTS_HOME%" 2>nul
  echo Dev profile deleted. Run dev.bat again for a fresh one.
  goto :eof
)

if not exist "%OPENACHIEVEMENTS_HOME%\profiles" (
  "%OA%" profile create "Dev" || goto :fail
  "%OA%" pack install examples\achievement-packs\cave-story-community || goto :fail
  "%OA%" achievement unlock cave-story-community:balrog --note "dev sample" >nul
)

if /i "%~1"=="sync-server" (
  start "Greycell Achievements sync server" cmd /k "set OA_DATA_DIR=%OPENACHIEVEMENTS_HOME%\server&& set OA_REGISTRATION=open&& set OA_HOST=127.0.0.1&& .venv\Scripts\openachievements-server.exe"
  echo Sync server starting on http://127.0.0.1:8787  ^(API docs at /docs^)
  echo Connect this profile:  dev.bat server connect http://127.0.0.1:8787 dev --register
  goto :serve
)
if not "%~1"=="" (
  "%OA%" %*
  goto :eof
)

:serve
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8788"
"%OA%" serve
goto :eof

:fail
echo Setup failed. Delete the .venv folder and run dev.bat again.
exit /b 1
