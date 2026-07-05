"""NEW INDIA TESTING HARNESS — runs against PAST records on Tameen's APPLICATIONS page.

Purpose: batch-test the New India form fill with real historical data so we can
build confidence in it, WITHOUT touching the normal Payments journey (that stays
in test.py / production.py, untouched).

What it does, each round:
  1. Tameen → APPLICATIONS tile (instead of PAYMENTS — no channel step here)
  2. Lists ONLY the New India rows from the table, you pick one by number
  3. Opens it with the eye icon, reads the same fields the normal run reads
  4. Fills the New India form — STOPS before Premium Calculator, exactly like
     the normal run. NOTHING is ever saved or submitted.
  5. You review in the browser, come back, press ENTER → both tabs reset and
     the list is shown again for the next record.  Type 'q' to finish.

Usage:   venv\\Scripts\\python.exe test_ni.py
"""
from playwright.sync_api import sync_playwright
from common import read_field, parse_tameen_date, expiry_far_off

# test.py's main flow is behind `if __name__ == "__main__"`, so this import only
# loads the helper functions — it does NOT start the normal payments run.
from test import (
    NI_LOGIN_URL,
    ni_login_if_needed, ni_go_to_motor_policy,
    ni_fill_primary_top, ni_fill_primary_client, ni_fill_previous_policy,
    ni_fill_vehicle_details, ni_fill_premium_calculation, ni_reset_to_motor_policy,
    reformat_plate_for_ni, compute_commencing_date_ni, read_tameen_addons,
)


# ══════════════════════════════════════════════════════════════════════════════
#  TAMEEN: Applications page navigation
# ══════════════════════════════════════════════════════════════════════════════

def _tameen_wait_for_table(page, timeout=60000) -> None:
    """The Applications table appears after a short loading delay — wait for it."""
    page.wait_for_function(
        """() => document.querySelectorAll('table tbody tr').length > 0
              || document.querySelectorAll('[role="row"]').length > 1""",
        timeout=timeout,
    )
    page.wait_for_timeout(1500)   # let the rows finish rendering


def _tameen_table_showing(page) -> bool:
    try:
        return page.evaluate(
            """() => document.querySelectorAll('table tbody tr').length > 0
                  || document.querySelectorAll('[role="row"]').length > 1"""
        )
    except Exception:
        return False


def tameen_go_to_applications(page) -> None:
    """Click the APPLICATIONS tile on the Tameen dashboard, then wait for the
    records table to load. Same click strategy as the PAYMENTS tile in common.py."""
    print("\n── Tameen: Click APPLICATIONS tile ──")
    try:
        page.wait_for_function(
            """() => [...document.querySelectorAll('p, span, div, a, button')]
                .some(e => (e.innerText || '').trim().toUpperCase() === 'APPLICATIONS')""",
            timeout=60000,
        )
    except Exception:
        pass  # fall through — the fallbacks below will report if it's missing

    for sel in ['p:has-text("APPLICATIONS")', 'span:has-text("APPLICATIONS")',
                'div:has-text("APPLICATIONS")', 'a:has-text("APPLICATIONS")']:
        try:
            loc = page.locator(sel)
            if loc.count() == 0:
                continue
            el = loc.last
            el.scroll_into_view_if_needed(timeout=5000)
            el.click(timeout=8000)
            page.wait_for_load_state("domcontentloaded")
            _tameen_wait_for_table(page)
            print("  ✅  Applications page loaded")
            return
        except Exception:
            continue

    # JS fallback — click the smallest element whose text is exactly APPLICATIONS.
    result = page.evaluate("""() => {
        const all = [...document.querySelectorAll('*')];
        const is = e => (e.innerText || '').trim().toUpperCase() === 'APPLICATIONS';
        let t = all.find(e => e.children.length === 0 && is(e));
        if (!t) t = all.find(is);
        if (t) { t.scrollIntoView({block:'center'}); t.click(); return 'clicked'; }
        return 'not-found';
    }""")
    if result == "clicked":
        page.wait_for_load_state("domcontentloaded")
        _tameen_wait_for_table(page)
        print("  ✅  Applications page loaded (JS fallback)")
        return
    raise RuntimeError("Could not find the APPLICATIONS tile on the dashboard")


def tameen_reset_to_applications(page) -> None:
    """Send Tameen back to the Applications table using in-app Back navigation
    (no full reload, so the OTP session is preserved). Falls back to re-clicking
    the APPLICATIONS tile from the dashboard."""
    print("\n── Tameen reset: returning to the Applications page ──")
    for _ in range(4):
        if _tameen_table_showing(page):
            print("  ✅  Back on the Applications table")
            return
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            break
        page.wait_for_timeout(800)
    if _tameen_table_showing(page):
        print("  ✅  Back on the Applications table")
        return
    try:
        tameen_go_to_applications(page)
    except Exception:
        print("  ⚠️  Could not get back to Applications — please navigate by hand.")


