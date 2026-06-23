from playwright.sync_api import sync_playwright
from datetime import timedelta, date
import os
from common import (
    MIC_HOME_URL,
    read_field, parse_tameen_date, compute_period_from, split_plate,
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
    """Step 4: list the rows we can process (Muscat Insurance AND New India) and
    ask which to open.

    Each row is tagged so you can see which insurer it belongs to, and the chosen
    insurer is returned so the main flow knows which company's form to fill.

    Returns a (status, record_text, company) tuple:
      ("BACK", None, None)            — user typed 0 (go back to the channel select)
      ("OK", "<row text>", "MIC")     — a Muscat Insurance record was opened
      ("OK", "<row text>", "NEW_INDIA") — a New India record was opened
    """
    print("\n── Tameen Step 4: Select which record to open (Muscat Insurance + New India) ──")
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
        row, or None for any other company (those rows are hidden)."""
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        cv = cell_val.lower()
        if "muscat insurance" in cv:
            return "MIC"
        if "new india" in cv:
            return "NEW_INDIA"
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
        print(f"  ⚠️  No Muscat Insurance or New India rows found. Showing all {len(all_rows)} rows.")
        for r in all_rows:
            r["company"] = company_of(r)   # may be None for unknown companies
        filtered = all_rows

    TAG = {"MIC": "[MIC]      ", "NEW_INDIA": "[New India]", None: "[Other]    "}
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

    if brand:
        ni_select_contains(page, "Make", [brand])
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

    mic_page.goto(MIC_HOME_URL, timeout=60000)
    ni_page.goto(NI_LOGIN_URL, timeout=60000)

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
            company     = None     # "MIC" or "NEW_INDIA" — which flow to run

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

                if company not in ("MIC", "NEW_INDIA"):
                    raise RuntimeError(
                        "This record's insurance company is not one we can process "
                        "automatically (only Muscat Insurance and New India are supported)."
                    )
                print(f"\n  → This record is a {'Muscat Insurance' if company == 'MIC' else 'New India'} policy.")

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

                else:  # company == "NEW_INDIA"
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
