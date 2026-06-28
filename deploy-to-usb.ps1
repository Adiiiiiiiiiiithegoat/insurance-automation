# Copy this project onto a USB stick for moving to an employee laptop.
# Keeps the .git folder (so start.bat's auto-update works) but EXCLUDES the
# venv, the secret .env, the browser profile, and customer PII — only the code
# + git history travel. Usage:  right-click > Run with PowerShell, or
#   powershell -ExecutionPolicy Bypass -File deploy-to-usb.ps1 [E:]

param([string]$Drive)

$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $Drive) {
    $rem = @(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 })
    if ($rem.Count -eq 0) { Write-Host "No USB drive found. Plug one in, or pass the letter:  deploy-to-usb.ps1 E:"; exit 1 }
    if ($rem.Count -gt 1) { Write-Host "Several USB drives found. Pass the one you want:  deploy-to-usb.ps1 E:"; exit 1 }
    $Drive = $rem[0].DeviceID
}

$dest = Join-Path $Drive 'insurance-automation'

# Wipe any previous copy first. robocopy /E only ADDS/updates files; it would
# leave a stale .env (your credentials!) or old customer files from an earlier
# deploy sitting on the stick. The name guard keeps us from deleting the drive root.
if ((Split-Path $dest -Leaf) -eq 'insurance-automation' -and (Test-Path $dest)) {
    Write-Host "Clearing the previous copy on the USB..."
    Remove-Item $dest -Recurse -Force
}

Write-Host "Copying project to $dest (this can take a minute)..."

# .git is NOT excluded, so the copy stays a real clone with 'origin' already set.
robocopy $src $dest /E /R:1 /W:1 /NFL /NDL /NJH /NJS `
    /XD venv automation_profile screenshots iran_uploads __pycache__ .pytest_cache `
    /XF .env *.pyc | Out-Null

if ($LASTEXITCODE -ge 8) { Write-Host "robocopy reported a problem (code $LASTEXITCODE)."; exit 1 }
Write-Host "Done. Safely eject the USB, then follow the laptop steps in DEPLOY.md."