def tameen_select_ni_and_click_eye(page):
    """List ONLY the New India rows of the Applications table and ask which one
    to open (same row-reading strategy as the normal payments flow).

    Returns ("OK", "<row text>") after opening a record,
    or      ("REFRESH", None)    if you typed 0 to re-read the table.
    """
    print("\n── Tameen: Select which New India record to open ──")
    page.wait_for_timeout(1500)

    rows_data = page.evaluate("""
        () => {
            const tableRows = document.querySelectorAll('table tbody tr');
            if (tableRows.length > 0) {
                const headers = Array.from(document.querySelectorAll('table thead th, table thead td'))
                    .map(h => h.innerText.trim().toLowerCase());
                const rows = Array.from(tableRows).map((row, domIdx) => {
                    const cells = Array.from(row.querySelectorAll('td')).slice(1);
                    return { text: cells.map(c => c.innerText.trim()).filter(Boolean).join('  |  '),
                             domIdx, cells: cells.map(c => c.innerText.trim()) };
                });
                return { headers, rows };
            }
            const headerRow = document.querySelector('[role="row"]:has([role="columnheader"])');
            const headers = headerRow ? Array.from(headerRow.querySelectorAll('[role="columnheader"]'))
                    .map(h => h.innerText.trim().toLowerCase()) : [];
            const allRows = Array.from(document.querySelectorAll('[role="row"], [class*="tr"]'))
                .filter(row => !row.querySelector('[role="columnheader"], th, [class*="th"]') && !row.closest('thead'));
            const rows = Array.from(allRows).map((row, domIdx) => {
                const cells = Array.from(row.querySelectorAll('[role="cell"], [class*="td"], td')).slice(1);
                return { text: cells.map(c => c.innerText.trim()).filter(Boolean).join('  |  '),
                         domIdx, cells: cells.map(c => c.innerText.trim()) };
            });
            return { headers, rows };
        }
    """)
    if not rows_data or not rows_data.get("rows"):
        raise RuntimeError("Could not read any table rows on the Applications page.")

    headers  = rows_data.get("headers", [])
    all_rows = rows_data["rows"]
    company_col_idx = next((i for i, h in enumerate(headers[1:], start=0) if "company" in h.lower()), None)

    def is_new_india(r):
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        return "new india" in cell_val.lower()

    filtered = [r for r in all_rows if is_new_india(r)]
    if not filtered:
        print(f"  ⚠️  No New India rows found on this page ({len(all_rows)} rows total).")
        print("      Showing ALL rows so you can check the company names:")
        filtered = all_rows

    print("\n" + "=" * 70)
    print(f"  NEW INDIA RECORDS ON THIS PAGE  ({len(filtered)} of {len(all_rows)} total rows)")
    print("=" * 70)
    for i, r in enumerate(filtered, start=1):
        print(f"  [{i:>2}]  {r['text'] or '(no text)'}")
    print("-" * 70)
    print("  [ 0]  🔄  Refresh (re-read the table — e.g. after changing page/filter by hand)")
    print("=" * 70)

    while True:
        raw = input(f"\nEnter row number to open (1–{len(filtered)}), or 0 to refresh: ").strip()
        try:
            choice = int(raw)
        except ValueError:
            print("  Please enter a valid number in range.")
            continue
        if choice == 0:
            return "REFRESH", None
        if 1 <= choice <= len(filtered):
            break
        print("  Please enter a valid number in range.")

    selected = filtered[choice - 1]
    idx      = selected["domIdx"]
    print(f"\n  Opening: {selected['text'] or '(no text)'}")

    result = page.evaluate("""
        (idx) => {
            const tableRows = document.querySelectorAll('table tbody tr');
            let row = null;
            if (tableRows.length > idx) row = tableRows[idx];
            else {
                const allRows = Array.from(document.querySelectorAll('[role="row"], [class*="tr"]'))
                    .filter(r => !r.querySelector('[role="columnheader"], th, [class*="th"]') && !r.closest('thead'));
                if (allRows.length > idx) row = allRows[idx];
            }
            if (!row) return null;
            const firstCell = row.querySelector('td, [role="cell"], [class*="td"]') || row.children[0];
            if (!firstCell) return null;
            (firstCell.querySelector('button') || firstCell.querySelector('[role="button"]') ||
             firstCell.querySelector('a') || firstCell.querySelector('svg') ||
             firstCell.querySelector('i') || firstCell).click();
            return 'clicked';
        }
    """, idx)
    if result:
        page.wait_for_load_state("domcontentloaded")
        print("  ✅  Opened record")
        return "OK", selected["text"]
    raise RuntimeError(f"Could not open row {choice}.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW  (Tameen + New India tabs only — MIC and IRAN are not opened)
# ══════════════════════════════════════════════════════════════════════════════
def main():
    with sync_playwright() as p:

        context = p.chromium.launch_persistent_context(
            user_data_dir="automation_profile",
            headless=False,
            slow_mo=450,
            locale="en-US",
            args=["--lang=en-US"],
            permissions=["clipboard-read", "clipboard-write"],
            ignore_https_errors=True,
        )

        print("Opening Tameen website...")
        tameen_page = context.pages[0] if context.pages else context.new_page()
        tameen_page.set_default_timeout(120000)
        tameen_page.goto("https://mis.tameen.om/dashboard/login", timeout=60000)

        print("Opening New India website...")
        ni_page = context.new_page()
        ni_page.set_default_timeout(120000)
        ni_page.goto(NI_LOGIN_URL, timeout=60000)

        # Auto-accept native browser dialogs (e.g. beforeunload) on both tabs.
        tameen_page.on("dialog", lambda dialog: dialog.accept())
        ni_page.on("dialog", lambda dialog: dialog.accept())

        tameen_page.bring_to_front()

        try:
            print("\n" + "=" * 60)
            print("⏸  ACTION REQUIRED")
            print("  1. Switch to the Tameen tab")
            print("  2. Log in and complete the OTP")
            print("  3. Come back here and press ENTER")
            print("=" * 60)
            input("\nPress ENTER once you are logged in to Tameen ▶  ")

            tameen_page.bring_to_front()
            tameen_go_to_applications(tameen_page)

            # ── PER-RECORD TEST LOOP ──────────────────────────────────────────
            while True:
                record_text = None
                prepared    = None

                try:
                    # read_field uses the clipboard, which needs this tab focused.
                    tameen_page.bring_to_front()

                    while True:
                        status, record_text = tameen_select_ni_and_click_eye(tameen_page)
                        if status != "REFRESH":
                            break
                        tameen_reset_to_applications(tameen_page)

                    # ── Read the record — same fields as the normal New India run ──
                    print("\nReading data from Tameen record...")
                    first_name   = read_field(tameen_page, "First Name")
                    last_name    = read_field(tameen_page, "Last Name")
                    license_id   = read_field(tameen_page, "License ID")
                    product_name = read_field(tameen_page, "Product Name")
                    prev_expiry  = read_field(tameen_page, "Previous Expiry")
                    vehicle_no   = read_field(tameen_page, "Vehicle Number")

                    seats_raw = ""
                    for seats_label in ("Seats", "No. of Seats", "No Of Seats",
                                        "Number of Seats", "Seating Capacity", "Seat Capacity"):
                        seats_raw = read_field(tameen_page, seats_label)
                        if seats_raw:
                            break
                    seats = "".join(ch for ch in seats_raw if ch.isdigit())

                    full_name = (first_name + " " + last_name).strip()

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
                    tameen_make = read_field(tameen_page, "Make")
                    tameen_model = ""
                    for lbl in ("Modal", "Model"):
                        tameen_model = read_field(tameen_page, lbl)
                        if tameen_model:
                            break
                    addons = read_tameen_addons(tameen_page)

                    # Policy type: Product Name first; if blank (some records keep it
                    # empty, like Mobileapp does on Payments), fall back to a Policy
                    # Type field, then to the words in the row itself.
                    type_source = product_name
                    if not type_source:
                        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
                            type_source = read_field(tameen_page, lbl)
                            if type_source:
                                break
                    if not type_source:
                        type_source = record_text or ""

                    pn = (type_source or "").lower().replace(" ", "")
                    if "thirdparty" in pn:
                        policy_type = "Third Party"
                    elif "comprehensive" in pn:
                        policy_type = "Comprehensive"
                    else:
                        policy_type = None
                        print(f"  ⚠️  Could not tell policy type from '{type_source}' — "
                              "Coverage Type will be left for you to pick.")

                    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
                    # Past records will usually trip this — it's informational here.
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
                    print("📋  VALUES PREPARED FOR NEW INDIA  (TEST RUN)")
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

                    ans = input("\nReview the result. Press ENTER to reset and test "
                                "another record, or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                except Exception as e:
                    print("\n" + "=" * 60)
                    print("❌  THIS TEST HIT A PROBLEM")
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
                    print("  → Note what went wrong (this is exactly what testing is for),")
                    print("    then continue to the next record.")
                    print("=" * 60)

                    ans = input("\nPress ENTER to reset and continue to the next record, "
                                "or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                # Reset both tabs for the next round.
                ni_reset_to_motor_policy(ni_page)
                tameen_reset_to_applications(tameen_page)

        except Exception as e:
            print("\n" + "=" * 60)
            print(f"❌  ERROR:\n{e}")
            print("=" * 60)

        finally:
            input("\nPress ENTER in this terminal to close the browser when you're done ▶  ")
            context.close()
            print("Browser closed.")


if __name__ == "__main__":
    main()
