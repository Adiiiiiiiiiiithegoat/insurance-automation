"""
Shared engine for the insurance automation.

Both main.py (MIC only) and test.py (MIC + New India) import everything they need
from here, so the MIC/Tameen helpers live in ONE place instead of being copy-pasted
into both files (which used to drift apart on every fix).

Credentials live in .env (never committed) — see main.py's header for the format.
"""
from datetime import datetime, timedelta, date
from dotenv import load_dotenv
import calendar
import os
import re

load_dotenv()
MIC_USERNAME = os.getenv("MIC_USERNAME", "")
MIC_PASSWORD = os.getenv("MIC_PASSWORD", "")
if not MIC_USERNAME or not MIC_PASSWORD:
    print("⚠️  MIC credentials not found in .env — login will be skipped/may fail.")

# ══ CONFIGURATION (same on every MIC policy) ══
MIC_HOME_URL = "https://portal.muscatinsurance.com:444/ords/r/mic/mic/home"

CUSTOMER_CODE   = "21252"      # step 7
FIXED_ADDRESS   = "Muscat"     # step 10
FIXED_MOBILE    = "99435202"   # step 11
MULKIYA_TYPE    = "Renewal"    # step 12
FIXED_GEO_AREA  = "UAE"        # step 17  (form default is Oman, we change it)
ADDL_BENEFIT_DESC = "ROAD SIDE ASSISTANCE - SILVER"   # step 18

# Policy type options (exact text from the Policy Type dropdown). Defaults to PRIVATE.
POLICY_TYPE_COMPREHENSIVE = "MOTOR PRIVATE COMPREHENSIVE"
POLICY_TYPE_THIRD_PARTY   = "MOTOR PRIVATE THIRD PARTY"

PREMIUM_TOLERANCE = 1.0   # OMR — treat as a match if within 1 OMR
STEP_PAUSE = 500          # ms pause after most actions

# The six channels, in the order they appear on the "Payments by Channel" page.
TAMEEN_CHANNELS = ["Branchmotor", "Carsecure", "Kioskmotor",
                   "Opal_motor_branch", "Tripsecure", "Mobileapp"]
# The two section headings on that page.
TAMEEN_SECTIONS = ["PAYMENT DONE CASES", "PAYMENT DONE DOCUMENT PENDING CASES"]

# Fake in-page clipboard so read_field's copy-icon strategy never touches the
# employee's real OS clipboard — they can copy/paste other things while this runs.
# Covers both the modern Clipboard API (writeText/readText) and the legacy
# document.execCommand('copy') path some sites still use.
_CLIPBOARD_SHIM_JS = """
() => {
  window.__automationClipboard = '';
  try {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: {
        writeText: (text) => { window.__automationClipboard = String(text); return Promise.resolve(); },
        readText: () => Promise.resolve(window.__automationClipboard),
      },
    });
  } catch (e) {}
  const origExec = document.execCommand ? document.execCommand.bind(document) : null;
  document.execCommand = function(cmd, showUi, value) {
    if (typeof cmd === 'string' && cmd.toLowerCase() === 'copy') {
      const sel = window.getSelection().toString();
      const active = document.activeElement;
      const activeVal = (active && 'value' in active) ? active.value : '';
      window.__automationClipboard = sel || activeVal || window.__automationClipboard;
      return true;
    }
    return origExec ? origExec(cmd, showUi, value) : false;
  };
}
"""


def install_clipboard_shim(context) -> None:
    """Call once, right after launch_persistent_context and before any goto().
    Makes every page in this context (including ones opened later) use a fake
    in-memory clipboard instead of the real OS one — read_field's clipboard.readText()
    calls keep working exactly as before, just against the fake buffer."""
    context.add_init_script(_CLIPBOARD_SHIM_JS)


def _find_label(page, pattern, timeout: int):
    """Wait up to `timeout` for `pattern` to attach, then — if there's more than
    one match (e.g. a hidden duplicate in a filter/search bar) — prefer the
    first *visible* one instead of blindly trusting DOM order."""
    loc = page.get_by_text(pattern)
    try:
        loc.first.wait_for(state="attached", timeout=timeout)
    except Exception:
        return None
    for i in range(loc.count()):
        cand = loc.nth(i)
        try:
            if cand.is_visible():
                return cand
        except Exception:
            continue
    return loc.first   # none reported visible (shouldn't normally happen) — best guess


def read_field(page, label_text: str) -> str:
    """
    Read a Tameen detail field value.
    Strategy 1 (preferred): click the copy icon, then read the clipboard.
    Strategy 2 (fallback):  several XPath traversals to the value element.
    (Unchanged from your working code.)
    """
    # Fast-fail existence check. A label that isn't on this record (e.g. a record
    # whose whole name sits in 'First Name' and has no 'Last Name') must NOT cost
    # 15s+ per traversal — that long silence looked like a hang.
    # ponytail: exact=True string match broke on "First Name"; \s+ in the regex is
    # forgiving of stray/non-breaking whitespace, and [\s:*]* tolerates a trailing
    # ':' or required-field '*' that some labels carry and others don't.
    words = [re.escape(w) for w in label_text.split()]
    strict_pattern = re.compile(r"^[\s*]*" + r"\s+".join(words) + r"[\s:*]*$", re.I)
    label = _find_label(page, strict_pattern, timeout=2500)

    if label is None:
        # Last-resort: same words, but as an unanchored substring — catches a label
        # that's wrapped in extra text (e.g. "Applicant First Name"). Short timeout
        # so records that genuinely lack this field (Mobileapp + Product Name, etc.)
        # still fail fast instead of adding a second full wait.
        loose_pattern = re.compile(r"\s+".join(words), re.I)
        label = _find_label(page, loose_pattern, timeout=800)
        if label is not None:
            print(f"  ℹ️  '{label_text}' matched loosely (label text wraps more than expected)")

    if label is None:
        print(f"  ⚠️  '{label_text}' not on this record — skipping")
        return ""

    # Strategy 1: copy icon → clipboard (bounded; clipboard.readText() stalls when
    # the window isn't OS-focused, so we race it against a timer and fast-fail the click).
    try:
        page.bring_to_front()
        copy_btn = label.locator("xpath=following-sibling::*[1]")
        copy_btn.click(timeout=2500)
        page.wait_for_timeout(300)
        value = page.evaluate(
            "() => Promise.race(["
            "  navigator.clipboard.readText().catch(() => ''),"
            "  new Promise(r => setTimeout(() => r(''), 1500))"
            "])"
        )
        if value and value.strip():
            print(f"  ✅  Read '{label_text}': {value.strip()}")
            return value.strip()
    except Exception:
        pass

    # Strategy 2: DOM traversal to the value element (short timeouts so misses fail fast).
    xpaths = [
        "xpath=../following-sibling::div[1]",
        "xpath=../../following-sibling::div[1]",
        "xpath=../../following-sibling::p[1]",
        "xpath=../following-sibling::*[not(self::svg) and not(self::button)][1]",
        "xpath=following::div[not(.//svg) and not(.//button)][1]",
        "xpath=following::span[not(.//svg)][1]",
    ]
    for xpath in xpaths:
        try:
            val = label.locator(xpath).first.inner_text(timeout=1500).strip()
            if val and val != label_text and len(val) < 500:
                print(f"  ✅  Read '{label_text}' (DOM fallback): {val}")
                return val
        except Exception:
            continue

    print(f"  ⚠️  Could not read '{label_text}'")
    return ""


