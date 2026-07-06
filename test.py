from playwright.sync_api import sync_playwright
import os
from common import (
    MIC_HOME_URL,
    read_field, parse_tameen_date, compute_period_from, split_plate,
    expiry_far_off, enable_download_dialogs,
    tameen_go_to_payments, tameen_click_payments_by_channel,
    tameen_select_channel, tameen_reset_to_payments,
    mic_login_if_needed, mic_open_policy_create, mic_choose_policy_type_and_create,
    mic_get_licence, mic_fill_policy_info, mic_get_vehicle,
    mic_fill_vehicle_info, mic_calculate_and_check, mic_reset_to_home,
    NI_LOGIN_URL,
    reformat_plate_for_ni, compute_commencing_date_ni, read_tameen_addons,
    ni_login_if_needed, ni_go_to_motor_policy,
    ni_fill_primary_top, ni_fill_primary_client, ni_fill_previous_policy,
    ni_fill_vehicle_details, ni_fill_premium_calculation, ni_reset_to_motor_policy,
)


# ══════════════════════════════════════════════════════════════════════════════
#  CREDENTIALS  —  stored OUTSIDE this file for safety
# ══════════════════════════════════════════════════════════════════════════════
#  Create a file next to this one called  .env  containing exactly these two
#  lines (with your REAL, CHANGED password):
#
#       MIC_USERNAME=your_username_here
#       MIC_PASSWORD=your_new_password_here
#       NI_USERNAME=your_new_india_username_here
#       NI_PASSWORD=your_new_india_password_here
#       IRAN_USERNAME=your_iran_login_email_here
#       IRAN_PASSWORD=your_iran_password_here
#
#  .env is listed in .gitignore so it never gets uploaded anywhere.
#  Never upload .env to Claude, to a project, or to GitHub.
# ──────────────────────────────────────────────────────────────────────────────


# MIC configuration + all shared MIC/Tameen helpers now live in common.py.



# ══════════════════════════════════════════════════════════════════════════════
#  IRAN INSURANCE CO  —  CONFIGURATION  (third insurer, runs alongside MIC + NI)
# ══════════════════════════════════════════════════════════════════════════════
#  .env needs two more lines (never committed — see the header above):
#       IRAN_USERNAME=your_iran_login_email_here
#       IRAN_PASSWORD=your_iran_password_here
IRAN_USERNAME = os.getenv("IRAN_USERNAME", "")
IRAN_PASSWORD = os.getenv("IRAN_PASSWORD", "")
if not IRAN_USERNAME or not IRAN_PASSWORD:
    print("⚠️  IRAN credentials not found in .env — login fields will be left for you "
          "to type by hand during the CAPTCHA pause.")

IRAN_LOGIN_URL        = "https://ecrm-portal.com:92/"
IRAN_DASHBOARD_URL    = "https://ecrm-portal.com:92/User/Home/Dashboard"
IRAN_STEP_PAUSE       = 700          # ms wait after each action; raise if fields get skipped
IRAN_FIXED_MOBILE     = "99435202"   # always
IRAN_FIXED_ADDRESS    = "Muscat"     # always
IRAN_TRANSACTION_TYPE = "Renewal"    # always
IRAN_ASSURED_TYPE     = "Civil ID"   # always
IRAN_COMPANY_FILTER   = "iran"       # ⚠️ CONFIRM exact company text shown on Tameen rows

# Tameen add-on keyword -> IRAN "Choose Your Plan" checkbox label.
# ⚠️ CONFIRM these mappings against the real Tameen add-on names during testing.
# IRAN plan checkbox labels seen on screen: "PAB Cover for Driver Only",
# "Pab Cover To Passenger", "RSA Cover", "Orange Card Coverage".
IRAN_ADDON_MAP = {
    "rsa":           "RSA Cover",
    "roadside":      "RSA Cover",
    "road side":     "RSA Cover",
    "orange":        "Orange Card Coverage",
    "pab driver":    "PAB Cover for Driver Only",
    "pab passenger": "Pab Cover To Passenger",
}

# Always ticked on every IRAN policy regardless of Tameen add-ons — standard
# PAB cover for driver (OMR 1.000) and passengers (OMR 3.000), not optional extras.
IRAN_PLAN_ALWAYS_TICK = ["PAB Cover for Driver Only", "Pab Cover To Passenger"]


# ══════════════════════════════════════════════════════════════════════════════
#  TAMEEN: select-and-open the record (MIC + New India variant)
# ══════════════════════════════════════════════════════════════════════════════
#  NEW FLOW (replaces the old date-filter approach that did not work):
#    Step 1 — click the PAYMENTS tile
#    Step 2 — click the red "Payments by Channel" button (top-right)
#    Step 3 — pick a channel by number (record counts are shown in the terminal)
#    Step 4 — pick a row by number (only Muscat Insurance rows are shown)
# ──────────────────────────────────────────────────────────────────────────────

# The six channels, in the order they appear on the "Payments by Channel" page.

# The two section headings on that page.


