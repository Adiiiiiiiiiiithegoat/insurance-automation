@echo off
setlocal enabledelayedexpansion
echo ============================================================
echo    Muscat Insurance Automation  -  ONE-TIME SETUP
echo ============================================================
echo.

REM ---- 1. Find a Python we can use --------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)

if not defined PY (
  echo Python was not found on this computer.
  echo Downloading the official Python 3 installer...
  set "PYURL=https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe"
  set "PYEXE=%TEMP%\python-installer.exe"
  REM !..! (delayed expansion) — %..% would expand to empty here, inside the block.
  powershell -Command "Invoke-WebRequest -Uri '!PYURL!' -OutFile '!PYEXE!'"
  if errorlevel 1 (
    echo.
    echo   Could not download Python. Please install Python 3 from
    echo   https://www.python.org/downloads/  then run setup.bat again.
    pause & exit /b 1
  )
  echo Installing Python just for you ^(no administrator needed^)...
  "!PYEXE!" /quiet PrependPath=1 Include_pip=1
  REM Do NOT rely on PATH being refreshed in this same window. Locate python directly.
  set "PY="
  where py >nul 2>nul && set "PY=py"
  if not defined PY (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
      set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    )
  )
  if not defined PY (
    echo.
    echo   Python was installed but could not be located in this window.
    echo   Please CLOSE this window, open a NEW one, and run setup.bat again.
    pause & exit /b 1
  )
)

echo Using Python: %PY%
echo.

REM ---- 2. Create the virtual environment -------------------------------------
echo Creating the virtual environment (venv folder)...
%PY% -m venv venv
if errorlevel 1 (
  echo   Could not create the virtual environment. & pause & exit /b 1
)
set "VPY=%~dp0venv\Scripts\python.exe"

echo Installing the required packages (flask, playwright, python-dotenv)...
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install flask playwright python-dotenv
if errorlevel 1 (
  echo   Installing the packages failed. Check your internet connection and retry. & pause & exit /b 1
)

echo Installing the Chromium browser that the automation drives...
"%VPY%" -m playwright install chromium
if errorlevel 1 (
  echo   Installing Chromium failed. Check your internet connection and retry. & pause & exit /b 1
)

REM ---- 3. Create a .env template if there isn't one --------------------------
if not exist "%~dp0.env" (
  echo Creating a template .env file for your login...
  > "%~dp0.env" echo # Fill in your Muscat Insurance (MIC) login below, then SAVE this file.
  >> "%~dp0.env" echo MIC_USERNAME=
  >> "%~dp0.env" echo MIC_PASSWORD=
  echo   A blank .env was created. Open it and fill in MIC_USERNAME and MIC_PASSWORD.
)

echo.
echo ============================================================
echo    Setup complete  -  you can now double-click start.bat
echo ============================================================
pause
