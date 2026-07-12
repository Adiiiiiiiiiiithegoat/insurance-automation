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
import difflib
import json
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

NI_USERNAME = os.getenv("NI_USERNAME", "")
NI_PASSWORD = os.getenv("NI_PASSWORD", "")
if not NI_USERNAME or not NI_PASSWORD:
    print("⚠️  New India credentials not found in .env — login will be skipped/may fail.")

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

# Fake in-page clipboard, installed ONLY for the moment read_field's copy-icon
# strategy needs it and restored immediately after — so the employee's real OS
# clipboard is normal at every other instant, including right after the run ends
# (no separate "switch back when done" step needed). Covers both the modern
# Clipboard API (writeText/readText) and the legacy document.execCommand('copy')
# path some sites still use.
_INSTALL_CLIPBOARD_SHIM_JS = """
() => {
  if (window.__automationClipboardActive) return;   // already installed
  window.__automationClipboardActive = true;
  window.__automationClipboard = '';
  window.__realClipboard = navigator.clipboard;
  window.__realExecCommand = document.execCommand.bind(document);
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: {
      writeText: (text) => { window.__automationClipboard = String(text); return Promise.resolve(); },
      readText: () => Promise.resolve(window.__automationClipboard),
    },
  });
  document.execCommand = function(cmd, showUi, value) {
    if (typeof cmd === 'string' && cmd.toLowerCase() === 'copy') {
      const sel = window.getSelection().toString();
      const active = document.activeElement;
      const activeVal = (active && 'value' in active) ? active.value : '';
      window.__automationClipboard = sel || activeVal || window.__automationClipboard;
      return true;
    }
    return window.__realExecCommand(cmd, showUi, value);
  };
}
"""

_RESTORE_CLIPBOARD_JS = """
() => {
  if (!window.__automationClipboardActive) return;
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: window.__realClipboard });
  document.execCommand = window.__realExecCommand;
  window.__automationClipboardActive = false;
}
"""


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
    # The fake clipboard is installed only for this block and always restored after,
    # even on error, so the employee's real clipboard is untouched the rest of the time.
    try:
        page.bring_to_front()
        page.evaluate(_INSTALL_CLIPBOARD_SHIM_JS)
        try:
            copy_btn = label.locator("xpath=following-sibling::*[1]")
            copy_btn.click(timeout=2500)
            page.wait_for_timeout(300)
            value = page.evaluate(
                "() => Promise.race(["
                "  navigator.clipboard.readText().catch(() => ''),"
                "  new Promise(r => setTimeout(() => r(''), 1500))"
                "])"
            )
        finally:
            page.evaluate(_RESTORE_CLIPBOARD_JS)
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


def mic_fill_by_label(page, label: str, value: str, press_enter: bool = False,
                       attempts: int = 3) -> bool:
    """Fill an APEX input found by its visible label (with XPath fallbacks).
    Retries the whole getter list a few times — a field can be briefly absent
    right after a postback from the previous step (e.g. Plate No re-rendering
    after Mulkiya Type), which used to fail this on the first try."""
    getters = [
        lambda: page.get_by_label(label, exact=False).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.), "{label}")]/following::input[not(@type="hidden")][1]'
        ).first,
        lambda: page.locator(
            f'xpath=//label[contains(normalize-space(.), "{label}")]/following::textarea[1]'
        ).first,
    ]
    for attempt in range(attempts):
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
        if attempt < attempts - 1:
            page.wait_for_timeout(1500)                # field may still be re-rendering
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

    # type the value + Enter — retried, because a value that doesn't stick on the
    # first pass (silently swallowed by the popup timing) used to be reported as a
    # warning and left as-is, letting the wrong/blank customer code through.
    for attempt in range(3):
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
            continue

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


def tameen_click_dashboard_tile(page, tile_text: str) -> None:
    """Click a tile on the Tameen dashboard by its exact text (case-insensitive),
    e.g. "PAYMENTS" or "APPLICATIONS".

    SPEED: we let the BROWSER watch for the tile to appear (it checks many times
    per second and returns the instant it shows up), then click it right away with
    a SHORT per-try timeout — so we never sit on the page's 10-minute default while
    the tile briefly flickers or is covered by a loading overlay after login.
    After clicking we wait only for 'domcontentloaded' (HTML ready), not
    'networkidle' (which waits for ALL background traffic to fall silent and can
    take many seconds on a live dashboard). The next step waits for its own button.
    """
    print(f"\n── Tameen Step 1: Click {tile_text} tile ──")
    tile_upper = tile_text.strip().upper()

    # Wait inside the browser until an element whose exact text is the tile name exists.
    try:
        page.wait_for_function(
            """(t) => [...document.querySelectorAll('p, span, div, a, button')]
                .some(e => (e.innerText || '').trim().toUpperCase() === t)""",
            arg=tile_upper,
            timeout=60000,
        )
    except Exception:
        pass  # fall through and still try to click — the fallbacks below will report if missing

    # Try a real Playwright click first, with a SHORT per-try timeout so it can't hang.
    for sel in [f'p:has-text("{tile_text}")', f'span:has-text("{tile_text}")',
                f'div:has-text("{tile_text}")', f'a:has-text("{tile_text}")']:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:          # instant check — skip absent types with no waiting
                continue
            el = loc.last
            el.scroll_into_view_if_needed(timeout=5000)
            el.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded")
            print(f"  ✅  {tile_text} page loaded")
            return
        except Exception:
            continue

    # JavaScript fallback — click the smallest element whose exact text is the tile name.
    result = page.evaluate("""(t) => {
        const all = [...document.querySelectorAll('*')];
        let m = all.find(e => e.children.length === 0 && (e.innerText || '').trim().toUpperCase() === t);
        if (!m) m = all.find(e => (e.innerText || '').trim().toUpperCase() === t);
        if (m) { m.scrollIntoView({block:'center'}); m.click(); return 'clicked'; }
        return 'not-found';
    }""", tile_upper)
    if result == "clicked":
        page.wait_for_load_state("domcontentloaded")
        print(f"  ✅  {tile_text} page loaded (JS fallback)")
        return
    raise RuntimeError(f"Could not find the {tile_text} tile on the dashboard")