def keystroke_fill(field_locator, value: str) -> None:
    """Type a value character-by-character so any onChange handlers fire."""
    field_locator.scroll_into_view_if_needed()
    field_locator.click()
    field_locator.press("Control+a")
    field_locator.press("Backspace")
    field_locator.type(value, delay=25)


def parse_tameen_date(s: str):
    """Try several common date formats and return a date object, or None."""
    s = (s or "").strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y",
                "%d-%B-%Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def compute_period_from(expiry) -> str:
    """
    Step 9 business rule, returns MIC format like '11-JUN-2026':
      - if the previous policy is already expired (expiry < today) → start = today
      - otherwise (expiry today or in the future)                  → start = expiry + 1 day
    """
    today = date.today()
    if expiry is None:
        start = today          # safe fallback if we couldn't read the expiry
    elif expiry < today:
        start = today
    else:
        start = expiry + timedelta(days=1)
    return start.strftime("%d-%b-%Y").upper()


def expiry_far_off(expiry, months: int = 1) -> bool:
    """True if the policy expiry is `months` calendar month(s) or more after today.
    Flags records whose previous policy hasn't lapsed yet (renewing too early).
    The one-month mark itself flags: today 26-Jun → 26-Jul and onwards flag."""
    if expiry is None:
        return False
    today = date.today()
    m = today.month - 1 + months
    y = today.year + m // 12
    m = m % 12
    cutoff = date(y, m + 1, min(today.day, calendar.monthrange(y, m + 1)[1]))
    return expiry >= cutoff


def save_download_with_dialog(download) -> None:
    """Give a normal browser 'Save As' dialog for a Playwright-intercepted download.

    Playwright (accept_downloads is on by default) captures downloads to a temp file
    named with a random GUID and suppresses Chrome's native save prompt — so the
    employee's Print → Download produces GUID files with no chance to name them.
    This pops a real Save As dialog so they can name it and save it as a PDF.
    """
    import tkinter as tk
    from tkinter import filedialog

    suggested = download.suggested_filename or ""
    if not suggested.lower().endswith(".pdf"):
        suggested = "MIC_Policy.pdf"          # GUID/extensionless → sensible default

    downloads = os.path.join(os.path.expanduser("~"), "Downloads")

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.asksaveasfilename(
        title="Save policy PDF",
        initialdir=downloads if os.path.isdir(downloads) else None,
        initialfile=suggested,
        defaultextension=".pdf",
        filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
    )
    root.destroy()

    if not path:
        download.cancel()
        print("  ⚠️  Download cancelled (no file name chosen).")
        return
    download.save_as(path)
    print(f"  ✅  Saved download to: {path}")


def enable_download_dialogs(context) -> None:
    """Wire save_download_with_dialog onto every current and future tab/popup, so a
    download started from any tab (including the PDF viewer the Print opens) prompts
    for a name like a normal browser instead of silently saving a GUID file."""
    for pg in context.pages:
        pg.on("download", save_download_with_dialog)
    context.on("page", lambda pg: pg.on("download", save_download_with_dialog))


def split_plate(vehicle_number: str):
    """
    Step 13: Tameen gives e.g. 'B S-4788'.
    Returns (plate_code, plate_number) → ('B S', '4788').
    Splits on the LAST dash; keeps only digits for the number; normalises the code spacing.
    """
    code, _, number = (vehicle_number or "").rpartition("-")
    plate_code   = " ".join(code.upper().split())            # 'B  S' → 'B S'
    plate_number = "".join(ch for ch in number if ch.isdigit())
    return plate_code, plate_number


def wait_for_apex(page, settle_ms: int = 700) -> None:
    """Wait for APEX's AJAX/page-processing to finish, then a short settle."""
    try:
        page.wait_for_load_state("networkidle", timeout=90000)
    except Exception:
        pass
    for sel in ["#apex_wait_overlay", ".u-Processing", ".apex-page-busy", "#wwvFlowForm .u-Processing"]:
        try:
            ov = page.locator(sel).first
            if ov.is_visible(timeout=2000):
                ov.wait_for(state="hidden", timeout=120000)
        except Exception:
            pass
    page.wait_for_timeout(settle_ms)


def mic_fill_by_label(page, label: str, value: str, press_enter: bool = False) -> bool:
    """Fill an APEX input found by its visible label (with XPath fallbacks)."""
    getters = [
        lambda: page.get_by_label(label, exact=False).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.), "{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.), "{label}")]/following::textarea[1]'
        ).first,
    ]
    for g in getters:
        try:
            el = g()
            el.scroll_into_view_if_needed()
            el.click()
            el.press("Control+a")
            el.press("Backspace")
            el.type(value, delay=20)
            if press_enter:
                el.press("Enter")
            print(f"  ✅  Filled '{label}' = {value}")
            return True
        except Exception:
            continue
    print(f"  ⚠️  Could not fill '{label}'")
    return False


def mic_select_by_label(page, label: str, option_text: str) -> bool:
    """Pick an option in an APEX dropdown found by its label."""
    # Try as a native <select> first (most APEX select lists are native)
    try:
        sel = page.get_by_label(label, exact=False).first
        sel.select_option(label=option_text)
        print(f"  ✅  Selected '{label}' = {option_text}")
        return True
    except Exception:
        pass
    # Fallback: open it and click the option text
    try:
        page.get_by_label(label, exact=False).first.click()
        page.wait_for_timeout(300)
        page.get_by_text(option_text, exact=True).first.click()
        print(f"  ✅  Selected '{label}' = {option_text} (click fallback)")
        return True
    except Exception:
        print(f"  ⚠️  Could not select '{label}' = {option_text}")
        return False


