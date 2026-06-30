"""
One-off scraper: builds ni_car_db.json — every Make and its Models from the
New India Motor Policy form. Run it once (or whenever the car list changes);
test.py reads the JSON to map a brand that New India lists as a *model*
(e.g. 'MINI COOPER' under 'BMW') back to its real Make.

    python ni_car_scraper.py

Credentials come from .env (NI_USERNAME / NI_PASSWORD), same as test.py.
"""
import json
import os
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Windows console is cp1252 by default and chokes on the ✅/🔑 emojis.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()
NI_USERNAME = os.getenv("NI_USERNAME", "")
NI_PASSWORD = os.getenv("NI_PASSWORD", "")

LOGIN_URL = "https://www.newindiaoman.com/Account/login.aspx"
MOTOR_POLICY_URL = "https://www.newindiaoman.com/AgBr/mtrPolicy.aspx"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ni_car_db.json")

# Text that only appears once the Motor Policy form has rendered.
FORM_MARKERS = ["Show Information", "Vehicle Details", "Primary Information", "Reg.No"]


def find_form_frame(page):
    """Scan every tab + frame, return the one holding the Motor Policy form.
    The form is often inside an <iframe>; postbacks reload it, so re-find each time."""
    for _ in range(20):
        for pg in page.context.pages:
            try:
                frames = pg.frames
            except Exception:
                continue
            for fr in frames:
                try:
                    for m in FORM_MARKERS:
                        if fr.locator(f'xpath=//*[contains(normalize-space(.),"{m}")]').count() > 0:
                            return fr
                except Exception:
                    continue
        page.wait_for_timeout(300)
    return page  # fall back to the top-level page


def open_tab(scope, tab_text):
    """Click a form tab by its text so its fields become visible. The Make/Model
    <select>s live on the Vehicle Details tab and are hidden until it's active —
    select_option needs them visible, hence this."""
    for sel in (f'a:has-text("{tab_text}")', f'span:has-text("{tab_text}")',
                f'td:has-text("{tab_text}")', f'li:has-text("{tab_text}")'):
        try:
            el = scope.locator(sel).first
            if el.count() > 0 and el.is_visible():
                el.click()
                print(f"  ✅  Opened tab '{tab_text}'")
                return True
        except Exception:
            continue
    print(f"  ⚠️  Could not find the '{tab_text}' tab")
    return False


def find_select(scope, label):
    """Find the <select> sitting next to a label containing `label`."""
    getters = [
        lambda: scope.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following-sibling::td[1]//select[1]'
        ).first,
        lambda: scope.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following::select[1]'
        ).first,
        lambda: scope.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::select[1]'
        ).first,
        lambda: scope.get_by_label(label, exact=False).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() > 0:
                return el
        except Exception:
            continue
    return None


def real_options(select_locator):
    """Option texts with blank / 'Select' placeholders stripped out."""
    try:
        texts = select_locator.locator("option").all_inner_texts()
    except Exception:
        return []
    return [t.strip() for t in texts if t.strip() and t.strip().lower() != "select"]


def login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1500)
    pwd = page.locator('input[type="password"]').first
    if not pwd.is_visible(timeout=8000):
        print("  ✅  Already logged in")
        return
    if not NI_USERNAME or not NI_PASSWORD:
        raise RuntimeError("NI_USERNAME / NI_PASSWORD missing from .env")
    print(f"  🔑  Signing in as '{NI_USERNAME}'...")
    for sel in ['input[type="text"]', 'input[id*="user" i]', 'input[name*="user" i]']:
        box = page.locator(sel).first
        if box.is_visible():
            box.click(); box.fill(""); box.type(NI_USERNAME, delay=20)
            break
    pwd.click(); pwd.fill(""); pwd.type(NI_PASSWORD, delay=20)
    clicked = False
    for sel in ['input[type="submit"]', 'button:has-text("Log In")', 'input[value*="Log" i]']:
        b = page.locator(sel).first
        if b.is_visible():
            b.click(); clicked = True; break
    if not clicked:
        pwd.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1500)
    print("  ✅  Logged in")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        try:
            login(page)

            print("── Opening the Motor Policy form ──")
            page.goto(MOTOR_POLICY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            # Manual checkpoint only when run interactively; piped/background runs skip it
            # (the tab-click below handles the setup the pause was for).
            if sys.stdin and sys.stdin.isatty():
                print("\nPlease make sure you are logged in and the Motor Policy form is open "
                      "and visible in the browser. Then come back here and press ENTER to "
                      "continue scraping.")
                input()

            scope = find_form_frame(page)
            open_tab(scope, "Vehicle Details")   # makes the Make/Model selects visible
            page.wait_for_timeout(1000)
            scope = find_form_frame(page)

            make_sel = find_select(scope, "Make")
            if make_sel is None:
                raise RuntimeError("Could not find the Make dropdown — is the form open?")
            makes = real_options(make_sel)
            print(f"Found {len(makes)} makes. Scraping models...\n")

            db = {}
            for make in makes:
                try:
                    scope = find_form_frame(page)              # re-find: postback reloads the frame
                    find_select(scope, "Make").select_option(label=make)
                    page.wait_for_timeout(2000)                # let the Model list reload
                    scope = find_form_frame(page)
                    model_sel = find_select(scope, "Model")
                    models = real_options(model_sel) if model_sel is not None else []
                    db[make] = models
                    print(f"Scraped Make: {make} ({len(models)} models)")
                except Exception as e:
                    db[make] = []
                    print(f"  ⚠️  {make} failed ({e}) — stored empty list, continuing")

            with open(OUT_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps(db, indent=2, ensure_ascii=False))
            print(f"\nDone. Saved to {OUT_FILE}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