def tameen_go_to_payments(page) -> None:
    """Step 1: click the PAYMENTS tile on the Tameen dashboard."""
    tameen_click_dashboard_tile(page, "PAYMENTS")


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
    _ni_check_server_error(page)


class NIServerError(Exception):
    """New India's own ASP.NET form crashed on a postback (their populateBodyType()
    does Convert.ToInt32 on a blank/non-numeric field for certain vehicles and throws
    'Input string was not in a correct format'). It is THEIR server bug on THEIR data —
    we can't fix their code, only detect it and recover. The NI fill is retried once
    (handles a load-race); if it still crashes the record is flagged. See errorlog #2."""


def _ni_check_server_error(page) -> None:
    """Raise NIServerError if New India replaced the form with its yellow 'Server
    Error in / Application' page. Called from ni_settle after every postback so the
    crash aborts the NI fill cleanly instead of cascading into 'field not found'
    errors on a page that is no longer the form."""
    try:
        for pg in page.context.pages:
            for fr in pg.frames:
                try:
                    body = (fr.locator("body").inner_text(timeout=800) or "")
                except Exception:
                    continue
                if "Server Error in" in body and "Input string was not in a correct format" in body:
                    raise NIServerError("New India crashed: 'Input string was not in a "
                                        "correct format' (their populateBodyType postback).")
    except NIServerError:
        raise
    except Exception:
        pass


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


def ni_fill_by_label(page, label: str, value: str, press_escape: bool = False,
                     alt_labels=None) -> bool:
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
    # Last resort: fuzzy, normalized label match (different case/spacing/wording).
    el = _ni_fuzzy_handle(page, [label] + (alt_labels or []), "input")
    if el is not None:
        try:
            el.scroll_into_view_if_needed(timeout=10000)
            el.click()
            el.press("Control+a")
            el.press("Backspace")
            el.type(value, delay=20)
            if press_escape:
                el.press("Escape")
            print(f"  ✅  Filled '{label}' = {value}   (matched loosely)")
            ni_settle(page)
            return True
        except Exception:
            pass
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


# Fuzzy, position-based finder used as a LAST-RESORT fallback by every label
# helper below. New India's labels are not always what we expect — different case
# ("Seating capacity"), spacing/punctuation ("U.A.E Extension" vs "U.A.E. Extention"),
# or abbreviations. This matches a label by its NORMALIZED text (uppercase, letters
# and digits only) against any of several candidate labels, then pairs it with the
# control sitting on the same on-screen line (to the right for inputs/selects, either
# side for checkboxes) — exactly how a human reads the form. Returns a JS element.
_NI_FUZZY_JS = """
    (args) => {
        const {labels, kind} = args;
        const NORM = s => (s || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
        const targets = labels.map(NORM).filter(Boolean);
        // Match only when the label's own text EQUALS or CONTAINS a candidate
        // (forward only, candidate >= 4 chars). The old reverse-containment let a
        // stray 'Type' label bind to the wrong control — that is what mis-filled
        // Coverage/Seating, so it is gone.
        const hit = own => {
            const o = NORM(own);
            if (!o) return false;
            return targets.some(t => o === t || (t.length >= 4 && o.includes(t)));
        };
        const tags = ['td','th','label','span','div','b','strong','font','p','a','li'];
        const labs = [];
        for (const el of document.querySelectorAll(tags.join(','))) {
            let own = '';
            for (const n of el.childNodes) if (n.nodeType === 3) own += n.textContent;
            if (hit(own)) labs.push(el);
        }
        if (!labs.length) return null;
        const isTarget = c => {
            const t = ((c.getAttribute && c.getAttribute('type')) || 'text').toLowerCase();
            if (kind === 'select')   return c.tagName === 'SELECT';
            if (kind === 'checkbox') return c.tagName === 'INPUT' && t === 'checkbox';
            if (c.tagName === 'TEXTAREA') return true;
            return c.tagName === 'INPUT' &&
                !['hidden','checkbox','radio','button','submit','image','file'].includes(t);
        };
        const ctrls = [...document.querySelectorAll('input,textarea,select')].filter(isTarget);
        if (!ctrls.length) return null;
        let best = null, bestScore = Infinity;
        for (const lab of labs) {
            const lr = lab.getBoundingClientRect();
            if (lr.width === 0 && lr.height === 0) continue;
            for (const c of ctrls) {
                const cr = c.getBoundingClientRect();
                if (cr.width === 0 && cr.height === 0) continue;
                const sameLine = (cr.bottom > lr.top + 2) && (cr.top < lr.bottom - 2);
                if (!sameLine) continue;
                let horiz;
                if (kind === 'checkbox') {
                    horiz = Math.abs((cr.left + cr.right) / 2 - (lr.left + lr.right) / 2);
                } else {
                    if (cr.left < lr.left - 2) continue;   // input/select must be to the right
                    horiz = cr.left - lr.right;
                }
                const score = horiz + Math.abs((cr.top + cr.bottom) / 2 - (lr.top + lr.bottom) / 2);
                if (score < bestScore) { bestScore = score; best = c; }
            }
        }
        if (best) return best;
        for (const c of ctrls)          // fallback: first control after the label in the DOM
            if (labs[0].compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) return c;
        return null;
    }
"""


def _ni_fuzzy_handle(page, labels, kind):
    """Return an ElementHandle for the control matched by fuzzy label + on-screen
    position, or None. `kind` is 'input', 'select' or 'checkbox'."""
    sc = _ni_scope(page)
    try:
        handle = sc.evaluate_handle(_NI_FUZZY_JS, {"labels": list(labels), "kind": kind})
        return handle.as_element()
    except Exception:
        return None


def _sel_option_texts(target):
    """Option texts of a dropdown, whether `target` is a Locator or an ElementHandle."""
    try:
        return target.locator("option").all_inner_texts()          # Locator
    except Exception:
        pass
    try:
        return [(o.inner_text() or "") for o in target.query_selector_all("option")]  # handle
    except Exception:
        return []


def _ni_find_select(page, label, alt_labels=None):
    """Find a New India dropdown (<select>) by the words next to it. None if missing.
    The <select>-specific XPaths are tried FIRST so a label like 'Customer' lands on
    the real dropdown and never on a same-named text box (e.g. 'Customer Name').
    Falls back to a fuzzy, normalized match (returns an ElementHandle then)."""
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
    # Last resort: fuzzy, normalized label match (handles different case/spacing).
    return _ni_fuzzy_handle(page, [label] + (alt_labels or []), "select")


