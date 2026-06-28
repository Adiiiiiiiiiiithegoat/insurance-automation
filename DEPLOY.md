# Putting the automation on an employee laptop

## One-time setup on your machine

Create a GitHub **read-only** token and save it so deploys can bake it in:

1. GitHub > Settings > Developer settings > Fine-grained tokens > Generate new.
   - Repository access: only `insurance-automation`.
   - Permissions: **Contents = Read-only**. (Nothing else.)
2. Copy the token into a file named **`deploy-token.txt`** in this project folder.
   (It's gitignored — it never gets committed or pushed.)

## Prepping a USB stick

1. Plug in the USB.
2. Double-click `deploy-to-usb.ps1` (or run it in PowerShell). It copies the
   project to `<USB>\insurance-automation` — including a working `.env` and the
   token-embedded git URL — and leaves out the venv and customer files.
3. Safely eject. Re-run any time to refresh the stick with the latest code.

## On the employee laptop

1. Copy the `insurance-automation` folder from the USB to the laptop
   (e.g. Desktop or `C:\`). Keep the folder name.
2. Double-click **`setup.bat`**. It installs Git, Python, the libraries and the
   browser. (Needs internet. ~5-10 min the first time.)
3. Double-click **`start.bat`**. Done.

That's it — credentials and GitHub access both travel on the stick, so there's
nothing to type.

## Daily use

Employee double-clicks **`start.bat`**. One line ("App updated - starting...")
then the browser opens.

## Security notes

- The token is **read-only** and scoped to this one repo. It sits in plaintext
  in each laptop's `.git/config` and on the USB. If a laptop is lost, revoke the
  token on GitHub and run a new deploy with a fresh one.
- The `.env` (MIC login) also travels on the stick and lives on each laptop.
  Treat the USB like a key to the account.
