# Copy this project onto a USB stick for moving to an employee laptop.
# Keeps the .git folder (so start.bat's auto-update works) and the .env (so the
# employee has working credentials), but EXCLUDES the venv, browser profile, and
# customer PII. If deploy-token.txt exists, its read-only GitHub token is baked
# into the USB copy's 'origin' so the laptop auto-updates with NO sign-in.
# Usage:  right-click > Run with PowerShell, or
#   powershell -ExecutionPolicy Bypass -File deploy-to-usb.ps1 [E:]

param([string]$Drive, [string]$Token)

$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoUrl = 'github.com/Adiiiiiiiiiiithegoat/insurance-automation.git'

if (-not $Drive) {
    $rem = @(Get-CimInstance Win32_LogicalDisk | Where-Object { $_.DriveType -eq 2 })
    if ($rem.Count -eq 0) { Write-Host "No USB drive found. Plug one in, or pass the letter:  deploy-to-usb.ps1 E:"; exit 1 }
    if ($rem.Count -gt 1) { Write-Host "Several USB drives found. Pass the one you want:  deploy-to-usb.ps1 E:"; exit 1 }
    $Drive = $rem[0].DeviceID
}

$dest = Join-Path $Drive 'insurance-automation'

# Wipe any previous copy first. robocopy /E only ADDS/updates files; it would
# leave old customer files from an earlier deploy sitting on the stick. The name
# guard keeps us from deleting the drive root.
if ((Split-Path $dest -Leaf) -eq 'insurance-automation' -and (Test-Path $dest)) {
    Write-Host "Clearing the previous copy on the USB..."
    Remove-Item $dest -Recurse -Force
}

Write-Host "Copying project to $dest (this can take a minute)..."

# .git is NOT excluded, so the copy stays a real clone with 'origin' already set.
# .env IS copied so the employee gets working credentials. PII/profile/venv are not.
robocopy $src $dest /E /R:1 /W:1 /NFL /NDL /NJH /NJS `
    /XD venv automation_profile screenshots iran_uploads __pycache__ .pytest_cache `
    /XF *.pyc deploy-token.txt | Out-Null

if ($LASTEXITCODE -ge 8) { Write-Host "robocopy reported a problem (code $LASTEXITCODE)."; exit 1 }

# Bake a read-only token into the USB copy's origin so the laptop never asks to
# sign in. Token comes from -Token or a gitignored deploy-token.txt; it is NEVER
# written into the source repo, only into the copied .git on the stick.
# safe.directory=* is needed because the USB (exFAT) doesn't record ownership.
if (-not $Token) {
    $tf = Join-Path $src 'deploy-token.txt'
    if (Test-Path $tf) { $Token = (Get-Content $tf -Raw).Trim() }
}
if ($Token) {
    & git -c "safe.directory=*" -C $dest remote set-url origin "https://$Token@$RepoUrl"
    Write-Host "Read-only token baked into the USB copy - laptops update with no sign-in."
} else {
    Write-Host "No token found (deploy-token.txt) - the laptop will ask for a GitHub sign-in once."
}

Write-Host "Done. Safely eject the USB, then follow the laptop steps in DEPLOY.md."