def ni_select_exact(page, label: str, option_text: str, alt_labels=None) -> bool:
    """Pick an EXACT option in a New India dropdown found by its label."""
    sel = _ni_find_select(page, label, alt_labels)
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


def _ni_pick_closest_model(page, model_sel, model, brand) -> bool:
    """Fuzzy fallback for the Model dropdown when an exact substring match failed.
    Mulkiya model text is often misspelled ('AVALOON') or abbreviated, so compare
    it against each option (with the brand prefix stripped, so 'TOYOTA ' noise
    doesn't drown out the model) and pick the closest one — but only if it's clearly
    close, so a typo lands on the right car instead of a random model.
    ponytail: difflib ratio + 0.72 cutoff; tune the cutoff if it mis-picks.
    """
    norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
    nb = norm(brand)
    nm = norm(model)
    if nb and nm.startswith(nb):     # mulkiya sometimes repeats the make ('TOYOTA AVALON')
        nm = nm[len(nb):]
    if not nm:
        return False
    best_opt, best_ratio = None, 0.0
    for opt in _sel_option_texts(model_sel):
        no = norm(opt)
        if not no or no == "SELECT":
            continue
        core = no[len(nb):] if nb and no.startswith(nb) else no
        r = difflib.SequenceMatcher(None, nm, core or no).ratio()
        if r > best_ratio:
            best_opt, best_ratio = opt, r
    if best_opt and best_ratio >= 0.72:
        model_sel.select_option(label=best_opt)
        print(f"  ✅  Selected 'Model' = {best_opt}   "
              f"(fuzzy match for '{model}', ratio={best_ratio:.2f})")
        # A confident match (≥0.85) is almost always right. A borderline one
        # (0.72–0.85) is a guess — shout so the operator eyeballs it before saving.
        if best_ratio < 0.85:
            red, reset = "\033[41m\033[97m", "\033[0m"
            print("\n" + red + " " * 70 + reset)
            print(red + f"  🚨  LOW-CONFIDENCE MODEL: '{model}' → '{best_opt}' "
                        f"(ratio={best_ratio:.2f}) — PLEASE VERIFY  ".ljust(70) + reset)
            print(red + " " * 70 + reset)
            try:
                log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "fuzzy_model_log.txt")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%Y-%m-%d %H:%M}  mulkiya='{model}' "
                            f"brand='{brand}' -> '{best_opt}' ratio={best_ratio:.2f}\n")
            except Exception:
                pass
        ni_settle(page)
        return True
    return False


def ni_select_contains(page, label: str, substrings, alt_labels=None) -> bool:
    """Pick the dropdown option that contains ALL of the given substrings
    (case-insensitive). Used when we don't know the exact option text — e.g. the
    car Make / Model / Body Type / Coverage Type lists. Prints which option it
    chose so you can double-check it picked the right one.
    """
    # Match on normalized text (letters+digits only) so a wanted 'HATCH BACK' still
    # matches an option written 'HATCHBACK UPTO 15000', and case/punctuation vary.
    def _n(s): return re.sub(r"[^A-Z0-9]", "", str(s).upper())
    wanted = [_n(s) for s in substrings]
    sel = _ni_find_select(page, label, alt_labels)
    if sel is None:
        print(f"  ⚠️  Could not find the '{label}' dropdown — please set it by hand "
              f"(looking for an option containing: {', '.join(map(str, substrings))}).")
        return False
    options = _sel_option_texts(sel)
    chosen = None
    for opt in options:
        up = _n(opt)
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


def _load_body_type_mapping():
    """Load the Body Type mapping from ni_body_type_mapping.json.
    Returns a dict of {body_type_upper: target_list} or {} if file not found."""
    mapping_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ni_body_type_mapping.json")
    if not os.path.exists(mapping_path):
        return {}
    try:
        with open(mapping_path, encoding="utf-8") as f:
            data = json.load(f)
            mappings = data.get("mappings", {})
            # Normalize keys to uppercase for case-insensitive lookup
            return {k.upper(): v for k, v in mappings.items()}
    except Exception:
        return {}


# ponytail: load once at module level to avoid repeated file I/O
_BODY_TYPE_MAP = _load_body_type_mapping()


def _resolve_body_type_value(val, seat_n):
    """A mapping entry is either a flat list of substrings, or a dict with
    seat-based branches: 'seats_<N>' (exact seat count), 'seats_other' (any
    other known seat count), 'seats_unknown' (no seat count was read)."""
    if isinstance(val, list):
        return val
    if isinstance(val, dict):
        if seat_n is not None:
            for key in (f"seats_{seat_n}", "seats_other"):
                if isinstance(val.get(key), list):
                    return val[key]
        if isinstance(val.get("seats_unknown"), list):
            return val["seats_unknown"]
    return None


def _is_pickup(body_type: str) -> bool:
    """True for ANY pickup body type, however it is spelt: 'pickup', 'pick up',
    'pick-up', 'PICK UP DOUBLE CAB', etc. Punctuation and spacing are ignored."""
    return "PICK" in re.sub(r"[^A-Z0-9]", "", (body_type or "").upper())


