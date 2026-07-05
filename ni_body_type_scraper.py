"""
One-off scraper: extracts all Body Type options from the New India Motor Policy form
and saves them to ni_body_types.json. Run this once to see what the actual dropdown
options are, then we can build a Tameen → New India mapping.

    python ni_body_type_scraper.py

Credentials come from .env (NI_USERNAME / NI_PASSWORD), same as test.py and ni_car_scraper.py.
"""
import json
import os
import sys

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()
NI_USERNAME = os.getenv("NI_USERNAME", "")
NI_PASSWORD = os.getenv("NI_PASSWORD", "")

LOGIN_URL = "https://www.newindiaoman.com/Account/login.aspx"
MOTOR_POLICY_URL = "https://www.newindiaoman.com/AgBr/mtrPolicy.aspx"
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ni_body_types.json")

FORM_MARKERS = ["Show Information", "Vehicle Details", "Primary Information", "Reg.No"]


def find_form_frame(page):
    """Scan every tab + frame for the Motor Policy form."""
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
    return page


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


def open_tab(scope, tab_text):
    """Click a form tab by its text."""
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


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_context().new_page()
        try:
            login(page)

            print("── Opening the Motor Policy form ──")
            page.goto(MOTOR_POLICY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)

            if sys.stdin and sys.stdin.isatty():
                print("\nPlease make sure you are logged in and the Motor Policy form is open. "
                      "Press ENTER to continue.")
                input()

            scope = find_form_frame(page)
            open_tab(scope, "Vehicle Details")
            page.wait_for_timeout(1000)
            scope = find_form_frame(page)

            # We need to select a Make and Model first, since Body Type options may
            # depend on what's selected (or the dropdown might be hidden otherwise).
            make_sel = find_select(scope, "Make")
            if make_sel is None:
                raise RuntimeError("Could not find the Make dropdown")

            makes = real_options(make_sel)
            print(f"Found {len(makes)} makes. Scraping Body Types...\n")

            # We'll collect Body Type options from several Make/Model combinations
            # to see if the list varies.
            body_types_set = set()
            body_types_by_make_model = {}

            for make in makes[:5]:  # sample first 5 makes to not take forever
                try:
                    scope = find_form_frame(page)
                    find_select(scope, "Make").select_option(label=make)
                    page.wait_for_timeout(1500)

                    scope = find_form_frame(page)
                    model_sel = find_select(scope, "Model")
                    models = real_options(model_sel) if model_sel else []

                    if not models:
                        continue

                    # Pick the first model
                    first_model = models[0]
                    scope = find_form_frame(page)
                    find_select(scope, "Model").select_option(label=first_model)
                    page.wait_for_timeout(1500)

                    scope = find_form_frame(page)
                    body_type_sel = find_select(scope, "Body Type")
                    body_types = real_options(body_type_sel) if body_type_sel else []

                    key = f"{make} / {first_model}"
                    body_types_by_make_model[key] = body_types
                    body_types_set.update(body_types)

                    print(f"  {make} → {first_model}: {len(body_types)} body types")
                except Exception as e:
                    print(f"  ⚠️  {make} failed: {e}")

            body_types_list = sorted(list(body_types_set))
            print(f"\nFound {len(body_types_list)} unique Body Type options across sampled makes/models:")
            for bt in body_types_list:
                print(f"  • {bt}")

            output = {
                "unique_body_types": body_types_list,
                "by_make_model": body_types_by_make_model,
            }

            with open(OUT_FILE, "w", encoding="utf-8") as f:
                f.write(json.dumps(output, indent=2, ensure_ascii=False))
            print(f"\nSaved to {OUT_FILE}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
