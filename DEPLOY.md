# Putting the automation on an employee laptop

## On your machine (once per laptop you're prepping)

1. Plug in the USB stick.
2. Double-click `deploy-to-usb.ps1` (or run it in PowerShell). It copies the
   project to `<USB>\insurance-automation`, leaving out the venv, your `.env`,
   and customer files.
3. Safely eject.

## On the employee laptop

1. Copy the `insurance-automation` folder from the USB to the laptop
   (e.g. onto the Desktop or `C:\`). Keep the folder name.
2. Double-click **`setup.bat`**. It installs Git, Python, the libraries and the
   browser, and creates a blank `.env`. (Needs internet. ~5–10 min the first time.)
3. Open the new **`.env`** file, fill in `MIC_USERNAME` and `MIC_PASSWORD`, save.
4. **One-time GitHub sign-in (because the repo is private):** open a terminal in
   the folder and run `git pull`. Enter the GitHub username and a Personal Access
   Token when asked. Setup already turned on credential storage, so this is asked
   **only once** — every future launch is silent.
5. Double-click **`start.bat`**. From now on the employee only ever uses
   `start.bat`: it pulls the latest code from GitHub and opens the app.

## Daily use

Employee double-clicks **`start.bat`**. One line ("App updated - starting...")
then the browser opens. Nothing else.

## Notes

- `.env` never leaves your machine and is never on GitHub — each laptop gets its
  own credentials in step 3.
- To skip step 4's token prompt entirely, you can instead bake a read-only token
  into the clone URL, but a one-time `git pull` is simpler and safer.