def ni_body_type_target(body_type: str, seats: str):
    """Turn the Tameen Body Type (+ seat count) into the words that MUST appear in
    New India's Body Type option. Uses the mapping from ni_body_type_mapping.json.
    Returns a list of substrings, or None if we can't decide (the caller then
    leaves Body Type unset and warns).

    Tries (in order):
      0. Pickup (any spelling): choose 3-TON when 3 seats, else the 4WD pickup
      1. Exact match: body_type as a key in the mapping
      2. Substring match: any key from the mapping that is contained in body_type
      3. Fallback: None (manual selection)
    """
    b_upper = (body_type or "").upper()
    if not b_upper:
        return None

    digits = "".join(ch for ch in str(seats) if ch.isdigit())
    seat_n = int(digits) if digits else None

    # Pickup is special: catch every spelling (incl. 'pick up double cab') up front,
    # and pick the option by seat count. The caller also flags it for manual review.
    if _is_pickup(body_type):
        return ["PICKUP", "3 TON"] if seat_n == 3 else ["PICKUP", "4WD"]

    if not _BODY_TYPE_MAP:
        return None

    if b_upper in _BODY_TYPE_MAP:
        result = _resolve_body_type_value(_BODY_TYPE_MAP[b_upper], seat_n)
        if result is not None:
            return result

    for key, val in _BODY_TYPE_MAP.items():
        if key in b_upper:
            result = _resolve_body_type_value(val, seat_n)
            if result is not None:
                return result

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
    # Blur the field so the site's onchange handler fires and computes Maturity
    # Date — it only appears after focus leaves the field, not just on typing.
    try:
        page.keyboard.press("Tab")
    except Exception:
        pass
    ni_settle(page)
    ni_select_exact(page, "Customer", NI_CUSTOMER)
    ni_fill_by_label(page, "Customer Name", customer_name,
                     alt_labels=["Name of Customer", "Customer's Name", "Insured Name", "Client Name"])
    ni_fill_by_label(page, "Telephone No", NI_TELEPHONE,
                     alt_labels=["Telephone", "Phone No", "Mobile No", "Contact No"])


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


def ni_fill_vehicle_details(page, brand: str, model: str, body_type: str, seats: str,
                             tameen_make: str = "", tameen_model: str = "") -> None:
    """Tab 3 (Vehicle Details): Make (from Brand), Model (from Model, waits for the
    list to reload after Make), and Body Type (from the Tameen body type + seats).
    tameen_make/tameen_model are a fallback for when New India's own vehicle lookup
    doesn't recognize the vehicle and shows 'NOT FOUND' instead of a real Brand/Model.
    """
    print("\n── New India Tab 3: Vehicle Details ──")
    ni_click_tab(page, "Vehicle Details")

    # New India shows this as '** NOT FOUND **' (with asterisks) when its own
    # Mulkiya lookup can't identify the vehicle — substring check, not exact match.
    if not brand or "not found" in brand.strip().lower():
        if tameen_make:
            print(f"  ℹ️  New India Brand was '{brand or '(blank)'}' — using Tameen's "
                  f"Make '{tameen_make}' instead.")
            brand = tameen_make
        else:
            brand = ""
    if not model or "not found" in model.strip().lower():
        if tameen_model and "not found" not in tameen_model.strip().lower():
            print(f"  ℹ️  New India Model was '{model or '(blank)'}' — using Tameen's "
                  f"Model '{tameen_model}' instead.")
            model = tameen_model
        else:
            model = ""

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
                    opts = [o for o in _sel_option_texts(model_sel)
                            if o.strip() and o.strip().lower() != "select"]
                    if opts:
                        break
            except Exception:
                pass
            page.wait_for_timeout(NI_STEP_PAUSE)
    else:
        print("  ⚠️  No Brand was read from New India — Make/Model left for you to pick.")

    if model:
        picked = ni_select_contains(page, "Model", [model])
        if not picked and brand:
            # Exact substring failed — the mulkiya model is often misspelled
            # ('AVALOON' for 'AVALON') or abbreviated. Try the closest option by
            # string similarity BEFORE the generic-brand fallback, so a typo lands
            # on the right car ('TOYOTA AVALON') instead of the generic 'TOYOTA'.
            model_sel = _ni_find_select(page, "Model")
            if model_sel is not None:
                picked = _ni_pick_closest_model(page, model_sel, model, brand)
        if not picked and brand:
            # Some Makes list themselves as their own single generic Model entry
            # (e.g. 'GREAT WALL' has no real models, just 'GREAT WALL' itself).
            # Must be an EXACT match, not substring — a substring match against
            # brands like 'TOYOTA' hits nearly every real model in the list
            # ('TOYOTA AURION V6', 'TOYOTA HILUX', ...) and silently picks the
            # first one, which is almost certainly the wrong vehicle.
            model_sel = _ni_find_select(page, "Model")
            picked = False
            if model_sel is not None:
                norm = lambda s: re.sub(r"[^A-Z0-9]", "", str(s).upper())
                for opt in _sel_option_texts(model_sel):
                    if norm(opt) == norm(brand):
                        model_sel.select_option(label=opt)
                        print(f"  ✅  Selected 'Model' = {opt}   (exact generic-Make match)")
                        ni_settle(page)
                        picked = True
                        break
            if picked:
                print(f"  ℹ️  No '{model}' option existed — used the generic "
                      f"'{brand}' Model entry instead.")
        # Selecting a real Model can trigger another reload (same as Make → Model)
        # that briefly removes/rebuilds the rest of the tab — wait for the Body
        # Type dropdown to actually exist again before touching it.
        for _ in range(30):
            try:
                if _ni_find_select(page, "Body Type") is not None:
                    break
            except Exception:
                pass
            page.wait_for_timeout(NI_STEP_PAUSE)

    # PICKUP needs a human eye — the seat count decides 3-TON vs 4WD and Tameen's
    # labels vary ('pick up double cab', etc). Shout about it, loudly and in red.
    if _is_pickup(body_type):
        red = "\033[41m\033[97m"      # white on red (falls back to plain text if unsupported)
        reset = "\033[0m"
        print("\n" + red + " " * 70 + reset)
        print(red + f"  🚨  PICKUP DETECTED ('{body_type}', seats={seats or '?'}) — PLEASE REVIEW BODY TYPE PROPERLY  ".ljust(70) + reset)
        print(red + " " * 70 + reset)

    targets = ni_body_type_target(body_type, seats)
    # Track unmatched body types for later mapping improvement
    try:
        from ni_body_type_tracker import track_body_type
        track_body_type(body_type, seats, targets)
    except ImportError:
        pass
    if targets is None:
        print(f"  ⚠️  Body Type '{body_type}' (seats={seats}) is not in the mapping — "
              "please pick the Body Type by hand.")
    else:
        ni_select_contains(page, "Body Type", targets)
        if "UPTO 15" in targets:
            print("  ℹ️  Body Type value bracket defaulted to 'UPTO 15,000'. If this "
                  "vehicle's value is higher, switch to the '15001-50000' option by hand.")