def tameen_select_and_click_eye(page):
    """Step 4: list the rows we can process (Muscat Insurance, New India AND IRAN)
    and ask which to open.

    Each row is tagged so you can see which insurer it belongs to, and the chosen
    insurer is returned so the main flow knows which company's form to fill.

    Returns a (status, record_text, company) tuple:
      ("BACK", None, None)            — user typed 0 (go back to the channel select)
      ("OK", "<row text>", "MIC")     — a Muscat Insurance record was opened
      ("OK", "<row text>", "NEW_INDIA") — a New India record was opened
      ("OK", "<row text>", "IRAN")    — an IRAN Insurance record was opened
    """
    print("\n── Tameen Step 4: Select which record to open (Muscat Insurance + New India + IRAN) ──")
    page.wait_for_timeout(1500)

    rows_data = page.evaluate("""
        () => {
            function cellsOf(row, cellSel){ return Array.from(row.querySelectorAll(cellSel)); }
            const tableRows = document.querySelectorAll('table tbody tr');
            if (tableRows.length > 0) {
                const headers = Array.from(document.querySelectorAll('table thead th, table thead td'))
                    .map(h => h.innerText.trim().toLowerCase());
                const rows = Array.from(tableRows).map((row, domIdx) => {
                    const cells = cellsOf(row, 'td').slice(1);
                    return { text: cells.map(c => c.innerText.trim()).filter(Boolean).join('  |  '),
                             domIdx, cells: cells.map(c => c.innerText.trim()) };
                });
                return { type:'html', headers, rows };
            }
            const headerRow = document.querySelector('[role="row"]:has([role="columnheader"])');
            const headers = headerRow ? Array.from(headerRow.querySelectorAll('[role="columnheader"]'))
                    .map(h => h.innerText.trim().toLowerCase()) : [];
            const allRows = Array.from(document.querySelectorAll('[role="row"], [class*="tr"]'))
                .filter(row => !row.querySelector('[role="columnheader"], th, [class*="th"]') && !row.closest('thead'));
            if (allRows.length > 0) {
                const rows = Array.from(allRows).map((row, domIdx) => {
                    const cells = Array.from(row.querySelectorAll('[role="cell"], [class*="td"], td')).slice(1);
                    return { text: cells.map(c => c.innerText.trim()).filter(Boolean).join('  |  '),
                             domIdx, cells: cells.map(c => c.innerText.trim()) };
                });
                return { type:'div', headers, rows };
            }
            return { type:'none', headers:[], rows:[] };
        }
    """)
    if not rows_data or not rows_data.get("rows"):
        raise RuntimeError("Could not read any table rows to display.")

    headers  = rows_data.get("headers", [])
    all_rows = rows_data["rows"]
    company_col_idx = next((i for i, h in enumerate(headers[1:], start=0) if "company" in h.lower()), None)

    def company_of(r):
        """Return 'MIC' for a Muscat Insurance row, 'NEW_INDIA' for a New India
        row, 'IRAN' for an IRAN Insurance row, or None for any other company
        (those rows are hidden)."""
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        cv = cell_val.lower()
        if "muscat insurance" in cv:
            return "MIC"
        if "new india" in cv:
            return "NEW_INDIA"
        if IRAN_COMPANY_FILTER in cv:
            return "IRAN"
        return None

    # Keep only the rows whose insurer we know how to process, and remember which
    # insurer each one is so we can route to the right flow after it is opened.
    filtered = []
    for r in all_rows:
        comp = company_of(r)
        if comp is not None:
            r["company"] = comp
            filtered.append(r)
    if not filtered:
        print(f"  ⚠️  No Muscat Insurance, New India or IRAN rows found. Showing all {len(all_rows)} rows.")
        for r in all_rows:
            r["company"] = company_of(r)   # may be None for unknown companies
        filtered = all_rows

    TAG = {"MIC": "[MIC]      ", "NEW_INDIA": "[New India]", "IRAN": "[IRAN]     ", None: "[Other]    "}
    print("\n" + "=" * 70)
    print(f"  RECORDS WE CAN PROCESS  ({len(filtered)} of {len(all_rows)} total rows)")
    print("=" * 70)
    for i, r in enumerate(filtered, start=1):
        tag = TAG.get(r.get("company"), "[?]        ")
        print(f"  [{i:>2}]  {tag}  {r['text'] or '(no text)'}")
    print("-" * 70)
    print("  [ 0]  ⤴  Go back (re-open 'Payments by Channel' → choose another channel)")
    print("=" * 70)

    while True:
        raw = input(f"\nEnter row number to open (1–{len(filtered)}), or 0 to go back: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("  Please enter a valid number in range.")
            continue
        if choice == 0:
            print("\n  ⤴  Going back to the channel select...")
            return "BACK", None, None
        if 1 <= choice <= len(filtered):
            break
        print("  Please enter a valid number in range.")

    selected = filtered[choice - 1]
    idx      = selected["domIdx"]
    print(f"\n  Opening: {selected['text'] or '(no text)'}")

    try:
        result = page.evaluate("""
            (idx) => {
                const tableRows = document.querySelectorAll('table tbody tr');
                if (tableRows.length > idx) {
                    const firstCell = tableRows[idx].querySelector('td');
                    if (firstCell) {
                        (firstCell.querySelector('button')||firstCell.querySelector('[role="button"]')||
                         firstCell.querySelector('a')||firstCell.querySelector('svg')||
                         firstCell.querySelector('i')||firstCell).click();
                        return 'clicked';
                    }
                }
                const allRows = Array.from(document.querySelectorAll('[role="row"], [class*="tr"]'))
                    .filter(row => !row.querySelector('[role="columnheader"], th, [class*="th"]') && !row.closest('thead'));
                if (allRows.length > idx) {
                    const firstCell = allRows[idx].querySelector('[role="cell"], [class*="td"], td') || allRows[idx].children[0];
                    if (firstCell) {
                        (firstCell.querySelector('button')||firstCell.querySelector('[role="button"]')||
                         firstCell.querySelector('a')||firstCell.querySelector('svg')||
                         firstCell.querySelector('i')||firstCell).click();
                        return 'clicked';
                    }
                }
                return null;
            }
        """, idx)
        if result:
            # 'domcontentloaded' (page structure ready), NOT 'networkidle'. The
            # Tameen dashboard keeps background traffic running forever, so the
            # network never falls silent — waiting for 'networkidle' here used to
            # hang for the full default timeout. Every other step already uses
            # 'domcontentloaded'; this was the last holdout.
            page.wait_for_load_state("domcontentloaded")
            print("  ✅  Opened record")
            return "OK", selected["text"], selected.get("company")
    except Exception:
        pass
    raise RuntimeError(f"Could not open row {choice}.")


# ══════════════════════════════════════════════════════════════════════════════
#  IRAN INSURANCE (INSURER #3)  —  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
#  IRAN's site (ecrm-portal.com:92) is a modern SPA with custom (Select2-style)
#  dropdowns and a 4-tab motor form. Same golden rules as New India: wait after
#  EVERY action (iran_settle) and re-find every element on every call. Each helper
#  prints a plain ✅/⚠️ line so a non-technical operator can see what happened.
# ──────────────────────────────────────────────────────────────────────────────

def iran_settle(page) -> None:
    """Wait for the page to settle after an action, then pause."""
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(IRAN_STEP_PAUSE)


def iran_fill_by_label(page, label: str, value: str, press_escape: bool = False) -> bool:
    """Type a value into the text box next to `label`. Tries table layouts, real
    <label> associations, placeholders, then a broad text-then-input search.
    Set press_escape=True for date fields to close the date-picker. Returns bool."""
    value = str(value)
    getters = [
        # table: label cell -> input in the very next cell (most precise)
        lambda: page.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]//input[not(@type="hidden")][1]'
        ).first,
        # flex/label layouts: a leaf cell containing the label -> next cell's input
        lambda: page.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following-sibling::td[1]//input[not(@type="hidden")][1]'
        ).first,
        # a proper <label for> association
        lambda: page.get_by_label(label, exact=False).first,
        # placeholder text
        lambda: page.get_by_placeholder(label, exact=False).first,
        # label/text node -> the next input after it (kept LAST; can jump fields)
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() == 0:
                continue
            el.scroll_into_view_if_needed(timeout=10000)
            el.click()
            el.press("Control+a")
            el.press("Backspace")
            el.type(value, delay=20)
            if press_escape:
                el.press("Escape")
            print(f"  ✅  Filled '{label}' = {value}")
            iran_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not fill '{label}' — please set it by hand.")
    return False


def _iran_read_value(page, label: str) -> str:
    """Read the current value of the input next to `label` (for auto-fill polling)."""
    getters = [
        lambda: page.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]//input[not(@type="hidden")][1]'
        ).first,
        lambda: page.get_by_label(label, exact=False).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() == 0:
                continue
            return (el.input_value() or "").strip()
        except Exception:
            continue
    return ""


def iran_wait_for_value(page, label: str, tries: int = 35) -> bool:
    """Poll until the field next to `label` has a value (an auto-fill has landed).
    Gives it plenty of time — IRAN's chassis/Civil-ID lookups are slow."""
    print(f"  …  waiting for '{label}' to auto-fill…")
    for _ in range(tries):
        try:
            v = _iran_read_value(page, label)
            if v:
                print(f"  ✅  '{label}' auto-filled: {v}")
                return True
        except Exception:
            pass
        page.wait_for_timeout(IRAN_STEP_PAUSE)
    print(f"  ⚠️  '{label}' did not auto-fill in time — check the IRAN tab / enter it by hand.")
    return False


