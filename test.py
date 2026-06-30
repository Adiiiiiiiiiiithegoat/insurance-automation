from playwright.sync_api import sync_playwright
from datetime import timedelta, date
import json
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

NI_USERNAME = os.getenv("NI_USERNAME", "")
NI_PASSWORD = os.getenv("NI_PASSWORD", "")
if not NI_USERNAME or not NI_PASSWORD:
    print("⚠️  New India credentials not found in .env — login will be skipped/may fail.")

# MIC configuration + all shared MIC/Tameen helpers now live in common.py.

# ══════════════════════════════════════════════════════════════════════════════
#  NEW INDIA ASSURANCE  —  CONFIGURATION  (second insurer, runs alongside MIC)
# ══════════════════════════════════════════════════════════════════════════════
NI_LOGIN_URL = "https://www.newindiaoman.com/Account/login.aspx"
# The Motor Policy form itself. We open this directly (it's reliable once logged
# in) instead of hovering the flaky Transactions menu.
NI_MOTOR_POLICY_URL = "https://www.newindiaoman.com/AgBr/mtrPolicy.aspx"

# New India is an OLD, SLOW ASP.NET site that reloads the whole page after many
# actions. This is the pause (in milliseconds) we wait after EVERY action for
# that reload to finish. If fields get skipped, make this bigger.
NI_STEP_PAUSE = 800

# ── Fixed values that are the SAME on every New India policy ──
NI_CUSTOMER          = "CASH CUSTOMER - ONEIC TAMEEN SPC"   # exact dropdown text
NI_TELEPHONE         = "99435202"
NI_TRANSMISSION      = "Auto"
NI_MUSIC_SYSTEM      = "Yes"
NI_EXTERNAL_DAMAGES  = "No"
NI_TYRE_CONDITION    = "good"
# ── Radio-button choices (these are buttons you click, not dropdowns) ──
NI_PAYMENT_MODE      = "Cash"      # Cash / Card
NI_FIRST_REG         = "Yes"       # Oman's First Registration
NI_VEHICLE_TYPE      = "Standard"  # Standard / Electric
NI_PAYBYLINK         = "No"        # Payment thru Paybylink
NI_IMPORT_VEHICLE    = "No"        # Import Vehicle


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
#  NEW INDIA ASSURANCE (INSURER #2)  —  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
#  New India's website is an OLD, SLOW ASP.NET site. After you type into some
#  boxes — and after EVERY button or tab click — the WHOLE page reloads. That
#  reload throws away any element we grabbed a moment ago, so the two golden
#  rules here are:
#     1. After EVERY action, call ni_settle() to wait for the reload to finish.
#     2. NEVER reuse an element across an action — always find it again fresh.
#  The label-based helpers below already re-find their field on every call.
# ──────────────────────────────────────────────────────────────────────────────

def ni_settle(page) -> None:
    """Wait for a New India postback (whole-page reload) to finish, then pause."""
    try:
        page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    page.wait_for_timeout(NI_STEP_PAUSE)


def _ni_scope(page):
    """Return the FRAME that actually holds the Motor Policy form, wherever it is.

    New India's form may be drawn inside an <iframe>, OR the 'Motor Policy' menu may
    open it in a SEPARATE browser tab. Either way the top-level page locators find
    nothing (which is why every field 'could not be found'). So we scan EVERY tab in
    the browser and EVERY frame in each tab, and return the first frame that contains
    a stable bit of the form's text. Re-resolved on every call (postbacks reload the
    frame). Falls back to the original page if the form can't be found yet.
    """
    markers = ["Show Information", "Primary Information", "Previous Policy", "Reg.No"]
    try:
        pages = page.context.pages
    except Exception:
        pages = [page]
    for _ in range(20):                      # the form/tab may still be loading
        for pg in pages:
            try:
                frames = pg.frames
            except Exception:
                continue
            for fr in frames:
                try:
                    for m in markers:
                        if fr.locator(f'xpath=//*[contains(normalize-space(.),"{m}")]').count() > 0:
                            return fr
                except Exception:
                    continue
        # nothing yet — wait a moment, refresh the tab list (a new tab may open)
        try:
            page.wait_for_timeout(300)
            pages = page.context.pages
        except Exception:
            break
    return page