# The Premium Calculation section is crowded with look-alike controls (a Gender
# dropdown, several number boxes, several checkboxes) and may live in a different
# frame/tab than the earlier fields. Two prior approaches (label-position, then
# option/id identity) each missed, so this one is EVIDENCE-BASED: it labels every
# control by the text of the table cell IMMEDIATELY BEFORE it (New India's real
# layout — 'label td' then 'control td'), which does not bleed across columns the
# way visual/position matching did. It also returns a full structural DUMP so we can
# see exactly what is on the page instead of guessing. Runs in ONE frame; the Python
# caller runs it across every frame of every tab and keeps the frame that matched.
_NI_SCAN_JS = r"""
    () => {
        const NORM = s => (s || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
        const SHOW = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, 40);
        const vis  = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };

        // Label = text of the nearest non-empty cell/sibling BEFORE the control.
        const labelOf = el => {
            const td = el.closest('td');
            if (td) {
                let prev = td.previousElementSibling;
                while (prev && !prev.innerText.trim()) prev = prev.previousElementSibling;
                if (prev && prev.innerText.trim()) return prev.innerText;
                if (td.innerText.trim()) return td.innerText;         // label shares the cell
            }
            let p = el.previousElementSibling;
            while (p && !p.innerText.trim()) p = p.previousElementSibling;
            if (p && p.innerText.trim()) return p.innerText;
            if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return l.innerText; }
            return el.parentElement ? el.parentElement.innerText : '';
        };

        const selects = [...document.querySelectorAll('select')];
        const inputs  = [...document.querySelectorAll('input')].filter(i => {
            const t = (i.getAttribute('type') || 'text').toLowerCase();
            return !['hidden','button','submit','image','file','reset'].includes(t);
        });

        // COVERAGE: a <select> whose label OR options mention coverage/third party.
        const coverage = selects.find(s => {
            const lbl = NORM(labelOf(s));
            if (lbl.includes('COVERAGE') || lbl.includes('COVERTYPE')) return true;
            return [...s.options].some(o => {
                const t = NORM(o.textContent);
                return t.includes('THIRDPARTY') || t.includes('COMPREHENSIVE') || t.includes('PACOVER');
            });
        }) || null;

        // SEATING: a text/number input whose label OR id/name mentions seat/capacity.
        const seating = inputs.find(i => {
            const t = (i.getAttribute('type') || 'text').toLowerCase();
            if (t === 'checkbox' || t === 'radio') return false;
            const key = NORM(labelOf(i) + ' ' + (i.id || '') + ' ' + (i.name || ''));
            return key.includes('SEAT') || key.includes('CAPACITY');
        }) || null;

        // DUMP: compact, human-readable list of what is actually here (shown only when
        // a control is missing, so the real names are visible for a quick fix).
        const dump = [];
        selects.forEach(s => dump.push('SELECT [' + SHOW(labelOf(s)) + '] id=' + (s.id || '-') +
            ' opts={' + [...s.options].slice(0, 5).map(o => SHOW(o.textContent)).join(' / ') + '}'));
        inputs.forEach(i => {
            const t = (i.getAttribute('type') || 'text').toLowerCase();
            if (!vis(i)) return;
            dump.push((t === 'checkbox' ? 'CHECK ' : 'INPUT ') + '[' + SHOW(labelOf(i)) + '] id=' +
                (i.id || '-') + ' name=' + (i.name || '-') + (t !== 'checkbox' && t !== 'text' ? ' type=' + t : ''));
        });

        return {
            coverage, seating,
            found: !!(coverage || seating),
            note: 'Coverage:' + (coverage ? 'found' : 'MISSING') +
                  ' Seating:' + (seating ? 'found' : 'MISSING'),
            dump,
        };
    }
"""


def _ni_all_frames(page):
    """Every frame in every open tab, with the known form frame FIRST — the Premium
    section may not be in the same frame as the earlier fields."""
    frames = []
    scope = _ni_scope(page)                 # the frame that holds the form (markers)
    if scope is not None:
        frames.append(scope)
    try:
        for pg in page.context.pages:
            for fr in pg.frames:
                if fr not in frames:
                    frames.append(fr)
    except Exception:
        pass
    return frames or [page]


def _ni_premium_controls(page, need=("coverage", "seating"),
                         quiet: bool = False, tries: int = 20):
    """Locate the Premium-section Coverage/Seating controls across ALL frames/tabs and
    return element handles (either may be None).

    These controls render a beat AFTER the Vehicle Details tab loads, and REBUILD
    after a postback (e.g. selecting Coverage). So we POLL — re-scanning every ~400ms
    — until every control named in `need` is present, or we run out of tries. Prints a
    structural dump if something in `need` is still missing after the wait, so the
    real control names stay visible. Callers ask only for what they are about to use,
    which lets each field wait out its own render/postback independently."""
    need = tuple(need)
    out = {"coverage": None, "seating": None}
    best_note = best_dump = None
    for _ in range(max(1, tries)):
        out = {"coverage": None, "seating": None}
        best_dump = None
        for fr in _ni_all_frames(page):
            try:
                h = fr.evaluate_handle(_NI_SCAN_JS)
                dump = h.get_property("dump").json_value() or []
                note = h.get_property("note").json_value()
            except Exception:
                continue                        # frame busy (mid-postback) — skip it
            if best_dump is None and dump:      # first non-empty dump = the form frame
                best_dump, best_note = dump, note
            if h.get_property("found").json_value():
                for k in out:
                    if out[k] is None:
                        el = h.get_property(k).as_element()
                        if el is not None:
                            out[k] = el
                best_note = note
        if all(out[k] is not None for k in need):
            break
        page.wait_for_timeout(400)

    if not quiet:
        print(f"  🔎  Premium controls — {best_note or 'no form frame found'}")
        if best_dump and not all(out[k] is not None for k in need):
            print("  ── form controls found on the page (label / id / name) ──")
            for line in best_dump:
                print(f"       {line}")
            print("  ──────────────────────────────────────────────────────────")
    return out


# Add-on checkboxes, targeted by their EXACT ASP.NET id (learned from the page):
#   U.A.E. Extension        → id ends '...chkUAE'
#   Road Assistance (ERA)   → id ends '...chkERA'   (a separate '...chkIMC' exists)
# A JS synthetic .click() would NOT stick on chkERA (its onclick/postback ignores an
# untrusted event), which is why every "click in the browser" attempt failed. So we
# use a real, TRUSTED Playwright click via a locator on the exact id, re-resolved each
# pass (no stale handle), and VERIFY the box actually reads checked — retrying to ride
# out the U.A.E. postback. Locating by id also removes all label-matching guesswork.
_ADDON_ID_SUFFIX = {"uae": "chkUAE", "road": "chkERA"}