def mic_click_button(page, text: str, which: str = "first", per_try_timeout: int = 15000) -> bool:
    """
    Click a button/link by its visible text.
    Only considers elements that are actually VISIBLE and tries each one quickly,
    so it never hangs on a hidden/covered button (e.g. the old Create button
    sitting behind a popup). which='last' picks the last visible match — use this
    for buttons inside a popup, because popups are added later in the page.
    """
    selectors = [
        f'button:has-text("{text}")',
        f'a:has-text("{text}")',
        f'[role="button"]:has-text("{text}")',
        f'input[type="button"][value="{text}"]',
        f'input[type="submit"][value="{text}"]',
        f'span:has-text("{text}")',
    ]
    candidates = []
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                candidates.append(loc.nth(i))
        except Exception:
            continue

    # keep only the ones currently visible on screen
    visible = []
    for c in candidates:
        try:
            if c.is_visible():
                visible.append(c)
        except Exception:
            continue

    if which == "last":
        visible = list(reversed(visible))

    for c in visible:
        try:
            c.scroll_into_view_if_needed(timeout=10000)
            c.click(timeout=per_try_timeout)
            print(f"  ✅  Clicked '{text}'")
            return True
        except Exception:
            continue   # covered or not clickable → try the next visible one

    print(f"  ⚠️  Could not click '{text}'")
    return False


