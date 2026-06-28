@echo off
REM Everyday launcher: runs the control panel with the venv's Python.
REM The console window stays VISIBLE on purpose so errors are easy to see during
REM testing. (Later we can hide it by launching via pythonw / a .vbs wrapper.)

set "VPY=%~dp0venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo Setup has not been run yet. Please double-click setup.bat first.
  pause & exit /b 1
)

REM --- Auto-update: pull the latest code from GitHub on every launch. ---
REM Force-match the remote so it always works (no merge prompts). .env and the
REM venv are gitignored, so they survive the reset. Skip silently if git is absent.
pushd "%~dp0"
where git >nul 2>&1
if %errorlevel%==0 (
  git fetch origin master >nul 2>&1
  git reset --hard origin/master >nul 2>&1
  "%VPY%" -m pip install -r requirements.txt --quiet >nul 2>&1
  echo App updated - starting...
)
popd

echo Starting the control panel...
echo A browser tab will open at http://localhost:5000
echo (Keep this window open while you work. Close it to stop.)

REM app.py opens the browser itself once Flask is up, so we do NOT open it here
REM (that was causing the tab to open twice).
"%VPY%" "%~dp0app.py"
pause