_NI_CHECKBOX_DUMP_JS = r"""
    () => [...document.querySelectorAll('input[type="checkbox"]')].map(cb => {
        const near = (cb.parentElement ? cb.parentElement.innerText : '')
            .replace(/\s+/g, ' ').trim().slice(0, 40);
        return 'CHECK checked=' + (cb.checked ? 'Y' : 'n') + ' id=' + (cb.id || '-') +
               ' name=' + (cb.name || '-') + ' near=[' + near + ']';
    })
"""


def _ni_addon_locator(page, suffix):
    """The checkbox whose id ends with `suffix`, in whichever frame/tab holds it."""
    for fr in _ni_all_frames(page):
        try:
            loc = fr.locator(f'input[id$="{suffix}"]')
            if loc.count() > 0:
                return loc.first
        except Exception:
            continue
    return None


def ni_set_addon(page, which, label, tries: int = 20) -> bool:
    """Guarantee an add-on checkbox ends up TICKED, targeting it by exact id and using
    a real trusted click. The loop condition is the box's ACTUAL checked state, so a
    success means it is genuinely on — retried to survive the U.A.E. postback."""
    suffix = _ADDON_ID_SUFFIX[which]
    for _ in range(tries):
        loc = _ni_addon_locator(page, suffix)
        if loc is None:
            page.wait_for_timeout(600)                 # not rendered yet
            continue
        try:
            if loc.is_checked():
                print(f"  ✅  Ticked '{label}'")
                return True
            if not loc.is_enabled():
                page.wait_for_timeout(500)             # disabled for now — may enable after a postback
                continue
            loc.scroll_into_view_if_needed(timeout=5000)
            loc.click(timeout=5000)                    # real trusted click (fires the postback)
        except Exception:
            pass                                       # postback can detach mid-click; verify next pass
        ni_settle(page)
    loc = _ni_addon_locator(page, suffix)
    try:
        if loc is not None and loc.is_checked():
            print(f"  ✅  Ticked '{label}'")
            return True
    except Exception:
        pass
    print(f"  ⚠️  Could not tick '{label}' — please tick it by hand.")
    for fr in _ni_all_frames(page):
        try:
            lines = fr.evaluate(_NI_CHECKBOX_DUMP_JS)
        except Exception:
            continue
        if lines:
            print("  ── checkboxes on the page ──")
            for ln in lines:
                print(f"       {ln}")
            break
    return False


def ni_fill_premium_calculation(page, policy_type, seats: str, addons: str) -> None:
    """Steps 34–37 (Premium Calculation section): Coverage Type, Seating capacity,
    and the two optional add-on checkboxes (UAE Extension, Roadside Assistance).
    STOPS here — does NOT press Premium Calculator / Save / Print / anything."""
    print("\n── New India: Premium Calculation (final fields) ──")

    # These controls live at the BOTTOM of the Vehicle Details tab. We do NOT click
    # 'Premium Calculator' (a submit button — never pressed here). Each control is
    # located by IDENTITY (option contents / id-name / adjacent text), NOT by label
    # position, because label matching kept binding to the wrong look-alike control.
    # We fetch each control right before using it, WAITING (polling) for it to render
    # — the section appears a beat after the tab loads and rebuilds after a postback.

    # Coverage Type — the <select> whose options mention Third Party / Comprehensive.
    cov = _ni_premium_controls(page, need=("coverage",))["coverage"]
    if cov is None:
        print("  ⚠️  Could not find the Coverage Type dropdown — please pick it by hand.")
    elif policy_type == "Third Party":
        # The real option reads e.g. 'Third Party with PA Cover to Driver & Family'.
        want_words = ["THIRDPARTY", "PACOVER", "DRIVER"]
        opts = _sel_option_texts(cov)
        chosen = next((o for o in opts if all(w in re.sub(r"[^A-Z0-9]", "", o.upper()) for w in want_words)), None)
        if chosen:
            try:
                cov.select_option(label=chosen)
                print(f"  ✅  Selected Coverage Type = {chosen}")
                ni_settle(page)
            except Exception:
                print(f"  ⚠️  Found Coverage option '{chosen}' but could not select it — pick it by hand.")
        else:
            print(f"  ⚠️  No Third-Party coverage option found — pick it by hand. Options: {opts}")
    elif policy_type == "Comprehensive":
        print("  ⚠️  Comprehensive Coverage Type is not defined yet — please pick it by hand.")
    else:
        print(f"  ⚠️  Unknown policy type '{policy_type}' — please pick Coverage Type by hand.")

    # Seating Capacity — the input whose id/name says 'seat'/'capacity'. NEVER a
    # blind label-following write (that is what put '5' into a random box before).
    # Poll: selecting Coverage above triggers a postback that briefly removes it.
    st = None if not seats else _ni_premium_controls(page, need=("seating",), quiet=True)["seating"]
    if not seats:
        print("  ⚠️  No seats value to put in Seating Capacity — please set it by hand.")
    elif st is None:
        print("  ⚠️  Could not find the Seating Capacity box — please set it by hand.")
    else:
        try:
            st.scroll_into_view_if_needed(timeout=10000)
            st.click(); st.press("Control+a"); st.press("Backspace"); st.type(str(seats), delay=20)
            print(f"  ✅  Filled Seating Capacity = {seats}")
            ni_settle(page)
        except Exception:
            print("  ⚠️  Could not fill Seating Capacity — please set it by hand.")

    # Add-on checkboxes — tick only what the Tameen add-ons text mentions. Normalize
    # (drop case/spaces/punctuation) so 'UAE COVER', 'U.A.E Extension', 'RSA' and
    # 'Road Side Assistance' all match. 'Orange Card' rides along with UAE — ignore it.
    if not addons:
        print("  ⚠️  No add-ons were read from Tameen — leaving U.A.E. Extension and "
              "Road Assistance UNticked. Please tick them by hand if needed.")
        return
    norm = re.sub(r"[^a-z0-9]", "", (addons or "").lower())
    want = {"uae": "uae" in norm,
            "road": ("rsa" in norm or "roadside" in norm or "roadassist" in norm)}

    # Tick ROAD FIRST, then U.A.E. — Road is reliable on a settled page, but ticking
    # U.A.E. fires a postback that briefly detaches Road (the intermittent miss). Doing
    # the fragile one before that postback removes the race; U.A.E. ticks fine either way.
    for key, label in (("road", "Road Assistance(ERA)"), ("uae", "U.A.E. Extension")):
        if want[key]:
            ni_set_addon(page, key, label)   # verifies the tick actually stuck


