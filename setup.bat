@echo off
REM One-time setup. The real work is done by setup.ps1 (PowerShell handles
REM detecting/installing Python far more reliably than batch, including ignoring
REM the Microsoft Store "python" stub on fresh Windows machines).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
echo.
pause