def iran_select(page, label: str, option_text: str) -> bool:
    """Pick `option_text` in the custom (Select2-style) dropdown next to `label`:
    open the control, type into its search box if there is one, then click the
    matching option (exact text first, then contains). Plain <select> handled too."""
    openers = [
        lambda: page.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]'
            f'//*[contains(@class,"select2") or self::select][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]'
            f'/following::*[contains(@class,"select2-selection") or contains(@class,"select2-choice") or self::select][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]'
            f'/following::*[contains(@class,"select2-selection") or contains(@class,"select2-choice") or self::select][1]'
        ).first,
    ]
    control = None
    for o in openers:
        try:
            el = o()
            if el.count() > 0 and el.first.is_visible():
                control = el.first
                break
        except Exception:
            continue
    if control is None:
        print(f"  ⚠️  Could not find the '{label}' dropdown — please pick '{option_text}' by hand.")
        return False

    # Plain <select>? Use the native API.
    try:
        if (control.evaluate("e => e.tagName") or "").upper() == "SELECT":
            control.select_option(label=option_text)
            print(f"  ✅  Selected '{label}' = {option_text}")
            iran_settle(page)
            return True
    except Exception:
        pass

    # Custom dropdown: open, (optional) search, click option.
    try:
        control.scroll_into_view_if_needed(timeout=8000)
        control.click()
        page.wait_for_timeout(300)
        try:
            sb = page.locator('.select2-search__field, .select2-search input, input.select2-search__field').first
            if sb.count() > 0 and sb.is_visible():
                sb.type(option_text, delay=20)
                page.wait_for_timeout(400)
        except Exception:
            pass
        opt = page.locator(f'xpath=//li[normalize-space(.)="{option_text}"]').first
        if opt.count() == 0 or not opt.is_visible():
            opt = page.locator(f'xpath=//li[contains(normalize-space(.),"{option_text}")]').first
        if opt.count() == 0:
            opt = page.get_by_role("option", name=option_text, exact=False).first
        opt.scroll_into_view_if_needed(timeout=8000)
        opt.click()
        print(f"  ✅  Selected '{label}' = {option_text}")
        iran_settle(page)
        return True
    except Exception:
        print(f"  ⚠️  Could not select '{label}' = {option_text} — please pick it by hand.")
        return False


def iran_set_toggle_yes(page, label: str) -> bool:
    """Set a Yes/No toggle to Yes — only clicks it if it currently reads 'No'."""
    try:
        ctrl = page.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]'
            f'/following::*[contains(@class,"switch") or contains(@class,"toggle") or @role="switch"][1]'
        ).first
        if ctrl.count() == 0:
            ctrl = page.locator(
                f'xpath=//*[contains(normalize-space(text()),"{label}")]/following::*[normalize-space(.)="No"][1]'
            ).first
        if ctrl.count() == 0:
            print(f"  ⚠️  Could not find the '{label}' toggle — please set it to Yes by hand.")
            return False
        txt = (ctrl.inner_text() or "").strip().lower()
        if "yes" in txt and "no" not in txt:
            print(f"  ℹ️  '{label}' is already Yes")
            return True
        ctrl.scroll_into_view_if_needed(timeout=8000)
        ctrl.click()
        print(f"  ✅  Set '{label}' = Yes")
        iran_settle(page)
        return True
    except Exception:
        print(f"  ⚠️  Could not set '{label}' = Yes — please toggle it by hand.")
        return False


def iran_tick_plan_addon(page, checkbox_label: str) -> bool:
    """Tick a 'Choose Your Plan' add-on checkbox by its label. Only ticks ON."""
    getters = [
        lambda: page.get_by_label(checkbox_label, exact=False).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.),"{checkbox_label}")]/preceding::input[@type="checkbox"][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.),"{checkbox_label}")]/following::input[@type="checkbox"][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//*[contains(normalize-space(.),"{checkbox_label}")]/preceding::input[@type="checkbox"][1]'
        ).first,
    ]
    for g in getters:
        try:
            cb = g()
            if cb.count() == 0:
                continue
            cb.scroll_into_view_if_needed(timeout=8000)
            if not cb.is_checked():
                try:
                    cb.check()
                except Exception:
                    cb.click()
            print(f"  ✅  Ticked plan add-on '{checkbox_label}'")
            iran_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not tick plan add-on '{checkbox_label}' — please tick it by hand if needed.")
    return False


