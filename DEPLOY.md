# Deploying the Insurance Automation control panel

## On your (developer) machine — prep a USB stick

1. **One-time:** create a GitHub **read-only** fine-grained token scoped to only
   the `insurance-automation` repo (Permissions: **Contents = Read-only**). Save it
   in a file named **`deploy-token.txt`** in this folder. It is gitignored — never
   committed.
2. Plug in the USB.
3. Run **`deploy-to-usb.ps1`** (right-click → Run with PowerShell, or
   `powershell -ExecutionPolicy Bypass -File deploy-to-usb.ps1 E:`). It copies the
   project to `<USB>\insurance-automation`, keeps `.git` and `.env`, leaves out the
   venv / browser profile / customer files, and bakes the read-only token into the
   copy's `origin` so laptops auto-update with no sign-in.
4. Safely eject. Re-run any time to refresh the stick with the latest code.

## On a fresh employee laptop (no Python, no Git, no admin needed)

1. Copy the `insurance-automation` folder off the USB to the laptop (keep the name).
2. Double-click **`setup.bat`** once. It installs Git and Python (both per-user, no
   admin), the libraries from `requirements.txt`, and the Chromium browser, then
   runs a **self-check**. Wait for **`SELF-CHECK PASSED`**. (~5–10 min, needs
   internet.) If it prints `SELF-CHECK FAILED`, just run `setup.bat` again.
3. Open **`.env`** and fill in at least `MIC_USERNAME` / `MIC_PASSWORD` (New India
   and IRAN logins are optional — only needed for those flows).
4. Double-click **`start.bat`**. The control panel opens at
   `http://localhost:5000`.

## Daily use

Employee double-clicks **`start.bat`**. It pulls the latest code from GitHub,
prints "App updated - starting...", then the browser opens. Keep the console
window open while working; close it to stop.

> **Note:** `start.bat` does `git reset --hard` on every launch so each laptop
> always matches the repo. Any local edits made on a laptop are wiped on purpose —
> make changes in the repo and redeploy, not on the laptop.

## Security notes

- The baked token is **read-only** and scoped to this one repo, but it sits in
  plaintext in each laptop's `.git/config` and on the USB. If a laptop is lost,
  revoke the token on GitHub and run a fresh deploy.
- The `.env` (insurer logins) also travels on the stick and lives on each laptop.
  Treat the USB like a key to the accounts.
