# One-time, A-to-Z setup for the Insurance Automation control panel.
# Installs Git (portable, no admin) and Python (per-user, no admin) only if they
# are missing, builds the venv, installs the packages from requirements.txt,
# installs the Chromium browser, writes a blank .env, then SELF-CHECKS the install.
# Run via setup.bat. Safe to run again on an already-working machine (idempotent).

# ── VERSION PINS — if a download fails with 404, replace the URL/version below ──
# Python:      latest stable from https://www.python.org/downloads/windows/
# PortableGit: latest from        https://github.com/git-for-windows/git/releases
$PYTHON_VERSION = '3.12.7'
$PYTHON_URL     = 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'
$GIT_URL        = 'https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/PortableGit-2.47.1-64-bit.7z.exe'

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

    Write-Host "Git was not found. Installing a private copy (no administrator needed)..."

    # PortableGit: a self-extracting archive that unpacks to a folder with NO admin
    # rights and NO Microsoft Store / winget dependency, so it always works silently.
    $exe = Join-Path $env:TEMP 'portablegit.exe'
    $dir = Join-Path $env:LOCALAPPDATA 'Programs\Git'
    try {
        Invoke-WebRequest -Uri $GIT_URL -OutFile $exe -UseBasicParsing
    } catch {
        Write-Host "  Could not download Git. Check the internet connection and re-run setup."
        exit 1
    }
    # 7-Zip self-extractor flags: -o<dir> (extract here), -y (assume yes), silent.
    Start-Process -FilePath $exe -Wait -ArgumentList "-o`"$dir`"", '-y' | Out-Null

    $gitCmd = Join-Path $dir 'cmd'
    if (-not (Test-Path (Join-Path $gitCmd 'git.exe'))) {
        Write-Host "  Git did not extract as expected. Install it by hand from https://git-scm.com/download/win, then re-run setup."
        exit 1
    }

    # Put it on PATH for THIS process (rest of setup uses git) and the USER PATH
    # (so start.bat's `where git` finds it in future windows) without admin.
    $env:Path = "$gitCmd;$env:Path"
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$gitCmd*") {
        [Environment]::SetEnvironmentVariable('Path', "$gitCmd;$userPath", 'User')
    }

    if (Test-Git) {
        Write-Host "Git installed successfully."
    } else {
        Write-Host "  Git was installed but is not visible yet. Close this window, open a new one, and run setup.bat again."
        exit 1
    }
}

Write-Host "============================================================"
Write-Host "   Insurance Automation  -  ONE-TIME SETUP"
Write-Host "============================================================"
Write-Host ""

Ensure-Git
Write-Host ""

$py = Find-Python

if (-not $py) {
    Write-Host "Python was not found on this computer."
    Write-Host "Downloading the official Python $PYTHON_VERSION installer..."
    $exe = Join-Path $env:TEMP 'python-installer.exe'
    try {
        Invoke-WebRequest -Uri $PYTHON_URL -OutFile $exe -UseBasicParsing
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

function Test-VenvUsable([string]$venvPython) {
    # A venv COPIED from another machine has incompatible compiled extensions
    # (e.g. greenlet._greenlet.pyd) and absolute paths to a user that doesn't exist
    # here, yet pip reports every package "already satisfied" and never rebuilds
    # them. So only reuse a venv if its key packages actually IMPORT on THIS machine.
    try {
        & $venvPython -c "import flask, dotenv; from playwright.sync_api import sync_playwright" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$venvPy  = Join-Path $root 'venv\Scripts\python.exe'
$venvDir = Join-Path $root 'venv'
$reuseVenv = $false
if (Test-Path $venvPy) {
    if (Test-VenvUsable $venvPy) {
        Write-Host "Virtual environment already exists and works - reusing it."
        $reuseVenv = $true
    } else {
        Write-Host "Existing venv is broken or was copied from another machine - rebuilding it..."
        Remove-Item $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if (-not $reuseVenv) {
    Write-Host "Creating the virtual environment (venv folder)..."
    Invoke-Py -m venv venv
}
if (-not (Test-Path $venvPy)) {
    Write-Host "  Could not create the virtual environment."
    exit 1
}

# Single source of truth for dependencies is requirements.txt — never a hardcoded
# package list here (that used to drift from what start.bat installed).
$reqFile = Join-Path $root 'requirements.txt'
if (-not (Test-Path $reqFile)) {
    Write-Host "  requirements.txt is missing next to setup. Cannot install packages."
    exit 1
}
Write-Host "Installing the required packages from requirements.txt..."
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r $reqFile
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

# Write a FULL .env template (all three insurers) only if one does not exist yet.
# Never overwrite an .env that already holds real credentials.
if (-not (Test-Path (Join-Path $root '.env'))) {
    Write-Host "Creating a template .env file for your logins..."
    @(
        '# ============================================================',
        '#  Insurance Automation - credentials',
        '#  Fill in your real logins below, then SAVE this file.',
        '#  This file is git-ignored and must NEVER be committed or shared.',
        '# ============================================================',
        '# --- Muscat Insurance (MIC) - REQUIRED (the web control panel uses this) ---',
        'MIC_USERNAME=',
        'MIC_PASSWORD=',
        '# --- New India Assurance - optional (only needed for the New India flow) ---',
        'NI_USERNAME=',
        'NI_PASSWORD=',
        '# --- IRAN Insurance - optional (only needed for the IRAN flow) ---',
        'IRAN_USERNAME=',
        'IRAN_PASSWORD='
    ) | Set-Content -Path (Join-Path $root '.env') -Encoding ASCII
    Write-Host "  A blank .env was created. Open it and fill in at least MIC_USERNAME and MIC_PASSWORD."
}

# Remember credentials after the first pull so employee machines never re-prompt.
git config --global credential.helper store

# ── SELF-CHECK: prove the install actually works before declaring success ──────
# Verifies (1) flask/playwright/dotenv import, (2) the Chromium executable exists
# on disk, (3) app.py is present. Runs in a throwaway temp script (nothing added
# to the repo) and exits non-zero on failure so the employee knows to re-run.
Write-Host ""
Write-Host "Running a quick self-check to confirm everything installed..."
$check = Join-Path $env:TEMP "ia_selfcheck.py"
@'
import sys, os
root = sys.argv[1]
try:
    import flask          # noqa: F401
    import dotenv         # noqa: F401
    from playwright.sync_api import sync_playwright
except Exception as e:
    print("FAIL: a required package did not import: %s" % e); sys.exit(2)
try:
    p = sync_playwright().start()
    exe = p.chromium.executable_path
    p.stop()
    if not exe or not os.path.exists(exe):
        print("FAIL: the Chromium browser is not installed (run setup again)."); sys.exit(3)
except Exception as e:
    print("FAIL: could not verify the Chromium browser: %s" % e); sys.exit(3)
if not os.path.exists(os.path.join(root, "app.py")):
    print("FAIL: app.py is missing next to setup."); sys.exit(4)
print("OK")
'@ | Set-Content -Path $check -Encoding ASCII

$checkOut = & $venvPy $check $root
$checkCode = $LASTEXITCODE
Remove-Item $check -Force -ErrorAction SilentlyContinue

if ($checkCode -ne 0) {
    Write-Host ""
    Write-Host "============================================================"
    Write-Host "   SELF-CHECK FAILED: $checkOut"
    Write-Host "   Please run setup.bat again, or report the line above."
    Write-Host "============================================================"
    exit 1
}

Write-Host ""
Write-Host "============================================================"
Write-Host "   SELF-CHECK PASSED  -  you can now double-click start.bat"
Write-Host "============================================================"
