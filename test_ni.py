"""
NEW INDIA TESTING script — practice runs against PAST records on Tameen's
APPLICATIONS page (instead of the normal Payments page).

Why this exists: to build confidence in the New India form-fill by running it
over lots of old records. Nothing is ever saved or submitted on New India —
the fill stops at the premium-calculation review, exactly like test.py.

How to run (from this folder):    python test_ni.py

This script is SEPARATE from the normal journey:
  - production.py / test.py / the control panel are untouched.
  - Do NOT run this at the same time as test.py or the control panel — they
    share the same browser profile (automation_profile) and Chromium only
    allows one window on a profile at a time.

Flow per record:
  Tameen dashboard → APPLICATIONS tile → New India rows listed → eye icon →
  read the record's fields → fill the New India form → you review on screen →
  ENTER here to reset both tabs and move to the next record ('q' quits).
"""
from playwright.sync_api import sync_playwright
from common import (
    read_field, parse_tameen_date, expiry_far_off, enable_download_dialogs,
    tameen_click_dashboard_tile,
)
# The New India helpers live in test.py; importing them does NOT start test.py's
# own browser flow (that part is behind its __main__ guard).
from test import (
    NI_LOGIN_URL,
    ni_login_if_needed, ni_go_to_motor_policy,
    ni_fill_primary_top, ni_fill_primary_client, ni_fill_previous_policy,
    ni_fill_vehicle_details, ni_fill_premium_calculation, ni_reset_to_motor_policy,
    reformat_plate_for_ni, compute_commencing_date_ni, read_tameen_addons,
)


# ══════════════════════════════════════════════════════════════════════════════
#  TAMEEN: the APPLICATIONS table
# ══════════════════════════════════════════════════════════════════════════════

# Same table-reading JavaScript as test.py's row picker — the Applications page
# uses the same kind of table as the Payments page (company column + eye icon).
_READ_ROWS_JS = """
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
"""

_CLICK_EYE_JS = """
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
"""


def _list_ni_rows(page):
    """Read the Applications table and return only the New India rows."""
    page.wait_for_timeout(1500)
    rows_data = page.evaluate(_READ_ROWS_JS)
    if not rows_data or not rows_data.get("rows"):
        raise RuntimeError("Could not read any table rows on the Applications page.")

    headers  = rows_data.get("headers", [])
    all_rows = rows_data["rows"]
    company_col_idx = next((i for i, h in enumerate(headers[1:], start=0) if "company" in h.lower()), None)

    ni_rows = []
    for r in all_rows:
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        if "new india" in cell_val.lower():
            ni_rows.append(r)
    return ni_rows, len(all_rows)


def tameen_pick_next_ni_record(page, done):
    """Show the New India rows (marking the ones already tested this run) and
    open the next untested one. ENTER = open it, a number = open that row
    instead, 'r' = re-read the table, 'q' = quit.

    Returns ("OK", "<row text>") after the record is opened, or ("QUIT", None).
    """
    while True:
        print("\n── Tameen: New India records on the Applications page ──")
        ni_rows, total = _list_ni_rows(page)
        if not ni_rows:
            print(f"  ⚠️  No New India rows found ({total} rows on the page).")
            if input("  Press ENTER to re-read the table, or 'q' to quit ▶  ").strip().lower() == "q":
                return "QUIT", None
            continue

        pending = [r for r in ni_rows if r["text"] not in done]
        next_row = pending[0] if pending else None

        print("\n" + "=" * 70)
        print(f"  NEW INDIA RECORDS  ({len(ni_rows)} of {total} rows — {len(done)} tested this run)")
        print("=" * 70)
        for i, r in enumerate(ni_rows, start=1):
            if r is next_row:
                mark = "→ next "
            elif r["text"] in done:
                mark = "✓ done "
            else:
                mark = "       "
            print(f"  [{i:>2}] {mark} {r['text'] or '(no text)'}")
        print("=" * 70)

        if next_row is None:
            print("  🎉  Every New India row on this page has been tested this run.")
            raw = input("  Type a number to re-test one, 'r' to re-read the table, or 'q' to quit ▶  ").strip().lower()
        else:
            raw = input("  ENTER = open the '→ next' row, or a number, 'r' = re-read, 'q' = quit ▶  ").strip().lower()

        if raw == "q":
            return "QUIT", None
        if raw == "r":
            continue
        if raw == "":
            selected = next_row
            if selected is None:
                continue
        else:
            try:
                choice = int(raw)
            except ValueError:
                print("  Please enter a number, ENTER, 'r' or 'q'.")
                continue
            if not 1 <= choice <= len(ni_rows):
                print("  Number out of range.")
                continue
            selected = ni_rows[choice - 1]

        print(f"\n  Opening: {selected['text'] or '(no text)'}")
        result = page.evaluate(_CLICK_EYE_JS, selected["domIdx"])
        if not result:
            raise RuntimeError("Could not click the eye icon on that row.")
        page.wait_for_load_state("domcontentloaded")
        print("  ✅  Opened record")
        return "OK", selected["text"]