def ni_reset_to_motor_policy(page) -> None:
    """Send New India back to a fresh Motor Policy form for the next record."""
    print("\n── New India reset: opening a fresh Motor Policy form ──")
    try:
        ni_login_if_needed(page)
        ni_go_to_motor_policy(page)
    except Exception as e:
        print(f"  ⚠️  Could not reset New India ({e}) — please open Motor Policy by hand.")


# ==============================================================================
#  IRAN INSURANCE CO  —  CONFIG + HELPERS  (moved here from test.py so the UI
#  and production can import them exactly like MIC/NI)
# ==============================================================================
# ══════════════════════════════════════════════════════════════════════════════
#  IRAN INSURANCE CO  —  CONFIGURATION  (third insurer, runs alongside MIC + NI)
# ══════════════════════════════════════════════════════════════════════════════
#  The IRAN login email is HARDCODED here so every laptop uses the same account
#  after pulling (no per-machine .env needed for it). The password still comes from
#  .env (never committed) — add one line there:
#       IRAN_PASSWORD=your_iran_password_here
IRAN_USERNAME = "suad.alkalbani@tameen.om"
IRAN_PASSWORD = os.getenv("IRAN_PASSWORD", "")

IRAN_LOGIN_URL        = "https://ecrm-portal.com:92/"
IRAN_DASHBOARD_URL    = "https://ecrm-portal.com:92/User/Home/Dashboard"
IRAN_STEP_PAUSE       = 700          # ms wait after each action; raise if fields get skipped
IRAN_FIXED_MOBILE     = "99435202"   # always
IRAN_FIXED_EMAIL      = "suad.alkalbani@tameen.om"   # always — overwrites the site default
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


def iran_set_toggle_yes(page, label: str, visible_label: str = "UAE Cover") -> bool:
    """Set the bootstrap-toggle with id=`label` to Yes/on. Its real <input> is hidden,
    so a plain click can't reach it — we drive the plugin's own API,
    $('#id').bootstrapToggle('on'), which flips the UI and fires change."""
    try:
        result = page.evaluate(
            """(id) => {
                const el = document.getElementById(id);
                if (!el) return 'missing';
                if (el.checked) return 'already';
                const $ = window.jQuery;
                if ($ && $(el).bootstrapToggle) {
                    $(el).bootstrapToggle('on');
                } else {                              // plugin gone: click the wrapper
                    const w = el.closest('.toggle') || el.parentElement;
                    if (w) w.click();
                }
                return el.checked ? 'set' : 'failed';
            }""",
            label,
        )
    except Exception as e:
        print(f"  ⚠️  Could not set '{visible_label}' = Yes ({e}) — please toggle it by hand.")
        return False

    if result == "already":
        print(f"  ℹ️  '{visible_label}' is already Yes")
        return True
    if result == "set":
        print(f"  ✅  Set '{visible_label}' = Yes")
        iran_settle(page)
        return True
    if result == "missing":
        print(f"  ⚠️  No '{label}' toggle on the page — please set '{visible_label}' by hand.")
        return False
    print(f"  ⚠️  Could not set '{visible_label}' = Yes — please toggle it by hand.")
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


def _tameen_wait_for_doc_details(tameen_page, tries: int = 20) -> bool:
    """Poll until the Document Details section actually has document rows (a
    'View' link), scrolling it into view each pass to trigger any lazy load.

    On the Applications page the section loads AFTER the record shell, so an
    early read saw 'No document available' and gave up. Returns True once real
    rows show, False if it stays empty (record genuinely has no documents)."""
    for _ in range(tries):
        try:
            state = tameen_page.evaluate("""() => {
                const all = [...document.querySelectorAll('*')];
                const header = all.find(e => (e.innerText || '').trim() === 'Document Details');
                if (!header) return 'no-header';
                header.scrollIntoView({block: 'center'});
                const hy = header.getBoundingClientRect().top;
                let hasView = false, sawNone = false;
                for (const e of all) {
                    if (e.children.length !== 0) continue;
                    const y = e.getBoundingClientRect().top;
                    if (y <= hy || y > hy + 600) continue;
                    const t = (e.innerText || '').trim().toLowerCase();
                    if (t === 'view') hasView = true;
                    if (t.includes('no document')) sawNone = true;
                }
                return hasView ? 'ready' : (sawNone ? 'none' : 'waiting');
            }""")
        except Exception:
            state = "waiting"
        if state == "ready":
            return True
        if state == "none":
            return False        # section rendered its empty state — record has no docs
        tameen_page.wait_for_timeout(500)
    return False


def tameen_download_iran_documents(tameen_page, record_tag: str) -> dict:
    """Download the four documents IRAN needs (Civil ID front/back, Driving License
    front/back) from the open Tameen record's Document Details section. Returns a
    dict of local file paths (or None per item). Never crashes the record — flags."""
    print("\n── Tameen: downloading the customer's documents for IRAN ──")
    if not _tameen_wait_for_doc_details(tameen_page):
        print("  ⏳  Document Details did not fill in time (or record has none) — continuing.")
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