def mic_accept_confirm_dialog(page, appear_timeout: int = 15000) -> bool:
    """
    Click OK on an APEX in-page confirmation dialog such as
    'There are unsaved changes. Do you want to continue?'.

    This dialog is drawn INSIDE the page (its OK button is green, matching the
    site theme) — it is NOT a native browser popup, so it must be clicked in the
    DOM. We WAIT for it to finish animating in, then click its OK with a REAL
    Playwright click (APEX ignores synthetic JS clicks).
    """
    print("  ⏳  Waiting for the confirmation dialog (OK / Cancel)...")
    selectors = ['.ui-dialog', '[role="dialog"]', '.a-Dialog', 'dialog[open]']

    # 1) Poll briefly until a dialog is actually visible (appears just after the click)
    dialog = None
    for _ in range(max(1, appear_timeout // 500)):
        for sel in selectors:
            try:
                d = page.locator(f'{sel}:visible').last
                if d.count() > 0 and d.is_visible():
                    dialog = d
                    break
            except Exception:
                continue
        if dialog is not None:
            break
        page.wait_for_timeout(500)

    # 2) Click the OK button INSIDE that dialog (exact text 'OK'), real click,
    #    so we never accidentally hit 'Cancel'.
    if dialog is not None:
        for getter in [
            lambda: dialog.get_by_role("button", name="OK", exact=True),
            lambda: dialog.locator('button', has_text=re.compile(r'^\s*OK\s*$')),
            lambda: dialog.locator('a', has_text=re.compile(r'^\s*OK\s*$')),
            lambda: dialog.get_by_text("OK", exact=True),
        ]:
            try:
                ok = getter().last
                if ok.count() > 0 and ok.is_visible():
                    ok.scroll_into_view_if_needed(timeout=5000)
                    ok.click(timeout=10000)
                    print("  ✅  Clicked OK on the confirmation dialog")
                    page.wait_for_timeout(400)
                    return True
            except Exception:
                continue
        print("  …  found a dialog but not its OK button — trying fallbacks")

    # 3) Fallbacks: any visible OK button on the page, then pressing Enter
    #    (Enter activates the dialog's default button, which is OK).
    if mic_click_button(page, "OK", which="last", per_try_timeout=6000):
        return True
    try:
        page.keyboard.press("Enter")
        print("  ✅  Pressed Enter to confirm the dialog")
        return True
    except Exception:
        pass

    print("  ⚠️  Could not confirm the dialog automatically — please click OK by hand.")
    return False


def mic_handle_popup_lov(page, field_label: str, value: str) -> bool:
    """
    Drive an APEX popup-LOV field that opens a MODAL with a search box + table
    (this is the Plate Code field — screenshot 'Plate Information').

    Key point: it finds the matching row's POSITION with JavaScript (fast),
    then performs a REAL Playwright click on that row — APEX ignores synthetic
    JS clicks, which is why the old version scrolled to the row but never selected it.

    Exact matching on any cell handles single-letter ('A') and double-letter
    ('B S') plates, even though typing 'A' also matches the letter A inside
    'PRIVATE' and returns many rows.
    """
    print(f"\n  ── Popup field '{field_label}'  →  '{value}' ──")
    want = " ".join(str(value).upper().split())

    # 1) OPEN THE POPUP (click the ≡ trigger next to the field) ────────────────
    opened = False
    for sel in [
        f'xpath=//label[contains(normalize-space(.),"{field_label}")]/following::button[1]',
        f'xpath=//label[contains(normalize-space(.),"{field_label}")]/following::a[1]',
    ]:
        try:
            page.locator(sel).first.click(timeout=20000)
            opened = True
            break
        except Exception:
            continue
    if not opened:
        try:
            page.get_by_label(field_label, exact=False).first.click(timeout=20000)
            opened = True
        except Exception:
            pass
    if not opened:
        print(f"    ⚠️  could not open the '{field_label}' popup")
        return False

    # 2) GET THE VISIBLE DIALOG ───────────────────────────────────────────────
    page.wait_for_timeout(1200)
    dialog = None
    for sel in ['.ui-dialog:visible', '[role="dialog"]:visible', 'dialog[open]', '.a-PopupLOV:visible']:
        try:
            d = page.locator(sel).last
            if d.is_visible(timeout=10000):
                dialog = d
                break
        except Exception:
            continue
    if dialog is None:
        print("    ⚠️  popup dialog did not appear")
        return False

    # 3) TYPE INTO THE POPUP SEARCH BOX (real keystrokes so APEX filters) ──────
    try:
        search = dialog.locator('input[type="text"], input:not([type])').first
        search.click()
        search.fill("")
        search.type(str(value), delay=70)
    except Exception:
        print("    ⚠️  could not type into the popup search box")
        return False
    page.wait_for_timeout(1800)   # let the list filter

    # 4) FIND THE EXACT ROW (JS) AND CLICK IT (real Playwright click) ──────────
    clicked = False
    last_seen = ""
    for attempt in range(6):       # retry while loading more rows if needed
        idx = dialog.evaluate("""(d, want) => {
            const rows = [...d.querySelectorAll('tr')];
            for (let i = 0; i < rows.length; i++) {
                const cells = [...rows[i].querySelectorAll('td')];
                if (!cells.length) continue;
                const hit = cells.some(c =>
                    c.innerText.trim().replace(/\\s+/g, ' ').toUpperCase() === want
                );
                if (hit) return i;
            }
            return -1;
        }""", want)

        if idx is not None and idx >= 0:
            row = dialog.locator('tr').nth(idx)
            link = row.locator('a')
            target = link.first if link.count() > 0 else row.locator('td').first
            try:
                target.scroll_into_view_if_needed(timeout=10000)
                target.click(timeout=20000)        # REAL click
                clicked = True
                break
            except Exception:
                pass

        # not found yet → try to load more rows, then look again
        loaded_more = False
        try:
            more = dialog.locator('button:has-text("Load More"), a:has-text("Load More")').first
            if more.is_visible(timeout=5000):
                more.click()
                page.wait_for_timeout(1000)
                loaded_more = True
        except Exception:
            pass
        if not loaded_more:
            try:
                dialog.evaluate("""d => {
                    const c = d.querySelector('.ui-dialog-content, .a-IRR-tableContainer, table');
                    if (c) c.scrollTop = c.scrollHeight;
                }""")
                page.wait_for_timeout(800)
            except Exception:
                break
        # capture what we can see for debugging
        last_seen = dialog.evaluate("""d => {
            const rows = [...d.querySelectorAll('tr')];
            return rows.slice(0, 12).map(r => {
                const c = r.querySelectorAll('td');
                return c.length ? c[0].innerText.trim() : '';
            }).filter(Boolean).join(' | ');
        }""")

    if not clicked:
        print(f"    ⚠️  no matching row clicked. First-column values seen: {last_seen}")
        return False

    # 5) VERIFY the dialog closed (means the value was accepted) ───────────────
    page.wait_for_timeout(800)
    try:
        still_open = dialog.is_visible(timeout=5000)
    except Exception:
        still_open = False
    if still_open:
        print(f"    ⚠️  clicked a row but popup still open — please check '{field_label}'")
    else:
        print(f"    ✅  selected '{value}'")
    return clicked


def mic_set_cust_code(page) -> bool:
    """
    Step 7 — Cust Code. The instruction is '21252 and then enter'.
    Approach: type the code straight into the field and press Enter. If that
    opens a selection menu/popup, click the exact '21252' row in it. Then verify
    the field actually holds the value.
    """
    print("\n── MIC Step 7: Cust Code = 21252 ──")
    want = CUSTOMER_CODE

    # find the small Cust Code input
    field = None
    for sel in [
        'xpath=//label[contains(normalize-space(.),"Cust Code")]/following::input[not(@type="hidden")][1]',
        'xpath=//*[contains(text(),"Cust Code")]/following::input[not(@type="hidden")][1]',
    ]:
        try:
            f = page.locator(sel).first
            if f.is_visible(timeout=10000):
                field = f
                break
        except Exception:
            continue
    if field is None:
        try:
            field = page.get_by_label("Cust Code", exact=False).first
        except Exception:
            print("  ⚠️  could not find the Cust Code field")
            return False

    # type the value + Enter
    try:
        field.scroll_into_view_if_needed(timeout=10000)
        field.click()
        field.press("Control+a")
        field.press("Backspace")
        field.type(want, delay=40)
        page.wait_for_timeout(500)
        field.press("Enter")
        page.wait_for_timeout(1200)
    except Exception as e:
        print(f"  ⚠️  could not type into Cust Code: {e}")
        return False

    # if a selection popup/menu appeared, click the exact 21252 row
    try:
        dialog = page.locator('.ui-dialog:visible, [role="dialog"]:visible').last
        if dialog.is_visible(timeout=5000):
            idx = dialog.evaluate("""(d, want) => {
                const rows = [...d.querySelectorAll('tr')];
                for (let i = 0; i < rows.length; i++) {
                    const cells = [...rows[i].querySelectorAll('td')];
                    if (cells.some(c => c.innerText.trim() === want)) return i;
                }
                return -1;
            }""", want)
            if idx is not None and idx >= 0:
                row = dialog.locator('tr').nth(idx)
                link = row.locator('a')
                (link.first if link.count() > 0 else row.locator('td').first).click(timeout=20000)
                page.wait_for_timeout(600)
    except Exception:
        pass

    page.keyboard.press("Escape")   # close any leftover menu
    page.wait_for_timeout(400)

    # verify the field now holds 21252
    try:
        current = (field.input_value() or "").strip()
    except Exception:
        current = ""
    if want in current:
        print(f"  ✅  Cust Code confirmed = {current}")
        return True
    else:
        print(f"  ⚠️  Cust Code may not be set (field shows '{current}') — please check")
        return False


def read_premium(page, label: str) -> str:
    """Read a value from the right-hand Premium Details panel by its label."""
    try:
        el = page.get_by_text(label, exact=True).first
        # the value box sits right after the label
        val = el.locator("xpath=following::input[1]").first.input_value(timeout=15000)
        if val:
            return val.strip()
    except Exception:
        pass
    try:
        return (
            page.get_by_text(label, exact=True).first
            .locator("xpath=following::*[normalize-space(text())!=''][1]")
            .inner_text(timeout=15000).strip()
        )
    except Exception:
        return ""


def tameen_go_to_payments(page) -> None:
    """Step 1: click the PAYMENTS tile on the Tameen dashboard.

    SPEED: we let the BROWSER watch for the tile to appear (it checks many times
    per second and returns the instant it shows up), then click it right away with
    a SHORT per-try timeout — so we never sit on the page's 10-minute default while
    the tile briefly flickers or is covered by a loading overlay after login.
    After clicking we wait only for 'domcontentloaded' (HTML ready), not
    'networkidle' (which waits for ALL background traffic to fall silent and can
    take many seconds on a live dashboard). The next step waits for its own button.
    """
    print("\n── Tameen Step 1: Click PAYMENTS tile ──")

    # Wait inside the browser until an element whose exact text is "PAYMENTS" exists.
    try:
        page.wait_for_function(
            """() => [...document.querySelectorAll('p, span, div, a, button')]
                .some(e => (e.innerText || '').trim() === 'PAYMENTS')""",
            timeout=60000,
        )
    except Exception:
        pass  # fall through and still try to click — the fallbacks below will report if missing

    # Try a real Playwright click first, with a SHORT per-try timeout so it can't hang.
    for sel in ['p:has-text("PAYMENTS")', 'span:has-text("PAYMENTS")',
                'div:has-text("PAYMENTS")', 'a:has-text("PAYMENTS")']:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:          # instant check — skip absent types with no waiting
                continue
            el = loc.last
            el.scroll_into_view_if_needed(timeout=5000)
            el.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded")
            print("  ✅  Payments page loaded")
            return
        except Exception:
            continue

    # JavaScript fallback — click the smallest element whose exact text is "PAYMENTS".
    result = page.evaluate("""() => {
        const all = [...document.querySelectorAll('*')];
        let t = all.find(e => e.children.length === 0 && (e.innerText || '').trim() === 'PAYMENTS');
        if (!t) t = all.find(e => (e.innerText || '').trim() === 'PAYMENTS');
        if (t) { t.scrollIntoView({block:'center'}); t.click(); return 'clicked'; }
        return 'not-found';
    }""")
    if result == "clicked":
        page.wait_for_load_state("domcontentloaded")
        print("  ✅  Payments page loaded (JS fallback)")
        return
    raise RuntimeError("Could not find the PAYMENTS tile on the dashboard")


def tameen_click_payments_by_channel(page) -> None:
    """Step 2: click the red 'Payments by Channel' button (top-right of the page).

    SPEED FIX: the old version tried 4 selectors one after another, each waiting up
    to 8 seconds. If the button is a styled <div> (not a real <button>/<a>) the first
    selectors all time out, so it could take ~24 seconds before it finally clicked.
    Now we let the BROWSER watch for the button (it checks many times per second and
    returns the instant it appears), then click it immediately — usually 1–2 seconds.
    """
    print("\n── Tameen Step 2: Click 'Payments by Channel' button ──")

    # Wait inside the browser until ANY element with that text exists (fast polling).
    try:
        page.wait_for_function(
            """() => {
                const w = "payments by channel";
                return [...document.querySelectorAll('button, a, [role=button], input, div, span')]
                    .some(e => ((e.innerText || e.value || "").trim().toLowerCase().includes(w)));
            }""",
            timeout=60000,
        )
    except Exception:
        pass  # fall through and still try to click — the fallbacks below will report if missing

    # The button now exists. Try a real Playwright click first (most reliable),
    # using a SHORT per-try timeout so we never hang.
    for sel in [
        'button:has-text("Payments by Channel")',
        'a:has-text("Payments by Channel")',
        '[role="button"]:has-text("Payments by Channel")',
        ':text("Payments by Channel")',
    ]:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                el.scroll_into_view_if_needed()
                el.click(timeout=20000)
                page.wait_for_load_state("domcontentloaded")
                print("  ✅  'Payments by Channel' page opened")
                return
        except Exception:
            continue

    # JavaScript fallback — find the smallest clickable element with that text and click it.
    result = page.evaluate("""() => {
        const wanted = "payments by channel";
        let t = [...document.querySelectorAll('button, a, [role=button]')]
                  .find(e => (e.innerText || "").trim().toLowerCase().includes(wanted));
        if (!t) {
            const all = [...document.querySelectorAll('*')]
                  .filter(e => (e.innerText || "").trim().toLowerCase().includes(wanted));
            all.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
            t = all[0];
        }
        if (t) { t.scrollIntoView({block:'center'}); t.click(); return 'clicked'; }
        return 'not-found';
    }""")
    if result == "clicked":
        page.wait_for_load_state("domcontentloaded")
        print("  ✅  'Payments by Channel' page opened (JS fallback)")
        return
    raise RuntimeError("Could not find the 'Payments by Channel' button")


def tameen_select_channel(page) -> str:
    """
    Step 3: read the channel tiles from BOTH sections, list them numbered with
    their record counts, ask which one to open, then click it. Channels showing
    a count of 0 are listed but cannot be selected (there is nothing to open).
    Returns the chosen channel name (e.g. 'Mobileapp').
    """
    print("\n── Tameen Step 3: Select a channel ──")

    # Wait for the 'Payments by Channel' page to actually render its tiles.
    print("  ⏳  Waiting for the channel page to load...")
    appeared = False
    for _ in range(20):
        try:
            txt = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            txt = ""
        if "Count" in txt and "PAYMENT DONE" in txt.upper():
            appeared = True
            break
        page.wait_for_timeout(500)
    if not appeared:
        page.wait_for_timeout(2000)   # fallback settle

    # Read every tile: channel name, its count, and which section it sits under.
    data = page.evaluate("""
        (cfg) => {
            const { channels, sectionTitles } = cfg;
            const allEls = [...document.querySelectorAll('*')];

            // Section headings and their vertical (top) positions.
            const headers = [];
            for (const title of sectionTitles) {
                const cand = allEls.filter(el => (el.innerText || "").trim().toUpperCase() === title);
                if (cand.length) {
                    cand.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
                    headers.push({ title, y: cand[0].getBoundingClientRect().top });
                }
            }

            const seen = new Set();
            const tiles = [];
            for (const ch of channels) {
                // Every element whose text is exactly this channel name (one per tile).
                const nameEls = allEls.filter(el => (el.innerText || "").trim().toLowerCase() === ch.toLowerCase());
                for (const nameEl of nameEls) {
                    // Walk up to the tile card: nearest ancestor that also shows "Count".
                    let card = nameEl;
                    for (let d = 0; d < 8; d++) {
                        if ((card.innerText || "").includes("Count")) break;
                        if (!card.parentElement) break;
                        card = card.parentElement;
                    }
                    if (!(card.innerText || "").includes("Count")) continue;
                    if (seen.has(card)) continue;
                    seen.add(card);

                    const m = (card.innerText || "").match(/Count\\s*:?\\s*(\\d+)/i);
                    const count = m ? parseInt(m[1], 10) : null;
                    const top = card.getBoundingClientRect().top;

                    // Section = the nearest heading sitting above this card.
                    let section = null, bestY = -Infinity;
                    for (const h of headers) {
                        if (h.y <= top + 5 && h.y > bestY) { bestY = h.y; section = h.title; }
                    }
                    tiles.push({ channel: ch, count, section });
                }
            }
            return { tiles };
        }
    """, {"channels": TAMEEN_CHANNELS, "sectionTitles": TAMEEN_SECTIONS})

    tiles = (data or {}).get("tiles", [])
    if not tiles:
        raise RuntimeError("Could not read any channel tiles on the 'Payments by Channel' page.")

    # Build the menu, grouped by section in the correct order, channel by channel.
    menu = []
    included = set()
    for section in TAMEEN_SECTIONS:
        for ch in TAMEEN_CHANNELS:
            tile = next((t for t in tiles
                         if (t.get("section") or "").upper() == section.upper()
                         and t["channel"].lower() == ch.lower()), None)
            if tile and id(tile) not in included:
                menu.append(tile)
                included.add(id(tile))
    for t in tiles:                       # safety net for anything not matched above
        if id(t) not in included:
            menu.append(t)
            included.add(id(t))

    # Print the numbered menu, with a heading before each section.
    print("\n" + "=" * 70)
    print("  SELECT A CHANNEL")
    print("=" * 70)
    numbered = []
    shown_section = "___start___"
    for item in menu:
        sec = item.get("section")
        sec_key = sec if sec is not None else "___other___"
        if sec_key != shown_section:
            shown_section = sec_key
            heading = (sec.title() if sec else "Other") + " Channel Select:"
            print(f"\n  {heading}")
        numbered.append(item)
        n = len(numbered)
        count_str = "" if item.get("count") is None else f"  (count: {item['count']})"
        print(f"    [{n:>2}]  {item['channel']}{count_str}")
    print("\n" + "=" * 70)

    # Ask which channel to open. Block channels with a count of 0.
    while True:
        raw = input(f"\nEnter channel number to open (1–{len(numbered)}): ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("  Please enter a valid number in range.")
            continue
        if not (1 <= choice <= len(numbered)):
            print("  Please enter a valid number in range.")
            continue
        if numbered[choice - 1].get("count") == 0:
            print("  ⚠️  That channel has 0 records — there is nothing to open. Pick another.")
            continue
        break

    selected = numbered[choice - 1]
    label = selected["channel"] + (f" — {selected['section'].title()}" if selected.get("section") else "")
    print(f"\n  Opening channel: {label}")

    # Click the chosen tile — re-found fresh by channel + section so it is never stale.
    result = page.evaluate("""
        (args) => {
            const { channel, section } = args;
            const allEls = [...document.querySelectorAll('*')];

            const headerY = {};
            for (const el of allEls) {
                const t = (el.innerText || "").trim().toUpperCase();
                if (t === "PAYMENT DONE CASES" && headerY["PAYMENT DONE CASES"] === undefined)
                    headerY["PAYMENT DONE CASES"] = el.getBoundingClientRect().top;
                if (t === "PAYMENT DONE DOCUMENT PENDING CASES" && headerY["PAYMENT DONE DOCUMENT PENDING CASES"] === undefined)
                    headerY["PAYMENT DONE DOCUMENT PENDING CASES"] = el.getBoundingClientRect().top;
            }

            const seen = new Set();
            const cards = [];
            for (const el of allEls) {
                if ((el.innerText || "").trim().toLowerCase() !== channel.toLowerCase()) continue;
                let card = el;
                for (let d = 0; d < 8; d++) {
                    if ((card.innerText || "").includes("Count")) break;
                    if (!card.parentElement) break;
                    card = card.parentElement;
                }
                if (!(card.innerText || "").includes("Count")) continue;
                if (seen.has(card)) continue;
                seen.add(card);
                cards.push(card);
            }
            if (!cards.length) return 'not-found';

            let chosen = cards[0];
            if (section && headerY[section] !== undefined) {
                const hy = headerY[section];
                let best = null, bestDist = Infinity;
                for (const c of cards) {
                    const top = c.getBoundingClientRect().top;
                    if (top >= hy - 5) {
                        const dist = top - hy;
                        if (dist < bestDist) { bestDist = dist; best = c; }
                    }
                }
                if (best) chosen = best;
            }
            chosen.scrollIntoView({ block: 'center' });
            chosen.click();
            return 'clicked';
        }
    """, {"channel": selected["channel"], "section": selected.get("section")})

    if result != "clicked":
        raise RuntimeError(f"Could not click the '{selected['channel']}' channel tile.")

    page.wait_for_load_state("domcontentloaded")
    print("  ✅  Channel opened")

    # Wait for the records table to appear before the row-selection step.
    print("  ⏳  Waiting for the records table to load...")
    table_loaded = False
    for sel in ["table tbody tr",
                "[role='row']:not([class*='header'])",
                "[class*='tr']:not([class*='header'])"]:
        try:
            page.wait_for_selector(sel, state="visible", timeout=90000)
            prev = 0
            for _ in range(10):
                c = page.locator(sel).count()
                if c > 0 and c == prev:
                    break
                prev = c
                page.wait_for_timeout(500)
            print(f"  ✅  Table loaded ({prev} rows detected)")
            table_loaded = True
            break
        except Exception:
            continue
    if not table_loaded:
        print("  ⚠️  Could not confirm the table — waiting 4 s as a fallback")
        page.wait_for_timeout(4000)

    return selected["channel"]


def _tameen_on_payments_page(page) -> bool:
    """True if the red 'Payments by Channel' button is currently on the page."""
    try:
        return page.evaluate("""() => {
            const w = "payments by channel";
            return [...document.querySelectorAll('button, a, [role=button], input, div, span')]
                .some(e => ((e.innerText || e.value || "").trim().toLowerCase().includes(w)));
        }""")
    except Exception:
        return False


def tameen_reset_to_payments(page) -> None:
    """Send Tameen back to the Payments page (where 'Payments by Channel' lives)
    using IN-APP navigation only — no full reload — so the login/OTP session is
    preserved.

    Strategy: press the browser Back button a few times, checking after each one
    whether the 'Payments by Channel' button is showing. If Back overshoots all
    the way to the dashboard, just re-open the PAYMENTS tile instead.
    """
    print("\n── Tameen reset: returning to the Payments page ──")
    for _ in range(4):
        if _tameen_on_payments_page(page):
            print("  ✅  Back on the Payments page")
            return
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            break
        page.wait_for_timeout(800)

    if _tameen_on_payments_page(page):
        print("  ✅  Back on the Payments page")
        return

    # Fallback: we are probably on the dashboard now → click the PAYMENTS tile again.
    try:
        tameen_go_to_payments(page)
    except Exception:
        print("  ⚠️  Could not confirm the Payments page — please navigate to it by hand.")


def mic_reset_to_home(page) -> None:
    """Return MIC to its home page so the next policy can be started fresh.

    The next record's mic_open_policy_create() clicks Policy → Create from here,
    and mic_login_if_needed() will sign back in if the session has dropped.
    """
    print("\n── MIC reset: returning to the home page ──")
    try:
        page.goto(MIC_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        print("  ✅  MIC home page loaded")
    except Exception as e:
        print(f"  ⚠️  Could not load the MIC home page ({e}) — please open it by hand.")


def mic_login_if_needed(page) -> None:
    """Step 0: log in to MIC automatically if a login page is showing.

    (If a previous run is still signed in, the saved profile keeps the session, so
    no login is needed and this just prints 'Already logged in'.)
    """
    print("\n── MIC: checking if login is needed ──")
    page.wait_for_timeout(1500)

    # Find the password box — its presence is the clearest sign a login page is up.
    pwd = None
    for sel in ['#P101_PASSWORD', 'input[name="p_password"]', 'input[type="password"]']:
        try:
            cand = page.locator(sel).first
            if cand.is_visible(timeout=15000):
                pwd = cand
                break
        except Exception:
            continue

    if pwd is None:
        print("  ✅  Already logged in (no login page detected)")
        return

    # We have a login page. Make sure we actually have credentials to use.
    if not MIC_USERNAME or not MIC_PASSWORD:
        print("  ⚠️  Login page is showing but .env has no username/password.")
        print("      Please type them into the .env file, or log in by hand now.")
        return

    print(f"  🔑  Login page detected — signing in as '{MIC_USERNAME}'...")

    # Type the username (keystrokes so APEX registers it).
    user_filled = False
    for sel in ['#P101_USERNAME', 'input[name="p_username"]',
                'input[type="text"]', 'input:not([type])']:
        try:
            box = page.locator(sel).first
            if box.is_visible(timeout=10000):
                box.click()
                box.fill("")
                box.type(MIC_USERNAME, delay=20)
                user_filled = True
                break
        except Exception:
            continue
    if not user_filled:
        print("  ⚠️  Could not find the username box — please check the login page.")

    # Type the password into the box we already found.
    try:
        pwd.click()
        pwd.fill("")
        pwd.type(MIC_PASSWORD, delay=20)
    except Exception:
        print("  ⚠️  Could not type the password — please log in by hand.")
        return

    # Submit: try the Sign In / Login button, then fall back to pressing Enter.
    submitted = (mic_click_button(page, "Sign In") or
                 mic_click_button(page, "Login") or
                 mic_click_button(page, "Log In"))
    if not submitted:
        try:
            pwd.press("Enter")
            submitted = True
        except Exception:
            pass

    wait_for_apex(page)

    # Confirm we actually left the login page.
    still_login = False
    try:
        still_login = page.locator('input[type="password"]').first.is_visible(timeout=10000)
    except Exception:
        still_login = False
    if still_login:
        print("  ⚠️  Still on the login page — sign-in may have failed. Please check the username/password.")
    else:
        print("  ✅  Logged in")


def mic_click_tile(page, tile_text: str) -> None:
    """
    Click a home-screen tile by its visible text label.
    MIC tiles are card-style divs (same pattern as Tameen's PAYMENTS tile).
    We try the innermost/most-specific element first, then fall back outward.
    """
    # Let the browser watch for the tile to appear (fast, checks many times/second),
    # so we don't sit on a fixed wait while the page is still drawing.
    try:
        page.wait_for_function(
            "(t) => [...document.querySelectorAll('span, p, a, button, div')]"
            ".some(e => (e.innerText || '').trim() === t)",
            arg=tile_text, timeout=60000,
        )
    except Exception:
        pass

    # Strategy 1: exact text match on the innermost element.
    # count() is instant (no waiting), so element types that don't exist are skipped
    # immediately instead of burning 4 seconds each.
    for el_type in ["span", "p", "a", "button", "div"]:
        try:
            loc = page.locator(f'{el_type}:has-text("{tile_text}")')
            if loc.count() == 0:
                continue
            el = loc.last
            el.scroll_into_view_if_needed(timeout=10000)
            el.click(timeout=20000)
            print(f"  ✅  Clicked tile '{tile_text}' (via {el_type})")
            return
        except Exception:
            continue
    # Strategy 2: JavaScript click — finds the element containing ONLY this text
    result = page.evaluate("""(text) => {
        const all = [...document.querySelectorAll('*')];
        const match = all.find(el =>
            el.children.length === 0 &&
            el.innerText.trim() === text
        );
        if (match) { match.click(); return 'clicked'; }
        // fallback: any element whose trimmed text matches
        const loose = all.find(el => el.innerText.trim() === text);
        if (loose) { loose.click(); return 'clicked-loose'; }
        return 'not-found';
    }""", tile_text)
    if "clicked" in (result or ""):
        print(f"  ✅  Clicked tile '{tile_text}' (JS fallback)")
        return
    raise RuntimeError(f"Could not click tile '{tile_text}'")


def mic_open_policy_create(page) -> None:
    """Steps 1–2: Home → Policy tile → Create button."""
    print("\n── MIC Steps 1–2: Policy tile → Create ──")
    mic_click_tile(page, "Policy")
    wait_for_apex(page)
    # Create is a proper button on the Insurance Policy Report page
    mic_click_button(page, "Create")
    wait_for_apex(page)


def mic_choose_policy_type_and_create(page, product_name: str) -> bool:
    """
    Steps 3–4: the full Create sequence:
      1. First Create click (already done in mic_open_policy_create) opens the popup
      2. Change the Policy Type dropdown
      3. Click Create again inside the popup
      4. Click OK on the confirmation that follows
    Returns is_comprehensive (used later for Sum Insured + premium logic).
    """
    print("\n── MIC Steps 3–4: choose Policy Type → Create → OK ──")
    pn = (product_name or "").lower().replace(" ", "")  # 'Third Party' and 'ThirdParty' both match
    if "thirdparty" in pn:
        policy_type, is_comprehensive = POLICY_TYPE_THIRD_PARTY, False
    elif "comprehensive" in pn:
        policy_type, is_comprehensive = POLICY_TYPE_COMPREHENSIVE, True
    else:
        raise RuntimeError(
            f"Could not decide policy type from Tameen product name: '{product_name}'. "
            "Expected it to contain 'Third Party' or 'Comprehensive'."
        )
    print(f"  Product '{product_name}'  →  {policy_type}")

    # Change the Policy Type dropdown in the popup
    mic_select_by_label(page, "Policy Type", policy_type)
    page.wait_for_timeout(STEP_PAUSE)

    # Second Create click — this one is INSIDE the popup, so use which="last"
    # (the first/original Create button is now hidden behind the popup).
    mic_click_button(page, "Create", which="last")
    page.wait_for_timeout(STEP_PAUSE)
    wait_for_apex(page)

    # OK confirmation that appears after the second Create (same in-page dialog)
    mic_accept_confirm_dialog(page)
    page.wait_for_timeout(STEP_PAUSE)
    wait_for_apex(page)

    return is_comprehensive


def mic_get_licence(page, license_no: str) -> None:
    """Steps 5–6: type License No, click Get Record in 'Get Licence Information'."""
    print("\n── MIC Steps 5–6: Licence No + Get Record ──")
    mic_fill_by_label(page, "License No", license_no)
    # Get Record button inside the Get Licence section (top-most one on the page)
    page.locator('button:has-text("Get Record"), a:has-text("Get Record")').first.click()
    wait_for_apex(page)


def mic_fill_policy_info(page, full_name: str, period_from: str) -> None:
    """Steps 7–12: Cust Code, Name, Period From, Address, Mobile, Mulkiya Type."""
    print("\n── MIC Steps 7–12: Policy Information ──")

    # Step 7 — Cust Code (type 21252 + Enter, handles any popup, verifies)
    mic_set_cust_code(page)
    page.wait_for_timeout(STEP_PAUSE)
    wait_for_apex(page)

    mic_fill_by_label(page, "Insured Name", full_name)        # step 8
    page.wait_for_timeout(STEP_PAUSE)
    mic_fill_by_label(page, "From", period_from)              # step 9 (Period From)
    page.keyboard.press("Escape")
    page.wait_for_timeout(STEP_PAUSE)
    mic_fill_by_label(page, "Address", FIXED_ADDRESS)         # step 10
    page.wait_for_timeout(STEP_PAUSE)
    mic_fill_by_label(page, "Mobile No", FIXED_MOBILE)        # step 11
    page.wait_for_timeout(STEP_PAUSE)
    mic_select_by_label(page, "Mulkiya Type", MULKIYA_TYPE)  # step 12
    page.wait_for_timeout(STEP_PAUSE)


def mic_select_plate_code(page, plate_code: str) -> None:
    """
    Step 14: select the plate code using the shared popup-menu helper.
    Exact matching on the first column handles single-letter ('A') and
    double-letter ('B S') plates correctly.
    """
    print(f"\n── MIC Step 14: select Plate Code '{plate_code}' ──")
    mic_handle_popup_lov(page, "Plate Code", plate_code)


def mic_get_vehicle(page, plate_number: str, plate_code: str) -> None:
    """Steps 13–15: Plate number, Plate code popup, Get Record (vehicle)."""
    print("\n── MIC Steps 13–15: Plate + Get Vehicle Record ──")
    mic_fill_by_label(page, "Plate No", plate_number)        # step 13
    page.wait_for_timeout(STEP_PAUSE)
    mic_select_plate_code(page, plate_code)                  # step 14
    page.wait_for_timeout(STEP_PAUSE)
    # Step 15 — the SECOND 'Get Record' on the page is the vehicle one
    try:
        page.locator('button:has-text("Get Record"), a:has-text("Get Record")').nth(1).click()
    except Exception:
        page.locator('button:has-text("Get Record"), a:has-text("Get Record")').last.click()
    # Step 15 requires waiting a MINIMUM of 8 seconds for the vehicle data to load
    print("  ⏳  Waiting at least 8 seconds for vehicle data to load...")
    page.wait_for_timeout(8000)
    wait_for_apex(page)


def mic_enable_addl_benefit(page) -> None:
    """
    Step 18: in the Addl Benefit table, find the 'ROAD SIDE ASSISTANCE - SILVER'
    row and change its Select dropdown from No to Yes. Only that one row is touched.

    Uses REAL Playwright double-click + select — APEX's editable grid ignores
    synthetic JS events, which is why the previous version didn't actually change it.
    """
    print(f"\n── MIC Step 18: set '{ADDL_BENEFIT_DESC}' to Yes ──")

    # Make sure the grid is on screen (the row must be rendered to interact with it)
    try:
        page.get_by_text(ADDL_BENEFIT_DESC, exact=True).first.scroll_into_view_if_needed(timeout=20000)
    except Exception:
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(600)

    # Locate the row by its (unique) description text
    row = page.locator('tr', has_text=ADDL_BENEFIT_DESC).first
    try:
        row.scroll_into_view_if_needed(timeout=20000)
    except Exception:
        print("  ⚠️  Could not find the Road Side Assistance - Silver row")
        return

    # The Select cell currently shows 'No'. In this row only the Select cell says 'No',
    # so match it exactly; fall back to the first data cell (Select is the first column).
    no_cell = row.locator('td', has_text=re.compile(r'^\s*No\s*$'))
    target_cell = no_cell.first if no_cell.count() > 0 else row.locator('td').first

    # REAL double-click to enter edit mode
    try:
        target_cell.scroll_into_view_if_needed(timeout=10000)
        target_cell.dblclick()
    except Exception as e:
        print(f"  ⚠️  double-click failed: {e}")
        return
    page.wait_for_timeout(700)   # let the inline editor appear

    # Choose 'Yes' in the dropdown that appeared. Try a real native <select> first.
    done = False
    for loc in [row.locator('select'), page.locator('select:visible')]:
        try:
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.select_option(label="Yes")
                done = True
                break
        except Exception:
            continue

    # Fallback: if it's not a native select, click the cell then click the 'Yes' option
    if not done:
        try:
            target_cell.click()
            page.wait_for_timeout(300)
            page.get_by_text("Yes", exact=True).last.click(timeout=20000)
            done = True
        except Exception:
            pass

    if not done:
        print("  ⚠️  Could not switch the dropdown to Yes — please check this row")
        return

    page.keyboard.press("Enter")   # commit the edit
    page.wait_for_timeout(400)

    # Verify: the row's Select cell should now read 'Yes'
    try:
        row_text = (row.inner_text() or "").upper()
        if "YES" in row_text:
            print("  ✅  Road Side Assistance - Silver is now Yes")
        else:
            print("  ⚠️  Set attempted but row still shows No — please check")
    except Exception:
        print("  ✅  Road Side Assistance - Silver set to Yes")

    wait_for_apex(page)


def mic_fill_vehicle_info(page, is_comprehensive: bool, sum_insured: str, seats: str) -> None:
    """Steps 16–19: Geo Area, Addl Benefit, Seats, (Sum Insured if comprehensive).

    ORDER CHANGED ON PURPOSE: the Seats value (read live from Tameen) is now typed
    in IMMEDIATELY AFTER 'Road Side Assistance - Silver' is switched from No to Yes,
    exactly as requested — then the flow carries on to Sum Insured and Calculate.
    """
    print("\n── MIC Steps 16–19: Vehicle Information ──")
    mic_select_by_label(page, "Geo Area", FIXED_GEO_AREA)   # step 17

    mic_enable_addl_benefit(page)                            # step 18 (Road Side Assistance → Yes)

    # Step 16 (moved here): Seats, taken from the Tameen record, typed right after
    # Road Side Assistance - Silver is set to Yes.
    if seats:
        mic_fill_by_label(page, "Seats", seats)
    else:
        print("  ⚠️  No Seats value was read from Tameen — Seats left unchanged. Please check.")

    if is_comprehensive:                                     # step 19
        if sum_insured:
            mic_fill_by_label(page, "Sum Insured", sum_insured)
        else:
            print("  ⚠️  Comprehensive policy but no Sum Insured read from Tameen.")


def mic_calculate_and_check(page, tameen_total: str) -> None:
    """Steps 20–22: Calculate, OK, then compare Net Prem Incl. VAT vs Tameen total."""
    print("\n── MIC Steps 20–22: Calculate + premium check ──")
    mic_click_button(page, "Calculate")                      # step 20
    # step 21: clicking Calculate pops up an APEX in-page dialog
    # 'There are unsaved changes. Do you want to continue?' with a green OK button.
    # It is an HTML dialog (NOT a native browser popup), so we click its OK in the
    # DOM. mic_accept_confirm_dialog waits for it to appear, then clicks OK.
    mic_accept_confirm_dialog(page)
    wait_for_apex(page)

    net_prem = read_premium(page, "Net Prem Incl. VAT")      # step 22
    print(f"\n  MIC  Net Prem Incl. VAT : {net_prem or '(not read)'}")
    print(f"  Tameen Total Premium    : {tameen_total or '(not read)'}")

    def _to_number(s):
        # Strip currency text, commas and spaces so '1,234.50 OMR' → 1234.50
        cleaned = "".join(ch for ch in str(s) if ch.isdigit() or ch in ".-")
        return float(cleaned)

    try:
        diff = abs(_to_number(net_prem) - _to_number(tameen_total))
        if diff <= PREMIUM_TOLERANCE:
            print("  ✅  PREMIUMS MATCH")
        else:
            print()
            print("  " + "!" * 60)
            print("  !!                                                        !!")
            print("  !!         POLICY VALUE MISMATCH — DO NOT CONFIRM         !!")
            print("  !!                                                        !!")
            print("  " + "!" * 60)
            print(f"  !!  MIC premium vs Tameen differ by {diff:.2f} OMR")
            print(f"  !!  Allowed tolerance: {PREMIUM_TOLERANCE:.2f} OMR")
            print("  !!")
            print("  !!  Policy left as DRAFT — review both systems manually")
            print("  !!  before taking any further action.")
            print("  " + "!" * 60)
            print()
    except (ValueError, TypeError):
        print("  ⚠️  Could not compare premiums numerically — check the values above.")