def _on_applications_table(page) -> bool:
    """True when the Applications records table is showing: a table header that
    mentions 'company' plus at least one data row underneath it."""
    try:
        return page.evaluate("""() => {
            const heads = [...document.querySelectorAll('table thead th, table thead td, [role="columnheader"]')];
            if (!heads.some(h => (h.innerText || '').toLowerCase().includes('company'))) return false;
            return document.querySelectorAll('table tbody tr, [role="row"]').length > 0;
        }""")
    except Exception:
        return False


def tameen_reset_to_applications(page) -> None:
    """Send Tameen back to the Applications table (browser Back, like the
    Payments reset). If Back overshoots to the dashboard, click the tile again."""
    print("\n── Tameen reset: returning to the Applications page ──")
    for _ in range(4):
        if _on_applications_table(page):
            print("  ✅  Back on the Applications page")
            return
        try:
            page.go_back(wait_until="domcontentloaded")
        except Exception:
            break
        page.wait_for_timeout(800)
    if _on_applications_table(page):
        print("  ✅  Back on the Applications page")
        return
    try:
        tameen_click_dashboard_tile(page, "APPLICATIONS")
    except Exception:
        print("  ⚠️  Could not get back to the Applications page — navigate there by hand,")
        print("      then continue in this terminal.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW  (Tameen + New India tabs only)
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
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

        # Auto-accept native browser dialogs (e.g. beforeunload "Leave site?").
        tameen_page.on("dialog", lambda dialog: dialog.accept())
        ni_page.on("dialog", lambda dialog: dialog.accept())
        enable_download_dialogs(context)

        ni_page.goto(NI_LOGIN_URL, timeout=60000)
        tameen_page.bring_to_front()

        done = set()   # row texts already tested this run

        try:
            print("\n" + "=" * 60)
            print("⏸  ACTION REQUIRED")
            print("  1. Switch to the Tameen tab")
            print("  2. Log in and complete the OTP")
            print("  3. Come back here and press ENTER")
            print("=" * 60)
            input("\nPress ENTER once you are logged in to Tameen ▶  ")

            tameen_page.bring_to_front()
            print("\nAutomating Tameen navigation...")
            tameen_click_dashboard_tile(tameen_page, "APPLICATIONS")

            # ══════════════════════════════════════════════════════════════════
            #  PER-RECORD TESTING LOOP
            # ══════════════════════════════════════════════════════════════════
            while True:
                record_text = None
                prepared    = None

                try:
                    # read_field reads via the clipboard, which needs this tab in front.
                    tameen_page.bring_to_front()

                    status, record_text = tameen_pick_next_ni_record(tameen_page, done)
                    if status == "QUIT":
                        break
                    done.add(record_text)

                    # ── TAMEEN: read the record's fields (same as test.py) ──────
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

                    # Policy type: from Product Name; if that's blank on this record
                    # (like the Mobileapp channel), fall back to a Policy Type field.
                    type_source = product_name
                    if not type_source:
                        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
                            pt = read_field(tameen_page, lbl)
                            if pt:
                                type_source = pt
                                print(f"  → Product Name blank: using Policy Type '{pt}'")
                                break

                    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
                    if expiry_flagged:
                        print(f"\n⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

                    # ── extra fields New India needs ────────────────────────────
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
                    for lbl in ("Modal", "Model"):   # 'Modal' is Tameen's real (misspelled) label
                        tameen_model = read_field(tameen_page, lbl)
                        if tameen_model:
                            break
                    addons = read_tameen_addons(tameen_page)

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
                    print("📋  VALUES PREPARED FOR NEW INDIA (TEST RUN)")
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

                    ans = input("\nReview the result. Press ENTER to reset the tabs and "
                                "test the next record, or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                except Exception as e:
                    print("\n" + "=" * 60)
                    print("❌  THIS TEST RECORD HIT A PROBLEM")
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

                    ans = input("\nPress ENTER to reset the tabs and continue, "
                                "or type 'q' then ENTER to finish ▶  ")
                    if ans.strip().lower() == "q":
                        break

                # Reset both tabs for the next record.
                ni_reset_to_motor_policy(ni_page)
                tameen_reset_to_applications(tameen_page)

        except Exception as e:
            print("\n" + "=" * 60)
            print(f"❌  ERROR:\n{e}")
            print("=" * 60)

        finally:
            if done:
                print(f"\nTested this run ({len(done)} record{'s' if len(done) != 1 else ''}):")
                for t in done:
                    print(f"  • {t}")
            input("\nPress ENTER in this terminal to close the browser when you're done ▶  ")
            context.close()
            print("Browser closed.")