def _iran_debug_footer(page) -> None:
    """TEMP DIAGNOSTIC: after the uploads, report what happened to the Next
    button/footer. Writes to BOTH the console AND iran_footer_debug.txt (next to
    the app) so an employee can just open that file and send it — it only breaks
    on their machines, so we need the real DOM/viewport state from there."""
    lines = ["── IRAN DEBUG: footer/Next state after uploads ──",
             f"  when: {datetime.now():%Y-%m-%d %H:%M:%S}"]
    try:
        info = page.evaluate(r"""() => {
            const out = {nexts: [], footers: [], bodyH: document.body.scrollHeight,
                innerW: window.innerWidth, innerH: window.innerHeight,
                dpr: window.devicePixelRatio, zoom: Math.round((window.outerWidth/window.innerWidth)*100)/100};
            const all = Array.from(document.querySelectorAll('button,a,input,span,div'));
            for (const el of all) {
                const t = (el.innerText || el.value || '').trim();
                if (t === 'Next') {
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    out.nexts.push({
                        tag: el.tagName,
                        display: cs.display, visibility: cs.visibility, opacity: cs.opacity,
                        position: cs.position,
                        disabled: el.disabled === true || el.getAttribute('disabled') !== null,
                        w: Math.round(r.width), h: Math.round(r.height),
                        top: Math.round(r.top), bottom: Math.round(r.bottom),
                        offscreen: r.bottom > window.innerHeight || r.top > window.innerHeight
                    });
                }
            }
            for (const el of document.querySelectorAll('[class*="footer" i],[class*="btn" i],[class*="action" i]')) {
                const t = (el.innerText || '').trim();
                if (t.includes('Next') || t.includes('Previous')) {
                    const cs = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    out.footers.push({tag: el.tagName, cls: String(el.className).slice(0,80),
                        display: cs.display, visibility: cs.visibility, position: cs.position,
                        h: Math.round(r.height), top: Math.round(r.top), bottom: Math.round(r.bottom)});
                    if (out.footers.length >= 4) break;
                }
            }
            return out;
        }""")
        lines.append(f"  window: innerW={info.get('innerW')} innerH={info.get('innerH')} "
                     f"dpr={info.get('dpr')} zoom={info.get('zoom')}")
        lines.append(f"  body scrollHeight: {info.get('bodyH')}")
        nexts = info.get("nexts", [])
        lines.append(f"  'Next' elements in DOM: {len(nexts)}")
        for n in nexts:
            lines.append(f"    - {n}")
        for f in info.get("footers", []):
            lines.append(f"  footer-ish: {f}")
        if not nexts:
            lines.append("  → No 'Next' element in the DOM at all → React unmounted the footer.")
    except Exception as e:
        lines.append(f"  ⚠️  debug dump failed: {e}")
    lines.append("── end IRAN DEBUG ──")
    block = "\n".join(lines)
    print("\n" + block + "\n")
    try:
        dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "iran_footer_debug.txt")
        with open(dbg, "a", encoding="utf-8") as f:
            f.write(block + "\n\n")
    except Exception:
        pass


def _iran_wait_upload_committed(page, label: str, tries: int = 40) -> bool:
    """After set_input_files, block until the ECRM server round-trip finishes.
    Success signal: the text box next to `label` shows the server GUID (non-empty
    value), the '×' clear button appears, or a preview image renders. Falls back to
    network-idle. Returns True if committed, False on timeout (caller carries on).
    ponytail: polls at 300ms; ~12s ceiling covers a slow PC without hanging."""
    box = page.locator(
        f'xpath=//*[contains(normalize-space(.),"{label}")]'
        f'/following::input[@type="text" or not(@type)][1]'
    ).first
    for _ in range(tries):
        try:
            if (box.count() and (box.input_value() or "").strip()):
                return True
        except Exception:
            pass
        page.wait_for_timeout(300)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return False


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
                # Each attach is an async POST to the ECRM server; the box only
                # shows the server GUID once it lands. Wait for that (or network
                # idle) instead of a flat pause — otherwise a slow PC clicks Next
                # while an upload is still pending and the wizard hides the footer.
                if _iran_wait_upload_committed(iran_page, label):
                    print(f"  ✅  Attached '{label}' ← {path}")
                else:
                    print(f"  ✅  Attached '{label}' ← {path} (upload confirmation not seen; continuing)")
                done = True
                break
            except Exception:
                continue
        if not done:
            print(f"  ⚠️  Could not attach '{label}' automatically. The file is saved at:\n"
                  f"        {path}\n      → click that Browse box and pick it by hand.")


# ── IRAN flow helpers (one per stage of the form) ─────────────────────────────

def iran_login_if_needed(page, pause=None) -> None:
    """Log in to IRAN if the Sign In modal is showing; otherwise carry on. The
    image CAPTCHA is ALWAYS solved by the operator during a manual pause (same idea
    as the Tameen OTP). We pre-fill email/password if .env has them.

    `pause` lets a caller replace the terminal wait: the web UI passes a callback
    that shows a 'Continue' button and blocks until it's clicked. Default (None)
    keeps the input() pause used by the terminal scripts."""
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
    if pause is not None:
        pause()
    else:
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

    # Email — overwrite the site's default (mahfoodh@oneic.com.om) with our own.
    # Label spelling varies, so try the likely ones and keep the first that fills.
    if not any(iran_fill_by_label(page, lbl, IRAN_FIXED_EMAIL)
               for lbl in ("E-mail Address", "Email Address", "Email", "Email ID", "E-mail", "Insured Email")):
        print("  ⚠️  Could not find the Email field — please set it to "
              f"{IRAN_FIXED_EMAIL} by hand.")

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


def iran_fill_plan_details(page, addons, uae=False) -> None:
    """Tab 2 — PlanDetails: tick the IRAN plan checkboxes matching the Tameen
    add-ons (via IRAN_ADDON_MAP), then Select Plan → Next. When `uae` is True (UAE
    Cover was toggled Yes on Tab 1), an 'Orange Card Coverage' checkbox appears here
    and MUST be ticked, regardless of the Tameen add-ons."""
    print("\n── IRAN Tab 2: Plan Details (Choose Your Plan) ──")
    addons_l = (addons or "").lower()

    always_ticked = [label for label in IRAN_PLAN_ALWAYS_TICK if iran_tick_plan_addon(page, label)]
    if always_ticked:
        print(f"  ✅  Mandatory PAB checkboxes ticked: {', '.join(always_ticked)}")

    # UAE Cover = Yes reveals 'Orange Card Coverage' here, which must be ticked.
    if uae and iran_tick_plan_addon(page, "Orange Card Coverage"):
        print("  ✅  UAE Cover is Yes → ticked 'Orange Card Coverage'")

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
    # All uploads must be fully committed server-side before Next renders; a
    # lingering in-flight POST is what hides the footer on slower PCs.
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    _iran_debug_footer(page)   # TEMP: report why Next vanishes after uploads
    iran_click_button(page, "Next")