def ni_fill_by_label(page, label: str, value: str, press_escape: bool = False) -> bool:
    """Type a value into a New India text box found by the words next to it.

    New India uses a TABLE layout: the label sits in one cell and the input box
    in the next cell (or just after the label). We try several ways to find it,
    clear the box, type slowly so the site keeps up, then wait for the reload.
    Set press_escape=True for date fields so the date-picker is dismissed.
    """
    value = str(value)
    sc = _ni_scope(page)
    getters = [
        # 1) EXACT label in its own cell → control in the very next cell (most precise;
        #    avoids matching a parent cell that wraps several rows, and avoids jumping
        #    into a neighbouring field — the bugs that mixed up Model / External Damages)
        lambda: sc.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]//input[not(@type="hidden")][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]//textarea[1]'
        ).first,
        # 2) a LEAF cell containing the label (handles "Label  العربية" in one cell)
        lambda: sc.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following-sibling::td[1]//input[not(@type="hidden")][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following-sibling::td[1]//textarea[1]'
        ).first,
        # 3) a proper <label> association
        lambda: sc.get_by_label(label, exact=False).first,
        # 4) last-resort broad search (can jump to a neighbouring field — kept LAST)
        lambda: sc.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]/following::textarea[1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(text()),"{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() == 0:        # skip a getter that matched nothing (fast, no wait)
                continue
            el.scroll_into_view_if_needed(timeout=10000)
            el.click()
            el.press("Control+a")
            el.press("Backspace")
            el.type(value, delay=20)
            if press_escape:
                el.press("Escape")     # close any date-picker that popped up
            print(f"  ✅  Filled '{label}' = {value}")
            ni_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not fill '{label}' — please set it by hand.")
    return False


def ni_read_by_label(page, label: str, quiet: bool = False) -> str:
    """Read the value New India already shows next to a label.

    Done in the browser (JavaScript) so it works no matter how the form is laid
    out: it finds the element whose OWN text is the label (ignoring any Arabic
    translation in the same cell, and never matching a parent cell that wraps
    several rows), then reads the value control in the SAME table row — falling
    back to the nearest control after the label. quiet=True hushes the polling.
    """
    sc = _ni_scope(page)
    val = ""
    try:
        val = sc.evaluate("""(label) => {
            const norm  = s => (s || '').replace(/\\s+/g, ' ').trim();
            // keep only ASCII so an Arabic translation sharing the cell is ignored
            const clean = s => norm(s).replace(/[^\\x00-\\x7F]/g, '').replace(/[:*]/g, '').trim();
            const want = label.toUpperCase();

            const isVal = i =>
                (i.tagName === 'TEXTAREA') ||
                (i.tagName === 'INPUT' && !['hidden','checkbox','radio','button','submit']
                    .includes((i.getAttribute('type') || 'text').toLowerCase()));

            // find EVERY element whose OWN direct text is exactly the label (there
            // may be a few; we try each and keep the best visual match below)
            const tags = ['td','th','label','span','div','b','strong','font','p'];
            const labs = [];
            for (const el of document.querySelectorAll(tags.join(','))) {
                let own = '';
                for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
                if (clean(own).toUpperCase() === want) labs.push(el);
            }
            if (!labs.length) return '';

            const ctrls = [...document.querySelectorAll('input,textarea')].filter(isVal);

            // VISUAL match: the field's box sits on the SAME line as its label and
            // just to its right. This matches what you see on screen and does NOT
            // depend on the HTML order (which was making Brand/Model/Year all read
            // the same value). Pick the nearest such box.
            let best = null, bestScore = Infinity;
            for (const lab of labs) {
                const lr = lab.getBoundingClientRect();
                if (lr.width === 0 && lr.height === 0) continue;
                for (const c of ctrls) {
                    const cr = c.getBoundingClientRect();
                    if (cr.width === 0 && cr.height === 0) continue;        // hidden
                    const sameLine = (cr.bottom > lr.top + 2) && (cr.top < lr.bottom - 2);
                    if (!sameLine) continue;                                // different row
                    if (cr.left < lr.left - 2) continue;                    // must be to the right
                    const score = (cr.left - lr.right) + Math.abs(
                        (cr.top + cr.bottom) / 2 - (lr.top + lr.bottom) / 2);
                    if (score < bestScore) { bestScore = score; best = c; }
                }
            }
            if (best) return best.value != null ? best.value : '';

            // Fallback: nearest control after the first label in document order.
            for (const c of ctrls) {
                if (labs[0].compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) {
                    return c.value != null ? c.value : '';
                }
            }
            return '';
        }""", label)
    except Exception:
        val = ""

    val = (val or "").strip()
    if val:
        if not quiet:
            print(f"  ✅  Read New India '{label}': {val}")
        return val
    if not quiet:
        print(f"  ⚠️  Could not read New India '{label}'")
    return ""


def _ni_find_select(page, label):
    """Find a New India dropdown (<select>) by the words next to it. None if missing.
    The <select>-specific XPaths are tried FIRST so a label like 'Customer' lands on
    the real dropdown and never on a same-named text box (e.g. 'Customer Name')."""
    sc = _ni_scope(page)
    getters = [
        # EXACT label in its own cell → the dropdown in the next cell (precise)
        lambda: sc.locator(
            f'xpath=//td[normalize-space(.)="{label}"]/following-sibling::td[1]//select[1]'
        ).first,
        # a LEAF cell containing the label → next cell's dropdown
        lambda: sc.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following-sibling::td[1]//select[1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//td[contains(normalize-space(.),"{label}") and not(.//td)]/following::select[1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::select[1]'
        ).first,
        lambda: sc.get_by_label(label, exact=False).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() > 0:
                return el
        except Exception:
            continue
    return None


def ni_select_exact(page, label: str, option_text: str) -> bool:
    """Pick an EXACT option in a New India dropdown found by its label."""
    sel = _ni_find_select(page, label)
    if sel is not None:
        try:
            sel.select_option(label=option_text)
            print(f"  ✅  Selected '{label}' = {option_text}")
            ni_settle(page)
            return True
        except Exception:
            pass
    # Fallback: open it and click the option text.
    try:
        sc = _ni_scope(page)
        if sel is not None:
            sel.click()
        else:
            sc.get_by_text(label, exact=False).first.click()
        page.wait_for_timeout(300)
        sc.get_by_text(option_text, exact=True).first.click()
        print(f"  ✅  Selected '{label}' = {option_text} (click fallback)")
        ni_settle(page)
        return True
    except Exception:
        print(f"  ⚠️  Could not select '{label}' = {option_text} — please set it by hand.")
        return False


def ni_select_contains(page, label: str, substrings) -> bool:
    """Pick the dropdown option that contains ALL of the given substrings
    (case-insensitive). Used when we don't know the exact option text — e.g. the
    car Make / Model / Body Type / Coverage Type lists. Prints which option it
    chose so you can double-check it picked the right one.
    """
    wanted = [str(s).upper() for s in substrings]
    sel = _ni_find_select(page, label)
    if sel is None:
        print(f"  ⚠️  Could not find the '{label}' dropdown — please set it by hand "
              f"(looking for an option containing: {', '.join(map(str, substrings))}).")
        return False
    try:
        options = sel.locator("option").all_inner_texts()
    except Exception:
        options = []
    chosen = None
    for opt in options:
        up = opt.upper()
        if all(w in up for w in wanted):
            chosen = opt
            break
    if chosen is None:
        print(f"  ⚠️  No '{label}' option contained {', '.join(map(str, substrings))} — "
              f"please pick it by hand. Options seen: {options}")
        return False
    try:
        sel.select_option(label=chosen)
        print(f"  ✅  Selected '{label}' = {chosen}   (matched: {', '.join(map(str, substrings))})")
        ni_settle(page)
        return True
    except Exception:
        try:
            sc = _ni_scope(page)
            sel.click()
            page.wait_for_timeout(300)
            sc.get_by_text(chosen, exact=True).first.click()
            print(f"  ✅  Selected '{label}' = {chosen} (click fallback)")
            ni_settle(page)
            return True
        except Exception:
            print(f"  ⚠️  Found option '{chosen}' for '{label}' but could not select it — please set it by hand.")
            return False


def ni_choose_radio(page, field_label: str, option_text: str) -> bool:
    """Click a radio button (e.g. Cash / Card) by the label next to it, searched
    near the field's heading so we click the correct group."""
    sc = _ni_scope(page)
    getters = [
        # a radio whose very next element (label OR span OR text) is the wanted
        # option, sitting after the field heading — clicking the radio ticks it
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(.),"{field_label}")]/following::input[@type="radio"]'
            f'[following-sibling::*[1][normalize-space(.)="{option_text}"]][1]'
        ).first,
        # a radio whose associated <label> is the wanted option, after the heading
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(.),"{field_label}")]/following::input[@type="radio"]'
            f'[following-sibling::label[1][normalize-space(.)="{option_text}"]][1]'
        ).first,
        # the option's own label/span after the heading (clicking a <label for> ticks it)
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(.),"{field_label}")]/following::*[self::label or self::span]'
            f'[normalize-space(.)="{option_text}"][1]'
        ).first,
        # any label with exactly that text, anywhere
        lambda: sc.locator(
            f'xpath=//label[normalize-space(.)="{option_text}"]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() == 0:
                continue
            el.scroll_into_view_if_needed(timeout=10000)
            el.click()
            print(f"  ✅  {field_label}: chose '{option_text}'")
            ni_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not set {field_label} = {option_text} — please choose it by hand.")
    return False


def ni_click_tab(page, tab_text: str) -> bool:
    """Click one of the form's tabs by its visible text, then wait for the reload.
    The tabs live inside the form's iframe, so we look there (via _ni_scope)."""
    sc = _ni_scope(page)
    getters = [
        lambda: sc.locator(f'a:has-text("{tab_text}")').first,
        lambda: sc.locator(f'span:has-text("{tab_text}")').first,
        lambda: sc.locator(f'td:has-text("{tab_text}")').first,
        lambda: sc.locator(f'li:has-text("{tab_text}")').first,
        lambda: sc.locator(f'div:has-text("{tab_text}")').first,
        lambda: sc.locator(
            f'xpath=//*[normalize-space(.)="{tab_text}" and (self::a or self::span or self::td or self::li or self::div)]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            if el.count() > 0 and el.is_visible():
                el.scroll_into_view_if_needed(timeout=10000)
                el.click()
                print(f"  ✅  Opened tab '{tab_text}'")
                ni_settle(page)
                return True
        except Exception:
            continue
    print(f"  ⚠️  Could not find the '{tab_text}' tab — please click it by hand.")
    return False


def ni_set_checkbox(page, label: str, checked: bool) -> bool:
    """Tick a checkbox found by the words next to it. Does NOTHING when checked is
    False (we only ever tick add-ons on, never off). 'label' is matched loosely,
    so 'UAE Exten' matches both 'UAE Extension' and the misspelled 'UAE Extention'.
    """
    if not checked:
        return False
    sc = _ni_scope(page)
    getters = [
        lambda: sc.get_by_label(label, exact=False).first,
        lambda: sc.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/preceding::input[@type="checkbox"][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(.),"{label}")]/preceding::input[@type="checkbox"][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//label[contains(normalize-space(.),"{label}")]/following::input[@type="checkbox"][1]'
        ).first,
        lambda: sc.locator(
            f'xpath=//*[contains(normalize-space(.),"{label}")]/following::input[@type="checkbox"][1]'
        ).first,
    ]
    for g in getters:
        try:
            cb = g()
            if cb.count() == 0:
                continue
            cb.scroll_into_view_if_needed(timeout=10000)
            if not cb.is_checked():
                try:
                    cb.check()
                except Exception:
                    cb.click()
            print(f"  ✅  Ticked '{label}'")
            ni_settle(page)
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not tick '{label}' — please tick it by hand if needed.")
    return False


# ── New India small value helpers ─────────────────────────────────────────────

def reformat_plate_for_ni(vehicle_number: str) -> str:
    """New India wants the plate as CODE/NUMBER with no spaces, e.g.
    'A A - 15030' → 'AA/15030'. Reuses split_plate so it matches the MIC logic."""
    code, number = split_plate(vehicle_number)
    return code.replace(" ", "") + "/" + number


def compute_commencing_date_ni(prev_expiry_str: str) -> str:
    """Same start-date rule as MIC, but in New India's dd/mm/yyyy format:
      expired / unreadable → today;  today or future → expiry + 1 day."""
    expiry = parse_tameen_date(prev_expiry_str)
    today = date.today()
    if expiry is None or expiry < today:
        start = today
    else:
        start = expiry + timedelta(days=1)
    return start.strftime("%d/%m/%Y")


def ni_body_type_target(body_type: str, seats: str):
    """Turn the Tameen Body Type (+ seat count) into the words that MUST appear in
    New India's Body Type option. Returns a list of substrings, or None if we
    can't decide (the caller then leaves Body Type unset and warns)."""
    # New India's real option text (and quirks) seen in the dropdown:
    #   FOUR WHEEL DRIVE (UPTO 15,000) / FOUR WHEEL DRIVE(15001-50000)
    #   SALOON (UPTO 15,000)           / SALOON(15001-50000)
    #   HATCH BACK (UPTO 15000)        / HATCH BACK (15001 - 50000)   ← "HATCH BACK", a space
    #   PICKUP - UPTO 3 TONS           / PICKUP - 4WD
    # The "(UPTO 15...)" vs "(15001-50000)" split is by VEHICLE VALUE. We don't get a
    # value from Tameen, so we DEFAULT to the lower "UPTO 15" bracket and print which
    # option was picked so it can be checked. ("UPTO 15" matches both "15000" and
    # "15,000" spellings.)
    b = (body_type or "").upper()
    seat_n = None
    digits = "".join(ch for ch in str(seats) if ch.isdigit())
    if digits:
        seat_n = int(digits)
    if "SUV" in b or "4 WHEEL" in b or "FOUR WHEEL" in b:
        return ["FOUR WHEEL DRIVE", "UPTO 15"]
    if "SALOON" in b:
        return ["SALOON", "UPTO 15"]
    if "HATCHBACK" in b or "HATCH BACK" in b:
        return ["HATCH BACK", "UPTO 15"]
    if "PICKUP" in b and seat_n == 3:
        return ["PICKUP", "3 TON"]
    if "PICKUP" in b and seat_n == 5:
        return ["PICKUP", "4WD"]
    return None


def read_tameen_addons(page) -> str:
    """Read the Tameen 'Addons Details' SECTION (not a normal copyable field).

    On the Tameen record this is a section heading 'Addons Details' followed by
    EITHER the text 'No addons available' OR one/more 'Addon Name' entries such as
    'UAE COVER'. Normal read_field() can't read it (there is no copy icon), so we
    grab everything between the 'Addons Details' heading and the next section
    heading, drop the 'Addon Name' labels, and return just the add-on name(s).
    Returns '' when there are no add-ons.
    """
    try:
        text = page.evaluate("""() => {
            const all = [...document.querySelectorAll('*')];
            const header = all.find(e => (e.innerText || '').trim() === 'Addons Details');
            if (!header) return '';
            const hy = header.getBoundingClientRect().top;
            // Find the next section heading below, to bound the add-ons area.
            const stops = ['Document Details', 'Additional Drivers', 'Vehicle Details'];
            let nextY = Infinity;
            for (const e of all) {
                const t = (e.innerText || '').trim();
                if (stops.includes(t)) {
                    const y = e.getBoundingClientRect().top;
                    if (y > hy && y < nextY) nextY = y;
                }
            }
            // Collect leaf text sitting between the heading and the next section.
            const out = [];
            for (const e of all) {
                if (e.children.length !== 0) continue;        // leaf elements only
                const y = e.getBoundingClientRect().top;
                if (y <= hy || y >= nextY) continue;
                const t = (e.innerText || '').trim();
                if (t) out.push(t);
            }
            return out.join(' | ');
        }""")
    except Exception:
        text = ""
    text = (text or "").strip()
    if not text or "no addons" in text.lower():
        print("  ℹ️  Tameen add-ons: none")
        return ""
    # Drop the literal 'Addon Name' labels so only the add-on value(s) remain.
    cleaned = " ".join(part.strip() for part in text.split(" | ")
                       if part.strip().lower() != "addon name")
    print(f"  ✅  Tameen add-ons: {cleaned}")
    return cleaned


# ══════════════════════════════════════════════════════════════════════════════
#  NEW INDIA  —  FLOW HELPERS  (one per stage of the form)
# ══════════════════════════════════════════════════════════════════════════════

def ni_login_if_needed(page) -> None:
    """Log in to New India if the login page is showing; otherwise carry on.
    No OTP on New India — just username + password."""
    print("\n── New India: checking if login is needed ──")
    page.wait_for_timeout(1500)

    pwd = None
    for sel in ['input[type="password"]', 'input[id*="pass" i]', 'input[name*="pass" i]']:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=8000):
                pwd = cand
                break
        except Exception:
            continue
    if pwd is None:
        print("  ✅  Already logged in (no login page detected)")
        return
    if not NI_USERNAME or not NI_PASSWORD:
        print("  ⚠️  New India login page is showing but .env has no NI_USERNAME/NI_PASSWORD.")
        print("      Please add them to .env, or log in by hand now.")
        return

    print(f"  🔑  Login page detected — signing in as '{NI_USERNAME}'...")
    for sel in ['input[type="text"]', 'input[id*="user" i]', 'input[name*="user" i]', 'input:not([type])']:
        try:
            box = page.locator(sel).first
            if box.is_visible(timeout=8000):
                box.click(); box.fill(""); box.type(NI_USERNAME, delay=20)
                break
        except Exception:
            continue
    try:
        pwd.click(); pwd.fill(""); pwd.type(NI_PASSWORD, delay=20)
    except Exception:
        print("  ⚠️  Could not type the password — please log in by hand.")
        return

    clicked = False
    for sel in ['input[type="submit"]', 'button:has-text("Log In")', 'button:has-text("Login")',
                'a:has-text("Log In")', 'input[value*="Log" i]']:
        try:
            b = page.locator(sel).first
            if b.is_visible():
                b.click(); clicked = True; break
        except Exception:
            continue
    if not clicked:
        try:
            pwd.press("Enter")
        except Exception:
            pass
    ni_settle(page)

    still_login = False
    try:
        still_login = page.locator('input[type="password"]').first.is_visible(timeout=6000)
    except Exception:
        still_login = False
    if still_login:
        print("  ⚠️  Still on the login page — sign-in may have failed. Please check NI_USERNAME/NI_PASSWORD.")
    else:
        print("  ✅  Logged in to New India")


def _ni_form_present(page) -> bool:
    """True once the Motor Policy form is actually loaded somewhere we can reach it."""
    return _ni_scope(page) is not page


def ni_go_to_motor_policy(page) -> None:
    """Open the Motor Policy form.

    Primary path: go STRAIGHT to the form's URL — this is reliable once logged in
    and avoids the flaky Transactions hover-menu (which was silently failing and
    leaving us on the welcome page). If that doesn't bring up the form, fall back
    to the menu. We then VERIFY the form is really present instead of assuming it.
    """
    print("\n── New India: opening the Motor Policy form ──")

    # 1) Direct navigation to the form.
    try:
        page.goto(NI_MOTOR_POLICY_URL, wait_until="domcontentloaded", timeout=60000)
        ni_settle(page)
    except Exception as e:
        print(f"  …  direct open failed ({e}); will try the Transactions menu")

    # 2) Fallback: the Transactions → Motor Policy menu, if the form isn't here yet.
    if not _ni_form_present(page):
        print("  …  form not detected — trying the Transactions menu")
        for sel in ['a:has-text("Transactions")', 'span:has-text("Transactions")', ':text("Transactions")']:
            try:
                m = page.locator(sel).first
                if m.is_visible():
                    m.hover(); break
            except Exception:
                continue
        page.wait_for_timeout(600)
        clicked = False
        for sel in ['a:has-text("Motor Policy")', 'span:has-text("Motor Policy")', ':text("Motor Policy")']:
            try:
                mp = page.locator(sel).first
                if mp.is_visible():
                    mp.click(); clicked = True; break
            except Exception:
                continue
        if not clicked:
            page.evaluate("""() => {
                const t = [...document.querySelectorAll('a, span, li, div')]
                    .find(e => (e.innerText || '').trim() === 'Motor Policy');
                if (t) t.click();
            }""")
        try:
            page.wait_for_url("**mtrPolicy.aspx**", timeout=60000)
        except Exception:
            pass
        ni_settle(page)

    # 3) Verify — don't lie about success.
    if _ni_form_present(page):
        print("  ✅  Motor Policy form open")
    else:
        print("  ⚠️  Could NOT open the Motor Policy form automatically.")
        print("      Please click Transactions → Motor Policy by hand, then re-run / continue.")


def ni_report_scope(page) -> None:
    """Print where the form was found (which tab + whether inside a frame) so any
    problem is easy to see in the terminal and paste back for fixing."""
    try:
        pages = page.context.pages
        print(f"  🔎  New India browser has {len(pages)} tab(s):")
        for i, pg in enumerate(pages):
            try:
                print(f"       tab {i}: {pg.url}")
            except Exception:
                print(f"       tab {i}: (url unavailable)")
    except Exception:
        pass
    sc = _ni_scope(page)
    if sc is page:
        print("  🔎  Could NOT locate the Motor Policy form in any tab/frame yet.")
    else:
        try:
            has_reg = sc.locator('xpath=//*[contains(normalize-space(.),"Reg.No")]').count() > 0
        except Exception:
            has_reg = False
        print(f"  🔎  Form located (inside a tab/frame). 'Reg.No' present there: {has_reg}")


def ni_fill_primary_top(page, reg_no: str, license_id: str) -> None:
    """Tab 1 (top): Reg.No, License/Civil ID, the five fixed radios, then click
    'Show Information' and wait for the big data load to finish."""
    print("\n── New India Tab 1 (top): vehicle lookup ──")
    ni_report_scope(page)
    ni_fill_by_label(page, "Reg.No", reg_no)
    ni_fill_by_label(page, "License Number (or) Civil ID", license_id)

    # Field labels are kept short to avoid apostrophe / exact-text issues
    # (e.g. "Oman's First Registration" → match on "First Registration").
    ni_choose_radio(page, "Payment Mode", NI_PAYMENT_MODE)
    ni_choose_radio(page, "First Registration", NI_FIRST_REG)
    ni_choose_radio(page, "Vehicle Type", NI_VEHICLE_TYPE)
    ni_choose_radio(page, "Paybylink", NI_PAYBYLINK)
    ni_choose_radio(page, "Import Vehicle", NI_IMPORT_VEHICLE)

    # Click 'Show Information' — this is the BIG load that fills the rest of the form.
    print("  ⏳  Clicking 'Show Information' and waiting for the vehicle data to load...")
    sc = _ni_scope(page)
    clicked = False
    for sel in ['input[value*="Show Information" i]', 'button:has-text("Show Information")',
                'a:has-text("Show Information")', ':text("Show Information")']:
        try:
            b = sc.locator(sel).first
            if b.count() > 0 and b.is_visible():
                b.scroll_into_view_if_needed(timeout=10000)
                b.click(); clicked = True; break
        except Exception:
            continue
    if not clicked:
        print("  ⚠️  Could not find the 'Show Information' button — please click it by hand.")
    ni_settle(page)
    # Poll until Commencing Date exists AND has a value — that means the load
    # finished. New India is slow, so give it plenty of time (~ up to 40 pauses).
    for _ in range(40):
        val = ni_read_by_label(page, "Commencing Date", quiet=True)
        if val and val.strip():
            break
        page.wait_for_timeout(NI_STEP_PAUSE)
    ni_settle(page)
    print("  ✅  Vehicle information loaded")


def ni_fill_primary_client(page, commencing_date: str, customer_name: str) -> None:
    """Tab 1 (lower): Commencing Date, Customer, Customer Name, Telephone."""
    print("\n── New India Tab 1 (lower): policy details ──")
    # Overwrite the auto-filled commencing date; Escape closes the date-picker.
    ni_fill_by_label(page, "Commencing Date", commencing_date, press_escape=True)
    ni_select_exact(page, "Customer", NI_CUSTOMER)
    ni_fill_by_label(page, "Customer Name", customer_name)
    ni_fill_by_label(page, "Telephone No", NI_TELEPHONE)


def ni_fill_previous_policy(page, mileage: str, color: str, full_name: str):
    """Tab 2 (Previous Policy & Mulkiya): copy the shown Policy Expiry Date into
    Mulkiya Expiry Date, set the fixed values, and READ Brand/Model/Year (which
    New India fills in automatically) so Tab 3 can reuse them.
    Returns (brand, model, year).
    """
    print("\n── New India Tab 2: Previous Policy & Mulkiya Details ──")
    ni_click_tab(page, "Previous Policy")

    # Mulkiya Expiry Date = whatever New India shows as 'Policy Expiry Date'.
    policy_expiry = ni_read_by_label(page, "Policy Expiry Date")
    if policy_expiry:
        ni_fill_by_label(page, "Mulkiya Expiry Date", policy_expiry, press_escape=True)
    else:
        print("  ⚠️  Could not read New India's 'Policy Expiry Date' — set Mulkiya Expiry Date by hand.")

    ni_fill_by_label(page, "Name of the Insured", full_name)

    # READ (do NOT change) the vehicle identity New India already filled in.
    brand = ni_read_by_label(page, "Brand")
    model = ni_read_by_label(page, "Model")
    year  = ni_read_by_label(page, "Year of Manufacturing")
    print(f"  📋  New India already shows  Brand='{brand}'  Model='{model}'  Year='{year}'")

    # These are all plain TEXT boxes on New India (they come pre-filled, e.g.
    # AUTO / YES / NO / GOOD), so we type into them rather than picking a dropdown.
    ni_fill_by_label(page, "Type of Transmission", NI_TRANSMISSION)
    ni_fill_by_label(page, "Music System", NI_MUSIC_SYSTEM)
    ni_fill_by_label(page, "Reading on Odometer", mileage)
    ni_fill_by_label(page, "Colour of the Vehicle", color)
    ni_fill_by_label(page, "External Damages", NI_EXTERNAL_DAMAGES)   # this one is a <textarea>
    ni_fill_by_label(page, "Tyre Condition", NI_TYRE_CONDITION)
    return brand, model, year


def ni_fill_vehicle_details(page, brand: str, model: str, body_type: str, seats: str) -> None:
    """Tab 3 (Vehicle Details): Make (from Brand), Model (from Model, waits for the
    list to reload after Make), and Body Type (from the Tameen body type + seats)."""
    print("\n── New India Tab 3: Vehicle Details ──")
    ni_click_tab(page, "Vehicle Details")

    # Car DB built by ni_car_scraper.py — lets us map a brand that New India lists
    # as a *model* (e.g. 'MINI COOPER' under 'BMW') back to its real Make.
    db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ni_car_db.json")
    if os.path.exists(db_path):
        with open(db_path, encoding="utf-8") as f:
            car_db = json.load(f)
    else:
        car_db = {}
        print("  ⚠️  ni_car_db.json not found — run ni_car_scraper.py once to build the "
              "car database. Falling back to direct match only.")

    if brand:
        if not ni_select_contains(page, "Make", [brand]):
            # Brand may actually be a MODEL under another Make — look it up in the DB.
            bl = brand.lower()
            real_make = None
            for mk, models in car_db.items():
                if any(bl in str(m).lower() for m in models):
                    real_make = mk
                    break
            if real_make:
                print(f"  ℹ️  Brand '{brand}' not found as a Make — found it as a model "
                      f"under '{real_make}'. Selecting {real_make} as Make.")
                ni_select_contains(page, "Make", [real_make])
            else:
                print(f"  ⚠️  Could not find '{brand}' as a Make or in the car database — "
                      "please select Make and Model by hand.")

        # Selecting Make reloads the Model list — wait until it has real options
        # (more than just the placeholder 'Select') before choosing the Model.
        for _ in range(30):
            try:
                model_sel = _ni_find_select(page, "Model")
                if model_sel is not None:
                    opts = [o for o in model_sel.locator("option").all_inner_texts()
                            if o.strip() and o.strip().lower() != "select"]
                    if opts:
                        break
            except Exception:
                pass
            page.wait_for_timeout(NI_STEP_PAUSE)
    else:
        print("  ⚠️  No Brand was read from New India — Make/Model left for you to pick.")

    if model:
        ni_select_contains(page, "Model", [model])

    targets = ni_body_type_target(body_type, seats)
    if targets is None:
        print(f"  ⚠️  Body Type '{body_type}' (seats={seats}) is not in the mapping — "
              "please pick the Body Type by hand.")
    else:
        ni_select_contains(page, "Body Type", targets)
        if "UPTO 15" in targets:
            print("  ℹ️  Body Type value bracket defaulted to 'UPTO 15,000'. If this "
                  "vehicle's value is higher, switch to the '15001-50000' option by hand.")


def ni_fill_premium_calculation(page, policy_type, seats: str, addons: str) -> None:
    """Steps 34–37 (Premium Calculation section): Coverage Type, Seating capacity,
    and the two optional add-on checkboxes (UAE Extension, Roadside Assistance).
    STOPS here — does NOT press Premium Calculator / Save / Print / anything."""
    print("\n── New India: Premium Calculation (final fields) ──")

    # This section sits at the BOTTOM of the Vehicle Details tab (Seating Capacity,
    # U.A.E. Extension, Road Assistance / ERA / IMC, etc.). It is already visible —
    # we do NOT click the 'Premium Calculator' button (that is a submit/action
    # button and must never be pressed here). Coverage Type may not exist on every
    # layout, so if we can't find it we just warn and move on.

    # Coverage Type — only the Third Party branch is defined for now. We match on a
    # few distinctive words because the real option uses '&' ("Third Party with PA
    # Cover to Driver & Family"), so matching the whole phrase with 'and' would miss.
    if policy_type == "Third Party":
        ni_select_contains(page, "Coverage Type", ["Third Party", "PA Cover", "Driver"])
    elif policy_type == "Comprehensive":
        print("  ⚠️  Comprehensive Coverage Type is not defined yet — please pick it by hand.")
    else:
        print(f"  ⚠️  Unknown policy type '{policy_type}' — please pick Coverage Type by hand.")

    # Seating Capacity — ni_fill_by_label already clears the box first, then types.
    if seats:
        ni_fill_by_label(page, "Seating Capacity", seats)
    else:
        print("  ⚠️  No seats value to put in Seating Capacity — please set it by hand.")

    # Add-on checkboxes — only tick what the Tameen add-ons text actually mentions.
    # New India labels them "U.A.E. Extension" and "Road Assistance / ERA / IMC".
    addons_l = (addons or "").lower()
    if not addons:
        print("  ⚠️  No add-ons were read from Tameen — leaving U.A.E. Extension and "
              "Road Assistance UNticked. Please tick them by hand if needed.")
        return
    ni_set_checkbox(page, "U.A.E. Exten", "uae" in addons_l)   # matches 'Extension' / 'Extention'
    ni_set_checkbox(page, "Road Assistance",
                    ("roadside" in addons_l or "road side" in addons_l))


def ni_reset_to_motor_policy(page) -> None:
    """Send New India back to a fresh Motor Policy form for the next record."""
    print("\n── New India reset: opening a fresh Motor Policy form ──")
    try:
        ni_login_if_needed(page)
        ni_go_to_motor_policy(page)
    except Exception as e:
        print(f"  ⚠️  Could not reset New India ({e}) — please open Motor Policy by hand.")


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


def iran_dismiss_popup_ok(page) -> bool:
    """Close an error/info dialog by clicking its OK / Ok / Close button. Returns
    False (no-op) if no popup is showing. Used after 'No, Manual Entry'."""
    for sel in ['button.swal2-confirm', '.swal2-actions button',
                'button:has-text("OK")', 'button:has-text("Ok")',
                'button:has-text("Close")', '.modal-footer button',
                '.modal button:has-text("OK")']:
        try:
            b = page.locator(sel).first
            if b.count() > 0 and b.is_visible():
                b.click()
                print("  ✅  Closed a popup (OK/Close)")
                iran_settle(page)
                return True
        except Exception:
            continue
    return False


# ── IRAN document download (from Tameen) → upload (to IRAN) ────────────────────
#  The four documents IRAN needs. Tameen 'Document Details' label spellings to try
#  on the LEFT; the IRAN FileUpload box label on the RIGHT.
IRAN_DOC_LABELS = {
    "civil_id_front": ["Civil ID Front", "Civil Id Front"],
    "civil_id_back":  ["Civil ID Back", "Civil Id Back"],
    "license_front":  ["Driving License Front", "License Front", "DL Front"],
    "license_back":   ["Driving License Back", "License Back", "DL Back"],
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


def _tameen_doc_url(tameen_page, labels):
    """Find a document's image URL in Tameen's Document Details section, trying the
    given label spellings. First reads an href/src; if none, clicks the element and
    catches the new tab the image opens in. Returns the URL string or None, and
    prints exactly what it found (these selectors are the most likely thing to tweak)."""
    # 1) Read an href/src directly from the labelled element or its row.
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
    # 2) Click the element and catch the new tab that opens with the image.
    for lab in labels:
        try:
            target = tameen_page.locator(f'xpath=//*[normalize-space(.)="{lab}"]').first
            if target.count() == 0:
                continue
            with tameen_page.context.expect_page(timeout=8000) as np:
                target.scroll_into_view_if_needed(timeout=6000)
                target.click()
            newpg = np.value
            try:
                newpg.wait_for_load_state("domcontentloaded")
            except Exception:
                pass
            url = newpg.url
            try:
                newpg.close()
            except Exception:
                pass
            if url and url.lower() != "about:blank":
                print(f"  🔎  '{lab}': found URL via new tab → {url}")
                return url
        except Exception:
            continue
    return None


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
        url = _tameen_doc_url(tameen_page, labels)
        if not url:
            print(f"  ⚠️  Could not find {pretty} in Document Details — upload it by hand on IRAN.")
            continue
        try:
            resp = tameen_page.context.request.get(url)
            if not resp.ok:
                print(f"  ⚠️  {pretty}: download failed (HTTP {resp.status}) — upload by hand. URL: {url}")
                continue
            ext = _iran_guess_ext(url, resp.headers.get("content-type", ""))
            path = os.path.abspath(os.path.join(folder, f"{key}{ext}"))
            with open(path, "wb") as f:
                f.write(resp.body())
            out[key] = path
            print(f"  ✅  downloaded {pretty} -> {path}")
        except Exception as e:
            print(f"  ⚠️  {pretty}: error downloading ({e}) — upload by hand. URL: {url}")
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
    if not addons:
        print("  ℹ️  No Tameen add-ons read — no plan checkboxes to tick.")
    else:
        print(f"  → Tameen add-ons read: {addons}")
        ticked = []
        for keyword, checkbox_label in IRAN_ADDON_MAP.items():
            if keyword in addons_l and iran_tick_plan_addon(page, checkbox_label):
                ticked.append(checkbox_label)
        if ticked:
            print(f"  ✅  Plan checkboxes ticked: {', '.join(sorted(set(ticked)))}")
        else:
            print("  ⚠️  No plan checkbox matched the Tameen add-ons — tick by hand if needed.")
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

    if context.pages:
        context.pages[0].close()

    print("Opening Tameen website...")
    tameen_page = context.new_page()
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
                    ni_fill_vehicle_details(ni_page, brand, model, body_type, seats)
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
