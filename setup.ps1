# One-time, A-to-Z setup for the Muscat Insurance automation.
# Detects a REAL Python (ignoring the Microsoft Store stub), installs Python
# itself if none is found, then builds the venv, installs the packages and the
# Chromium browser, and creates a blank .env. Run via setup.bat.

$ErrorActionPreference = 'Stop'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Test-RealPython([string[]]$invoke) {
    # Returns $true if running '<invoke> -c ...' actually works (a real Python,
    # not the Microsoft Store stub which just prints a "not found" message).
    try {
        $exe = $invoke[0]
        $rest = @()
        if ($invoke.Count -gt 1) { $rest = $invoke[1..($invoke.Count - 1)] }
        $out = & $exe @rest '-c' 'import sys; print(sys.version)' 2>$null
        return ($LASTEXITCODE -eq 0 -and $out -match '3\.')
    } catch { return $false }
}

function Find-Python {
    # 1) The 'py' launcher (comes with python.org installs; never the Store stub).
    if (Test-RealPython @('py', '-3')) { return @('py', '-3') }

    # 2) 'python' on PATH, but skip the Microsoft Store alias in WindowsApps.
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and ($cmd.Source -notlike '*WindowsApps*')) {
        if (Test-RealPython @($cmd.Source)) { return @($cmd.Source) }
    }

    # 3) Known install folders (covers "installed but not on PATH yet").
    $bases = @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles", "${env:ProgramFiles(x86)}")
    foreach ($b in $bases) {
        if ($b -and (Test-Path $b)) {
            $dirs = Get-ChildItem $b -Filter 'Python3*' -Directory -ErrorAction SilentlyContinue |
                    Sort-Object Name -Descending
            foreach ($d in $dirs) {
                $p = Join-Path $d.FullName 'python.exe'
                if ((Test-Path $p) -and (Test-RealPython @($p))) { return @($p) }
            }
        }
    }

    # 4) The exact install path Python recorded in the registry (most reliable
    #    right after a fresh install, when PATH isn't refreshed yet).
    foreach ($hive in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore') {
        if (Test-Path $hive) {
            foreach ($k in (Get-ChildItem $hive -ErrorAction SilentlyContinue)) {
                try {
                    $ip = (Get-ItemProperty -Path (Join-Path $k.PSPath 'InstallPath') -ErrorAction Stop).'(default)'
                    if ($ip) {
                        $p = Join-Path $ip 'python.exe'
                        if ((Test-Path $p) -and (Test-RealPython @($p))) { return @($p) }
                    }
                } catch {}
            }
        }
    }
    return $null
}

function Update-PathFromRegistry {
    # Pull the latest Machine+User PATH into THIS process so a just-installed
    # Python / py launcher becomes visible without opening a new window.
    try {
        $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $u = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = (@($m, $u) | Where-Object { $_ }) -join ';'
    } catch {}
}

function Test-Git {
    try { $null = & git --version 2>$null; return ($LASTEXITCODE -eq 0) } catch { return $false }
}

function Ensure-Git {
    # Make sure Git is on the machine (needed for start.bat's auto-update).
    if (Test-Git) { Write-Host "Git already installed - skipping."; return }

    Write-Host "Git was not found. Installing Git for Windows..."

    # Prefer winget: silent, no version to pin, installs per-user (no admin popup).
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install --id Git.Git -e --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        Update-PathFromRegistry
        if (Test-Git) { Write-Host "Git installed successfully."; return }
    }

    # Fallback: download the official Git for Windows installer and run it silently.
    # ponytail: pinned version like the Python installer above; bump when it rots.
    $url = 'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe'
    $exe = Join-Path $env:TEMP 'git-installer.exe'
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    } catch {
        Write-Host "  Could not download Git. Install it by hand from https://git-scm.com/download/win, then re-run setup."
        exit 1
    }
    Start-Process -FilePath $exe -Wait -ArgumentList @(
        '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES', '/NOCANCEL'
    ) | Out-Null
    Update-PathFromRegistry
    if (Test-Git) {
        Write-Host "Git installed successfully."
    } else {
        Write-Host "  Git was installed but is not visible yet. Close this window, open a new one, and run setup.bat again."
        exit 1
    }
}

Write-Host "============================================================"
Write-Host "   Muscat Insurance Automation  -  ONE-TIME SETUP"
Write-Host "============================================================"
Write-Host ""

Ensure-Git
Write-Host ""

$py = Find-Python

if (-not $py) {
    Write-Host "Python was not found on this computer."
    Write-Host "Downloading the official Python 3 installer..."
    $url = 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'
    $exe = Join-Path $env:TEMP 'python-installer.exe'
    try {
        Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    } catch {
        Write-Host ""
        Write-Host "  Could not download Python. Check your internet connection and"
        Write-Host "  run setup again. (Or install Python 3 from python.org by hand.)"
        exit 1
    }
    Write-Host "Installing Python just for you (no administrator needed). Please wait..."
    $proc = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_pip=1', 'Include_launcher=1', 'AssociateFiles=0'
    )
    Write-Host "Python installer finished (exit code $($proc.ExitCode))."

    # Detect the just-installed Python IN THIS SAME RUN. PATH/py launcher aren't
    # visible to the current process yet, so refresh PATH from the registry and
    # retry a few times (also rides out any brief post-install file locking).
    Write-Host "Locating the new Python installation..."
    for ($i = 0; $i -lt 12; $i++) {
        Update-PathFromRegistry
        $py = Find-Python
        if ($py) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $py) {
        Write-Host ""
        Write-Host "  Python was installed but could not be detected yet."
        Write-Host "  Please CLOSE this window, open a NEW one, and run setup.bat again."
        Write-Host "  (That second run will finish the setup.)"
        exit 1
    }
    Write-Host "Python is ready."
}

Write-Host "Using Python: $($py -join ' ')"
Write-Host ""

# Helper to call the chosen Python with arbitrary args.
$pyExe  = $py[0]
$pyArgs = @()
if ($py.Count -gt 1) { $pyArgs = $py[1..($py.Count - 1)] }
function Invoke-Py { & $pyExe @pyArgs @args }

$venvPy = Join-Path $root 'venv\Scripts\python.exe'
if ((Test-Path $venvPy) -and (Test-RealPython @($venvPy))) {
    Write-Host "Virtual environment already exists - reusing it."
} else {
    Write-Host "Creating the virtual environment (venv folder)..."
    Invoke-Py -m venv venv
}
if (-not (Test-Path $venvPy)) {
    Write-Host "  Could not create the virtual environment."
    exit 1
}

Write-Host "Installing the required packages (flask, playwright, python-dotenv)..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install flask playwright python-dotenv
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing the packages failed. Check your internet connection and retry."
    exit 1
}

Write-Host "Installing the Chromium browser that the automation drives..."
& $venvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host "  Installing Chromium failed. Check your internet connection and retry."
    exit 1
}

if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Host "Creating a template .env file for your login..."
    @(
        '# Fill in your Muscat Insurance (MIC) login below, then SAVE this file.',
        'MIC_USERNAME=',
        'MIC_PASSWORD='
    ) | Set-Content -Path (Join-Path $root '.env') -Encoding ASCII
    Write-Host "  A blank .env was created. Open it and fill in MIC_USERNAME and MIC_PASSWORD."
}

Write-Host ""
Write-Host "============================================================"
Write-Host "   Setup complete  -  you can now double-click start.bat"
Write-Host "============================================================"

# Remember credentials after the first pull so employee machines never re-prompt.
git config --global credential.helper store
Write-Host ""
Write-Host "Setup complete. You can now double-click start.bat to launch the app."