def iran_click_button(page, text: str) -> bool:
    """Click a VISIBLE button/link/input by its text or value, then settle."""
    getters = [
        lambda: page.get_by_role("button", name=text, exact=False).first,
        lambda: page.locator(f'button:has-text("{text}")').first,
        lambda: page.locator(f'a:has-text("{text}")').first,
        lambda: page.locator(
            f'input[type="button"][value*="{text}" i], input[type="submit"][value*="{text}" i]'
        ).first,
        lambda: page.locator(
            f'xpath=//*[self::button or self::a or self::span or self::div][normalize-space(.)="{text}"]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() == 0 or not el.first.is_visible():
                continue
            el.first.scroll_into_view_if_needed(timeout=8000)
            el.first.click()
            print(f"  ✅  Clicked '{text}'")
            iran_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not click '{text}' — please click it by hand.")
    return False


def iran_dismiss_popup_ok(page, tries: int = 8) -> bool:
    """Close an error/info dialog by clicking its OK / Ok / Close button. Polls for
    the dialog since it animates in a moment after the triggering click — checking
    only once missed it (same lesson as mic_accept_confirm_dialog; see
    errorlog.md.txt entry 1). Returns False if no popup ever shows up."""
    selectors = ['button.swal2-confirm', '.swal2-actions button',
                 'button:has-text("OK")', 'button:has-text("Ok")',
                 'button:has-text("Close")', '.modal-footer button',
                 '.modal button:has-text("OK")']
    for _ in range(tries):
        for sel in selectors:
            try:
                b = page.locator(sel).first
                if b.count() > 0 and b.is_visible():
                    b.click()
                    print("  ✅  Closed a popup (OK/Close)")
                    iran_settle(page)
                    return True
            except Exception:
                continue
        page.wait_for_timeout(300)
    return False


# ── IRAN document download (from Tameen) → upload (to IRAN) ────────────────────
#  The four documents IRAN needs. Tameen 'Document Details' label spellings to try
#  on the LEFT; the IRAN FileUpload box label on the RIGHT.
IRAN_DOC_LABELS = {
    "civil_id_front": ["Civil ID Front", "Civil Id Front"],
    "civil_id_back":  ["Civil ID Back", "Civil Id Back"],
    "license_front":  ["License ID Front", "Driving License Front", "License Front", "DL Front"],
    "license_back":   ["License ID Back", "Driving License Back", "License Back", "DL Back"],
}
IRAN_UPLOAD_LABELS = {
    "civil_id_front": "Civil Id Front",
    "civil_id_back":  "Civil Id Back",
    "license_front":  "Driving License Front",
    "license_back":   "Driving License Back",
}
IRAN_DOC_PRETTY = {
    "civil_id_front": "Civil ID Front",
    "civil_id_back":  "Civil ID Back",
    "license_front":  "Driving License Front",
    "license_back":   "Driving License Back",
}


def _iran_guess_ext(url: str, content_type: str) -> str:
    """Pick a file extension from the URL, else the content-type; default .jpg."""
    u = (url or "").lower().split("?")[0]
    for e in (".jpg", ".jpeg", ".png", ".pdf", ".gif", ".webp"):
        if u.endswith(e):
            return e
    ct = (content_type or "").lower()
    if "png" in ct:  return ".png"
    if "pdf" in ct:  return ".pdf"
    if "gif" in ct:  return ".gif"
    if "webp" in ct: return ".webp"
    return ".jpg"


def _tameen_doc_href(tameen_page, labels):
    """Read a plain href/src straight off the labelled row, if the element happens
    to be a real link (rare — most 'View' links mint a one-time URL via JS instead)."""
    for lab in labels:
        try:
            url = tameen_page.evaluate("""(lab) => {
                const norm = s => (s||'').replace(/\\s+/g,' ').trim().toLowerCase();
                const want = norm(lab);
                const els = [...document.querySelectorAll('*')];
                const hit = els.find(e => norm(e.innerText) === want);
                if (!hit) return null;
                const scopes = [hit, hit.parentElement, hit.closest('tr'), hit.closest('div')];
                for (const sc of scopes) {
                    if (!sc) continue;
                    const a = sc.querySelector('a[href]');
                    if (a && a.href) return a.href;
                    const img = sc.querySelector('img[src]');
                    if (img && img.src) return img.src;
                }
                return null;
            }""", lab)
            if url:
                print(f"  🔎  '{lab}': found URL via href/src → {url}")
                return url
        except Exception:
            continue
    return None


def _iran_find_doc_click_target(tameen_page, lab):
    """Find the element to click for a document row labelled `lab`.

    Confirmed via the Document Details dump: the section is a flat repeating
    sequence of leaf text — 'Document Type | <name> | Preview | View' — not a
    table with row-scoped cells, so proximity/ancestor searches (tr/div) don't
    reliably bracket a row. Instead: collect all leaf text under the 'Document
    Details' heading in DOM order, find the leaf equal to `lab`, then return
    the 'View' leaf a few positions after it (real click event bubbles to
    whatever link/handler wraps that leaf, so it doesn't matter that the leaf
    itself has no href). Falls back to 'Preview' if no 'View' follows.
    Returns a JSHandle (use .as_element(), may be None)."""
    return tameen_page.evaluate_handle("""(lab) => {
        const norm = s => (s||'').replace(/\\s+/g,' ').trim();
        const all = [...document.querySelectorAll('*')];
        const header = all.find(e => norm(e.innerText) === 'Document Details');
        if (!header) return null;
        const hy = header.getBoundingClientRect().top;
        const leaves = all
            .filter(e => e.children.length === 0)
            .map(e => ({el: e, t: norm(e.innerText), y: e.getBoundingClientRect().top}))
            .filter(x => x.t && x.y > hy);
        const idx = leaves.findIndex(x => x.t === lab);
        if (idx === -1) return null;
        for (let i = idx + 1; i < Math.min(idx + 5, leaves.length); i++) {
            if (leaves[i].t.toLowerCase() === 'view') return leaves[i].el;
        }
        for (let i = idx + 1; i < Math.min(idx + 5, leaves.length); i++) {
            if (leaves[i].t.toLowerCase() === 'preview') return leaves[i].el;
        }
        return null;
    }""", lab)


def _tameen_doc_details_dump(tameen_page):
    """Diagnostic only: when every label lookup fails, print the raw text under
    the 'Document Details' heading so the next run's console shows the real
    labels instead of guessing blind (same technique as read_tameen_addons)."""
    try:
        text = tameen_page.evaluate("""() => {
            const all = [...document.querySelectorAll('*')];
            const header = all.find(e => (e.innerText || '').trim() === 'Document Details');
            if (!header) return '(no "Document Details" heading found on this page)';
            const hy = header.getBoundingClientRect().top;
            const out = [];
            for (const e of all) {
                if (e.children.length !== 0) continue;
                const y = e.getBoundingClientRect().top;
                if (y <= hy || y > hy + 600) continue;
                const t = (e.innerText || '').trim();
                if (t) out.push(t);
            }
            return out.join(' | ');
        }""")
    except Exception:
        text = "(dump failed)"
    print(f"  🔎  Document Details raw content: {text}")


def _iran_capture_doc_bytes(tameen_page, labels):
    """Click the 'View' link and grab the document bytes via a route intercept,
    instead of reading them off the navigation response afterwards.

    The click-target fix works (tab opens at the right elocker URL), but
    listening for the "response" event and calling resp.body() consistently
    came back empty. Root cause: these documents are images/PDFs that Chrome's
    own viewer consumes internally once it takes over the navigation, so by the
    time our handler calls resp.body() the stream chrome already read is gone
    (a known Playwright limitation — response.body() often fails for resources
    handed to a native viewer/plugin). Fix: use context.route() to intercept
    the request ourselves — route.fetch() performs the ONE request that ever
    happens (so no "second GET invalidates a one-time token" risk either),
    we grab the bytes from that, then route.fulfill() hands the same response
    back to the browser so the tab still renders normally.
    Returns (bytes, url, content_type) or (None, None, None)."""
    ctx = tameen_page.context
    pattern = "**/elocker/document/**"
    captured = {}

    def handle_route(route):
        try:
            resp = route.fetch()
            captured["body"] = resp.body()
            captured["ctype"] = resp.headers.get("content-type", "")
            route.fulfill(response=resp)
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    ctx.route(pattern, handle_route)
    try:
        for lab in labels:
            try:
                handle = _iran_find_doc_click_target(tameen_page, lab)
                target = handle.as_element()
                if target is None:
                    continue
                captured.clear()
                with ctx.expect_page(timeout=8000) as np:
                    target.scroll_into_view_if_needed(timeout=6000)
                    target.click()
                newpg = np.value
                try:
                    newpg.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                newpg.wait_for_timeout(500)  # let the route handler settle
                url = newpg.url
                body = captured.get("body")
                ctype = captured.get("ctype", "")
                try:
                    newpg.close()
                except Exception:
                    pass
                if body:
                    print(f"  🔎  '{lab}': captured {len(body)} bytes via route intercept")
                    return body, url, ctype
                if url and url.lower() != "about:blank":
                    print(f"  ⚠️  '{lab}': tab opened ({url}) but no response body captured")
            except Exception:
                continue
    finally:
        try:
            ctx.unroute(pattern, handle_route)
        except Exception:
            pass
    return None, None, None


def tameen_download_iran_documents(tameen_page, record_tag: str) -> dict:
    """Download the four documents IRAN needs (Civil ID front/back, Driving License
    front/back) from the open Tameen record's Document Details section. Returns a
    dict of local file paths (or None per item). Never crashes the record — flags."""
    print("\n── Tameen: downloading the customer's documents for IRAN ──")
    out = {"civil_id_front": None, "civil_id_back": None,
           "license_front": None, "license_back": None}
    safe_tag = "".join(c if c.isalnum() else "_" for c in (record_tag or "record"))[:40] or "record"
    folder = os.path.join("iran_uploads", safe_tag)
    os.makedirs(folder, exist_ok=True)
    for key, labels in IRAN_DOC_LABELS.items():
        pretty = IRAN_DOC_PRETTY[key]
        body, url, ctype = None, None, ""

        # 1) plain href/src, if the row happens to have one (cheap, rarely present)
        href = _tameen_doc_href(tameen_page, labels)
        if href:
            try:
                resp = tameen_page.context.request.get(href)
                if resp.ok:
                    body, url, ctype = resp.body(), href, resp.headers.get("content-type", "")
            except Exception:
                pass

        # 2) fall back to capturing the view-tab's own response (handles one-time
        #    e-locker tokens that a second request can't re-fetch)
        if not body:
            body, url, ctype = _iran_capture_doc_bytes(tameen_page, labels)

        if not body:
            print(f"  ⚠️  Could not find {pretty} in Document Details — upload it by hand on IRAN.")
            continue

        ext = _iran_guess_ext(url or "", ctype)
        path = os.path.abspath(os.path.join(folder, f"{key}{ext}"))
        with open(path, "wb") as f:
            f.write(body)
        out[key] = path
        print(f"  ✅  downloaded {pretty} -> {path}")
    if not any(out.values()):
        _tameen_doc_details_dump(tameen_page)
    return out


def iran_upload_documents(iran_page, doc_paths) -> None:
    """On the AdditionalDetails tab, attach each downloaded file to its Browse box
    via set_input_files(). Names the saved path in any warning so the operator can
    pick it by hand (the browser is visible, so a manual Browse always works)."""
    print("\n── IRAN: uploading documents (AdditionalDetails) ──")
    for key, label in IRAN_UPLOAD_LABELS.items():
        path = (doc_paths or {}).get(key)
        if not path:
            print(f"  ⚠️  No file for '{label}' — click its Browse box and pick the file by hand.")
            continue
        getters = [
            lambda: iran_page.locator(
                f'xpath=//*[contains(normalize-space(.),"{label}")]/following::input[@type="file"][1]'
            ).first,
            lambda: iran_page.locator(
                f'xpath=//*[contains(normalize-space(.),"{label}")]/ancestor::*[self::td or self::div][1]//input[@type="file"][1]'
            ).first,
            lambda: iran_page.get_by_label(label, exact=False).first,
        ]
        done = False
        for g in getters:
            try:
                inp = g()
                if inp.count() == 0:
                    continue
                inp.set_input_files(path)
                print(f"  ✅  Attached '{label}' ← {path}")
                iran_settle(iran_page)
                done = True
                break
            except Exception:
                continue
        if not done:
            print(f"  ⚠️  Could not attach '{label}' automatically. The file is saved at:\n"
                  f"        {path}\n      → click that Browse box and pick it by hand.")


# ── IRAN flow helpers (one per stage of the form) ─────────────────────────────

def iran_login_if_needed(page) -> None:
    """Log in to IRAN if the Sign In modal is showing; otherwise carry on. The
    image CAPTCHA is ALWAYS solved by the operator during a manual pause (same idea
    as the Tameen OTP). We pre-fill email/password if .env has them."""
    print("\n── IRAN: checking if login is needed ──")
    page.wait_for_timeout(1500)

    def _find_pwd(timeout):
        for sel in ['input[type="password"]', 'input[id*="pass" i]', 'input[name*="pass" i]']:
            try:
                cand = page.locator(sel).first
                if cand.count() > 0 and cand.is_visible(timeout=timeout):
                    return cand
            except Exception:
                continue
        return None

    pwd = _find_pwd(6000)
    if pwd is None:
        # Maybe the modal is behind a "Sign In" button — open it, then look again.
        for sel in ['button:has-text("Sign In")', 'a:has-text("Sign In")',
                    ':text("Sign In")', 'button:has-text("Login")']:
            try:
                b = page.locator(sel).first
                if b.count() > 0 and b.is_visible():
                    b.click(); page.wait_for_timeout(800); break
            except Exception:
                continue
        pwd = _find_pwd(4000)
    if pwd is None:
        print("  ✅  Already logged in (no Sign In modal detected)")
        return

    if IRAN_USERNAME:
        for sel in ['input[type="email"]', 'input[id*="email" i]', 'input[name*="email" i]',
                    'input[placeholder*="Email" i]', 'input[type="text"]']:
            try:
                box = page.locator(sel).first
                if box.count() > 0 and box.is_visible():
                    box.click(); box.fill(""); box.type(IRAN_USERNAME, delay=20); break
            except Exception:
                continue
    else:
        print("  ⚠️  IRAN_USERNAME missing from .env — type the email by hand during the pause.")
    if IRAN_PASSWORD:
        try:
            pwd.click(); pwd.fill(""); pwd.type(IRAN_PASSWORD, delay=20)
        except Exception:
            print("  ⚠️  Could not type the IRAN password — type it by hand during the pause.")
    else:
        print("  ⚠️  IRAN_PASSWORD missing from .env — type the password by hand during the pause.")

    print("\n" + "=" * 60)
    print("⏸  ACTION REQUIRED — IRAN login (manual CAPTCHA)")
    print("=" * 60)
    input("⏸  In the IRAN tab: type the CAPTCHA shown, then click Sign In. "
          "Come back and press ENTER ▶  ")
    iran_settle(page)

    if _find_pwd(4000) is not None:
        print("  ⚠️  Still seeing the Sign In box — login may not be complete. Check and retry.")
    else:
        print("  ✅  Logged in to IRAN")


def iran_go_to_motor_form(page, policy_type: str) -> None:
    """Open Transaction → Comprehensive/Third Party, click 'No, Manual Entry' on the
    Automatic Data capturing popup, clear the follow-up popup, verify Basic Info."""
    target = "Comprehensive" if policy_type == "Comprehensive" else "Third Party"
    print(f"\n── IRAN: opening the {target} motor form ──")

    # 1) Open the Transaction fly-out menu.
    opened = False
    for sel in ['a:has-text("Transaction")', 'span:has-text("Transaction")', ':text("Transaction")']:
        try:
            m = page.locator(sel).first
            if m.count() > 0 and m.is_visible():
                m.hover()
                try:
                    m.click()
                except Exception:
                    pass
                opened = True
                page.wait_for_timeout(600)
                break
        except Exception:
            continue
    if not opened:
        print("  ⚠️  Could not open the Transaction menu — please open it by hand.")

    # 2) Click Comprehensive / Third Party.
    if not iran_click_button(page, target):
        print(f"  ⚠️  Could not click '{target}' — please click it by hand under Transaction → Motor.")

    # 3) 'Automatic Data capturing' popup → No, Manual Entry.
    if not iran_click_button(page, "No, Manual Entry"):
        iran_click_button(page, "Manual Entry")     # looser fallback

    # 4) Clear the error/info popup that follows.
    iran_dismiss_popup_ok(page)
    iran_settle(page)

    # 5) Verify the Basic Information form is showing.
    try:
        ok = page.locator('xpath=//*[contains(normalize-space(.),"Basic Information")]').count() > 0
    except Exception:
        ok = False
    if ok:
        print("  ✅  Motor form open (Basic Information showing)")
    else:
        print("  ⚠️  Could NOT confirm the Basic Information form — check the IRAN tab.")


def iran_fill_basic_info(page, license_id, full_name, chassis, policy_type, uae) -> None:
    """Tab 1 — Basic Information: Assured Type, Civil ID, wait for DOB auto-load,
    name, mobile, chassis (auto-fills the vehicle block), comprehensive value-range
    manual pause, and the UAE toggle. Stops at Next."""
    print("\n── IRAN Tab 1: Basic Information ──")
    iran_select(page, "Assured Type", IRAN_ASSURED_TYPE)

    # The Civil ID field may appear / rename after Assured Type = Civil ID.
    filled = False
    for lbl in ("Civil ID No / License No", "Civil ID No", "License No", "Organization ID"):
        if iran_fill_by_label(page, lbl, license_id):
            filled = True
            break
    if not filled:
        print("  ⚠️  Could not fill the Civil ID / License field — please enter it by hand.")

    # Blur, then wait for the date-of-birth to auto-load.
    try:
        page.keyboard.press("Tab")
    except Exception:
        pass
    iran_wait_for_value(page, "Insured Date of Birth", tries=40)

    iran_fill_by_label(page, "Insured Name", full_name)            # overwrite any prefill
    iran_fill_by_label(page, "Mobile Number", IRAN_FIXED_MOBILE)

    # Chassis → auto-fills plate / make / model / body / year / seats, etc.
    if chassis:
        iran_fill_by_label(page, "Chassis Number", chassis)
        try:
            page.keyboard.press("Tab")
        except Exception:
            pass
        if not iran_wait_for_value(page, "Vehicle Make", tries=40):
            iran_wait_for_value(page, "Vehicle Plate Number", tries=10)
    else:
        print("  ⚠️  No chassis number read from Tameen — the vehicle block won't "
              "auto-fill. Please enter the chassis by hand.")

    # Comprehensive may raise a value-RANGE error — left manual for now.
    if policy_type == "Comprehensive":
        print("\n" + "=" * 60)
        print("⏸  COMPREHENSIVE: if a value-RANGE error appeared on screen, enter the")
        print("   value now (use the HIGHEST number of the range) in the IRAN tab.")
        print("   (Third Party has no such error — nothing to do there.)")
        print("=" * 60)
        input("   Press ENTER once any value-range error is handled ▶  ")

    # UAE cover toggle — only when the Tameen add-ons mention UAE.
    if uae:
        iran_set_toggle_yes(page, "UAECoverYN")
    else:
        print("  ℹ️  No UAE add-on — leaving UAECoverYN as-is (No).")

    iran_click_button(page, "Next")


def iran_fill_plan_details(page, addons) -> None:
    """Tab 2 — PlanDetails: tick the IRAN plan checkboxes matching the Tameen
    add-ons (via IRAN_ADDON_MAP), then Select Plan → Next."""
    print("\n── IRAN Tab 2: Plan Details (Choose Your Plan) ──")
    addons_l = (addons or "").lower()

    always_ticked = [label for label in IRAN_PLAN_ALWAYS_TICK if iran_tick_plan_addon(page, label)]
    if always_ticked:
        print(f"  ✅  Mandatory PAB checkboxes ticked: {', '.join(always_ticked)}")

    if not addons:
        print("  ℹ️  No Tameen add-ons read — no optional plan checkboxes to tick.")
    else:
        print(f"  → Tameen add-ons read: {addons}")
        ticked = []
        for keyword, checkbox_label in IRAN_ADDON_MAP.items():
            if checkbox_label in IRAN_PLAN_ALWAYS_TICK:
                continue  # already handled unconditionally above
            if keyword in addons_l and iran_tick_plan_addon(page, checkbox_label):
                ticked.append(checkbox_label)
        if ticked:
            print(f"  ✅  Optional plan checkboxes ticked: {', '.join(sorted(set(ticked)))}")
        else:
            print("  ⚠️  No optional plan checkbox matched the Tameen add-ons.")
    iran_click_button(page, "Select Plan")
    iran_click_button(page, "Next")


def iran_fill_additional_details(page, policy_start_date, nationality, doc_paths) -> None:
    """Tab 3 — AdditionalDetails: Policy Start Date, Nationality, fixed Address,
    Transaction Type, the four document uploads, then Next → Summary."""
    print("\n── IRAN Tab 3: Additional Details ──")
    # ⚠️ The field shows date+time on screen — confirm the exact format IRAN accepts.
    iran_fill_by_label(page, "Policy Start Date", policy_start_date, press_escape=True)
    if nationality:
        iran_select(page, "Nationality", nationality)
    else:
        print("  ⚠️  No nationality read from Tameen — please pick Nationality by hand.")
    iran_fill_by_label(page, "Insured Address", IRAN_FIXED_ADDRESS)
    iran_select(page, "Transaction Type", IRAN_TRANSACTION_TYPE)
    iran_upload_documents(page, doc_paths)
    iran_click_button(page, "Next")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW
# ══════════════════════════════════════════════════════════════════════════════
# Everything below only runs when you launch this file directly
# (python test.py). It does NOT run when test_ni.py imports the helpers above.
if __name__ == "__main__":
    with sync_playwright() as p:

        context = p.chromium.launch_persistent_context(
            user_data_dir="automation_profile",
            headless=False,
            slow_mo=450,                           # small delay before each action (was 300)
            locale="en-US",
            args=["--lang=en-US"],
            permissions=["clipboard-read", "clipboard-write"],
            ignore_https_errors=True,          # MIC is on :444 with a custom cert
        )

        print("Opening Tameen website...")
        tameen_page = context.pages[0] if context.pages else context.new_page()
        # 2 minutes (120000 ms) per action — a safety net so nothing can ever hang for
        # 10 minutes. The long manual login/OTP wait is handled by the ENTER prompt
        # below (plain Python input, no Playwright timeout), so this does not rush you.
        tameen_page.set_default_timeout(120000)
        tameen_page.goto("https://mis.tameen.om/dashboard/login", timeout=60000)

        print("Opening MIC website...")
        mic_page = context.new_page()
        # 2 minutes (120000 ms) per action: long enough for slow APEX pages to react,
        # but bounded so a click on a covered element eventually fails with a clear
        # error instead of hanging forever.
        mic_page.set_default_timeout(120000)

        print("Opening New India website...")
        ni_page = context.new_page()
        # Same 2-minute safety net. New India is a slow ASP.NET site, so the per-action
        # waits (ni_settle) plus this timeout keep it from racing ahead or hanging.
        ni_page.set_default_timeout(120000)

        print("Opening IRAN Insurance website...")
        iran_page = context.new_page()
        iran_page.set_default_timeout(120000)

        # ── Native-dialog safety net ──────────────────────────────────────────────
        # Auto-accept (click OK on) any genuine NATIVE browser dialog — e.g. a
        # beforeunload "Leave site?" prompt that can fire when we navigate or reset a
        # tab that still holds unsaved changes. Playwright auto-DISMISSES (Cancel) such
        # dialogs unless a handler is registered, which would silently block a reset.
        # NOTE: the "There are unsaved changes…" popup after Calculate is NOT this — it
        # is an in-page APEX HTML dialog handled by mic_accept_confirm_dialog().
        # See errorlog.md.txt (entry 1) for why the two dialog types are handled apart.
        mic_page.on("dialog", lambda dialog: dialog.accept())
        tameen_page.on("dialog", lambda dialog: dialog.accept())
        ni_page.on("dialog", lambda dialog: dialog.accept())
        iran_page.on("dialog", lambda dialog: dialog.accept())

        # Restore a normal 'Save As' dialog for the employee's Print → Download step.
        enable_download_dialogs(context)

        mic_page.goto(MIC_HOME_URL, timeout=60000)
        ni_page.goto(NI_LOGIN_URL, timeout=60000)
        iran_page.goto(IRAN_LOGIN_URL, timeout=60000)

        # All sites loaded — bring Tameen back to the front (it's the tab the operator
        # logs into first; otherwise the last-loaded IRAN tab would be showing).
        tameen_page.bring_to_front()

        try:
            # ── TAMEEN: manual OTP login (ONE TIME for the whole run) ─────────────
            print("\n" + "=" * 60)
            print("⏸  ACTION REQUIRED")
            print("  1. Switch to the Tameen tab")
            print("  2. Log in and complete the OTP")
            print("  3. Come back here and press ENTER")
            print("=" * 60)
            input("\nPress ENTER once you are logged in to Tameen ▶  ")

            # ── TAMEEN: land on the Payments page (ONE TIME) ──────────────────────
            tameen_page.bring_to_front()
            print("\nAutomating Tameen navigation...")
            tameen_go_to_payments(tameen_page)               # step 1: PAYMENTS tile

            # ══════════════════════════════════════════════════════════════════════
            #  PER-RECORD LOOP
            #  Process one policy, then (on demand) reset BOTH tabs and offer the next.
            #  Type 'q' at either prompt to stop and close the browser.
            # ══════════════════════════════════════════════════════════════════════
            while True:
                record_text = None     # the chosen Tameen row's text (for the summaries)
                prepared    = None     # the values prepared for the insurer (filled in below)
                company     = None     # "MIC" / "NEW_INDIA" / "IRAN" — which flow to run

                try:
                    # Bring Tameen to the front EACH record: read_field reads via the
                    # clipboard, which only works while this tab is focused. (On record
                    # 2+ the MIC tab was last in front, so without this the reads would
                    # silently fall back to the less-reliable DOM path.)
                    tameen_page.bring_to_front()

                    # ── TAMEEN: pick channel + row ────────────────────────────────
                    # (typing 0 at the row prompt re-opens 'Payments by Channel' so
                    #  you can pick a different channel.)
                    while True:
                        tameen_click_payments_by_channel(tameen_page)  # step 2
                        channel_name = tameen_select_channel(tameen_page)  # step 3
                        status, record_text, company = tameen_select_and_click_eye(tameen_page)  # step 4
                        if status != "BACK":
                            break

                    if company not in ("MIC", "NEW_INDIA", "IRAN"):
                        raise RuntimeError(
                            "This record's insurance company is not one we can process "
                            "automatically (only Muscat Insurance, New India and IRAN are supported)."
                        )
                    _COMPANY_NAMES = {"MIC": "Muscat Insurance", "NEW_INDIA": "New India", "IRAN": "IRAN Insurance"}
                    print(f"\n  → This record is a {_COMPANY_NAMES.get(company, company)} policy.")

                    # ── TAMEEN: read the fields BOTH insurers need ────────────────
                    print("\nReading data from Tameen record...")
                    first_name   = read_field(tameen_page, "First Name")
                    last_name    = read_field(tameen_page, "Last Name")
                    license_id   = read_field(tameen_page, "License ID")
                    product_name = read_field(tameen_page, "Product Name")
                    prev_expiry  = read_field(tameen_page, "Previous Expiry")
                    vehicle_no   = read_field(tameen_page, "Vehicle Number")   # e.g. "B S-4788"

                    # Seats — read live from the Tameen View Details page. The label may be
                    # written a few different ways, so try the most likely ones in order.
                    seats_raw = ""
                    for seats_label in ("Seats", "No. of Seats", "No Of Seats",
                                        "Number of Seats", "Seating Capacity", "Seat Capacity"):
                        seats_raw = read_field(tameen_page, seats_label)
                        if seats_raw:
                            break
                    # Keep only the digits (so 'Seats: 7' or '7 seats' both become '7').
                    seats = "".join(ch for ch in seats_raw if ch.isdigit())

                    full_name = (first_name + " " + last_name).strip()

                    # Policy type source. Normally we read it from the Product Name, but
                    # the Mobileapp channel leaves Product Name blank and instead has a
                    # dedicated "Policy Type" field (Third Party / Comprehensive). For that
                    # channel only, read that field and use it to decide the policy type.
                    type_source = product_name
                    if (channel_name or "").lower() == "mobileapp":
                        pt = ""
                        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
                            pt = read_field(tameen_page, lbl)
                            if pt:
                                break
                        if not pt:   # fall back to the type tag shown in the record row
                            rt = (record_text or "").lower().replace(" ", "")
                            if "thirdparty" in rt:
                                pt = "Third Party"
                            elif "comprehensive" in rt:
                                pt = "Comprehensive"
                        if pt:
                            type_source = pt
                            print(f"  → Mobileapp: using Policy Type '{pt}' (Product Name is blank)")

                    # Flag a previous-policy expiry that's more than a month away
                    # (renewing too early) — shown in the prepared summary below.
                    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
                    if expiry_flagged:
                        print(f"\n⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

                    # ══════════════════════════════════════════════════════════════
                    #  ROUTE TO THE RIGHT INSURER
                    # ══════════════════════════════════════════════════════════════
                    if company == "MIC":
                        # ── extra fields only MIC needs ──
                        sum_insured  = read_field(tameen_page, "Sum Insured")
                        tameen_total = read_field(tameen_page, "Total Premium")
                        period_from  = compute_period_from(parse_tameen_date(prev_expiry))
                        plate_code, plate_number = split_plate(vehicle_no)

                        prepared = {
                            "Product Name"  : product_name,
                            "Insured Name"  : full_name,
                            "License No"    : license_id,
                            "Period From"   : f"{period_from}   (from expiry '{prev_expiry}')",
                            **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
                            "Plate"         : f"code='{plate_code}'  number='{plate_number}'  (from '{vehicle_no}')",
                            "Seats"         : seats or "(not read — check the Tameen label)",
                            "Sum Insured"   : sum_insured,
                            "Tameen Premium": tameen_total,
                        }
                        print("\n" + "=" * 60)
                        print("📋  VALUES PREPARED FOR MIC")
                        print("=" * 60)
                        for label, value in prepared.items():
                            print(f"  {label:<14}: {value}")
                        print("=" * 60)

                        # ── MIC: fill in the policy (left as Draft — not approved) ──
                        mic_page.bring_to_front()
                        mic_login_if_needed(mic_page)                                   # step 0
                        mic_open_policy_create(mic_page)                                # steps 1–2
                        is_comprehensive = mic_choose_policy_type_and_create(mic_page, type_source)  # steps 3–4
                        mic_get_licence(mic_page, license_id)                          # steps 5–6
                        mic_fill_policy_info(mic_page, full_name, period_from)         # steps 7–12
                        mic_get_vehicle(mic_page, plate_number, plate_code)           # steps 13–15
                        mic_fill_vehicle_info(mic_page, is_comprehensive, sum_insured, seats)  # steps 16–19
                        mic_calculate_and_check(mic_page, tameen_total)               # steps 20–22
                        # Status is intentionally left as Draft — no auto-approve.

                        print("\n" + "=" * 60)
                        print("✅  MIC FLOW FINISHED — review the form on screen.")
                        if record_text:
                            print(f"   Record: {record_text}")
                        print("=" * 60)

                    elif company == "NEW_INDIA":
                        # ── extra fields only New India needs ──
                        # ⚠️ CONFIRM these Tameen labels — the last three are guesses; we
                        #    try several spellings and keep the first that returns a value.
                        mileage = ""
                        for lbl in ("Mileage Est", "Mileage", "Mileage Estimate", "Odometer"):
                            mileage = read_field(tameen_page, lbl)
                            if mileage:
                                break
                        color = ""
                        for lbl in ("Color", "Colour", "Vehicle Color", "Vehicle Colour"):
                            color = read_field(tameen_page, lbl)
                            if color:
                                break
                        body_type = ""
                        for lbl in ("Body Type", "Body", "Vehicle Body Type", "Body Style"):
                            body_type = read_field(tameen_page, lbl)
                            if body_type:
                                break
                        # Fallback for when New India's own Mulkiya lookup shows 'NOT FOUND'
                        # for Brand/Model. Tameen's own field is misspelled 'Modal' on the
                        # live site (that's the real label) — try it first so the normal
                        # case doesn't print a bogus "'Model' not on this record" warning.
                        tameen_make = read_field(tameen_page, "Make")
                        tameen_model = ""
                        for lbl in ("Modal", "Model"):
                            tameen_model = read_field(tameen_page, lbl)
                            if tameen_model:
                                break
                        addons = read_tameen_addons(tameen_page)

                        # Policy type drives the Coverage Type dropdown later.
                        pn = (type_source or "").lower().replace(" ", "")
                        if "thirdparty" in pn:
                            policy_type = "Third Party"
                        elif "comprehensive" in pn:
                            policy_type = "Comprehensive"
                        else:
                            policy_type = None
                            print(f"  ⚠️  Could not tell policy type from '{type_source}' — "
                                  "Coverage Type will be left for you to pick.")

                        reg_no          = reformat_plate_for_ni(vehicle_no)
                        commencing_date = compute_commencing_date_ni(prev_expiry)

                        prepared = {
                            "Product/Type"  : f"{product_name}  →  {policy_type or '(unknown)'}",
                            "Insured Name"  : full_name,
                            "License/CivilID": license_id,
                            "Reg.No"        : f"{reg_no}   (from '{vehicle_no}')",
                            "Commencing"    : f"{commencing_date}   (from expiry '{prev_expiry}')",
                            **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
                            "Seats"         : seats or "(not read — check the Tameen label)",
                            "Mileage"       : mileage or "(not read)",
                            "Colour"        : color or "(not read)",
                            "Body Type"     : body_type or "(not read)",
                            "Add-ons"       : addons or "(none read)",
                        }
                        print("\n" + "=" * 60)
                        print("📋  VALUES PREPARED FOR NEW INDIA")
                        print("=" * 60)
                        for label, value in prepared.items():
                            print(f"  {label:<16}: {value}")
                        print("=" * 60)

                        # ── NEW INDIA: fill the form (stops at review — no submit) ──
                        ni_page.bring_to_front()
                        ni_login_if_needed(ni_page)
                        ni_go_to_motor_policy(ni_page)
                        ni_fill_primary_top(ni_page, reg_no, license_id)
                        ni_fill_primary_client(ni_page, commencing_date, full_name)
                        brand, model, year = ni_fill_previous_policy(ni_page, mileage, color, full_name)
                        ni_fill_vehicle_details(ni_page, brand, model, body_type, seats,
                                                 tameen_make, tameen_model)
                        ni_fill_premium_calculation(ni_page, policy_type, seats, addons)
                        # Intentionally STOPS here — nothing is saved/submitted.

                        print("\n" + "=" * 60)
                        print("✅  NEW INDIA FORM FILLED — review on screen.")
                        if record_text:
                            print(f"   Record: {record_text}")
                        print("=" * 60)

                    else:  # company == "IRAN"
                        # ── extra fields only IRAN needs (read while Tameen is in front) ──
                        chassis = ""
                        for lbl in ("Chassis Number", "Chassis No", "Chassis"):
                            chassis = read_field(tameen_page, lbl)
                            if chassis:
                                break
                        nationality = ""
                        for lbl in ("Nationality", "Nationality of Insured", "Insured Nationality"):
                            nationality = read_field(tameen_page, lbl)
                            if nationality:
                                break
                        addons = read_tameen_addons(tameen_page)

                        # Comprehensive / Third Party (default Third Party if unclear).
                        pn = (type_source or "").lower().replace(" ", "")
                        if "thirdparty" in pn:
                            policy_type = "Third Party"
                        elif "comprehensive" in pn:
                            policy_type = "Comprehensive"
                        else:
                            policy_type = "Third Party"
                            print(f"  ⚠️  Could not tell policy type from '{type_source}' — "
                                  "defaulting to Third Party. Change it by hand if wrong.")

                        uae = "uae" in (addons or "").lower()
                        # Same start-date rule as the others; IRAN wants dd/mm/yyyy too.
                        policy_start = compute_commencing_date_ni(prev_expiry)

                        # Download the customer's documents from Tameen (Tameen is in front).
                        doc_paths = tameen_download_iran_documents(tameen_page, record_text or "record")
                        got = [IRAN_DOC_PRETTY[k] for k, v in doc_paths.items() if v]

                        prepared = {
                            "Product/Type"   : f"{product_name}  →  {policy_type}",
                            "Insured Name"   : full_name,
                            "License/CivilID": license_id,
                            "Chassis"        : chassis or "(not read — check the Tameen label)",
                            "Nationality"    : nationality or "(not read)",
                            "Policy Start"   : f"{policy_start}   (from expiry '{prev_expiry}')",
                            **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
                            "UAE Cover"      : "Yes" if uae else "No",
                            "Add-ons"        : addons or "(none read)",
                            "Documents"      : (", ".join(got) if got else "(none downloaded — upload by hand)"),
                        }
                        print("\n" + "=" * 60)
                        print("📋  VALUES PREPARED FOR IRAN")
                        print("=" * 60)
                        for label, value in prepared.items():
                            print(f"  {label:<16}: {value}")
                        print("=" * 60)

                        # ── IRAN: fill the form (stops at Summary — no submit) ──
                        iran_page.bring_to_front()
                        iran_login_if_needed(iran_page)
                        iran_go_to_motor_form(iran_page, policy_type)
                        iran_fill_basic_info(iran_page, license_id, full_name, chassis, policy_type, uae)
                        iran_fill_plan_details(iran_page, addons)
                        iran_fill_additional_details(iran_page, policy_start, nationality, doc_paths)
                        # Lands on Summary — STOP. Nothing is submitted / issued / downloaded.

                        print("\n" + "=" * 60)
                        print("✅  IRAN FORM FILLED — review on screen.")
                        if record_text:
                            print(f"   Record: {record_text}")
                        print("=" * 60)

                    # RESET ON DEMAND: nothing is touched until the employee says so.
                    ans = input("\nReview the result. Press ENTER to reset the tabs and "
                                "process another record, or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                except Exception as e:
                    # ── FLAGGED QUOTE ─────────────────────────────────────────────
                    # Show the problem + the record details (same style as the row
                    # list) so the employee can investigate / contact whoever they
                    # need before moving on. Nothing resets until they continue.
                    print("\n" + "=" * 60)
                    print("❌  THIS QUOTE HIT A PROBLEM — FLAGGED FOR REVIEW")
                    print("=" * 60)
                    print(f"  Reason: {e}")
                    print("-" * 60)
                    if record_text:
                        print(f"  Tameen record : {record_text}")
                    if prepared:
                        print("  Details prepared so far:")
                        for label, value in prepared.items():
                            print(f"    {label:<14}: {value}")
                    else:
                        print("  (Failed before the record's details could be read.)")
                    print("-" * 60)
                    print("  → Investigate in the browser tabs. Contact whoever you need")
                    print("    for any missing/extra information to sort this quote out.")
                    print("=" * 60)

                    ans = input("\nWhen you are done, press ENTER to reset the tabs and "
                                "continue to the next record, or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                # Reached only when CONTINUING (after a success OR a flagged error):
                # send the tabs back to their starting points for the next record.
                # Only the insurer tab we actually used is reset (the other one is left
                # alone). If we failed before knowing the insurer, reset MIC by default.
                if company == "NEW_INDIA":
                    ni_reset_to_motor_policy(ni_page)
                elif company == "IRAN":
                    # Next IRAN record re-navigates Transaction → type itself, so just
                    # send this tab back to a clean Dashboard.
                    print("\n── IRAN reset: returning to the Dashboard ──")
                    try:
                        iran_page.goto(IRAN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
                        print("  ✅  IRAN back on the Dashboard")
                    except Exception as e:
                        print(f"  ⚠️  Could not reset IRAN ({e}) — open the Dashboard by hand.")
                else:
                    mic_reset_to_home(mic_page)
                tameen_reset_to_payments(tameen_page)

        except Exception as e:
            # Catches problems in the one-time login / first navigation above.
            print("\n" + "=" * 60)
            print(f"❌  ERROR:\n{e}")
            print("=" * 60)

        finally:
            # NO automatic closing — browser stays open until YOU press ENTER here.
            input("\nPress ENTER in this terminal to close the browser when you're done ▶  ")
            context.close()
            print("Browser closed.")
