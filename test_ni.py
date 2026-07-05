"""
TESTING script — practice runs against PAST records on Tameen's APPLICATIONS
page (instead of the normal Payments page). Tests BOTH insurers:
  • New India Assurance — filled to the premium-review only (nothing submitted).
  • Muscat Insurance (MIC) — filled to Calculate + premium check (left as Draft).

Why this exists: to build confidence in the form-fill by running it over lots of
old records and collecting the terminal output for diagnosis.

How to run (from this folder):    python test_ni.py
  → Log into Tameen once (OTP), press ENTER, then it runs UNATTENDED: it walks
    every New India + MIC row on the Applications page by itself, printing a
    PASS/FAIL block per record. Copy the whole terminal at the end for diagnosis.

This script is SEPARATE from the normal journey:
  - production.py / test.py / the control panel are untouched.
  - Do NOT run this at the same time as them — they share the browser profile
    (automation_profile) and Chromium allows one window per profile.

⚠️  MIC has no dry-run stop: testing a MIC row runs Create → … → Calculate, which
    leaves a real DRAFT policy on MIC (never confirmed/approved). New India creates
    nothing. ponytail: MIC's flow has no "stop before create", so a draft is the
    only way to exercise its fill.
"""
from playwright.sync_api import sync_playwright


class _SkipRecord(Exception):
    """Raised when a Tameen record is missing data essential to fill the New
    India form — skipping avoids feeding garbage into the form, which was
    crashing the browser (e.g. Vehicle Number '-' → invalid Reg.No '/')."""


from common import (
    MIC_HOME_URL, NI_LOGIN_URL,
    read_field, parse_tameen_date, expiry_far_off, enable_download_dialogs,
    tameen_click_dashboard_tile,
    compute_period_from, split_plate,
    mic_login_if_needed, mic_open_policy_create, mic_choose_policy_type_and_create,
    mic_get_licence, mic_fill_policy_info, mic_get_vehicle,
    mic_fill_vehicle_info, mic_calculate_and_check, mic_reset_to_home,
    reformat_plate_for_ni, compute_commencing_date_ni, read_tameen_addons,
    ni_login_if_needed, ni_go_to_motor_policy,
    ni_fill_primary_top, ni_fill_primary_client, ni_fill_previous_policy,
    ni_fill_vehicle_details, ni_fill_premium_calculation, ni_reset_to_motor_policy,
)


# ══════════════════════════════════════════════════════════════════════════════
#  TAMEEN: the APPLICATIONS table
# ══════════════════════════════════════════════════════════════════════════════

# Same table-reading JavaScript as production's row picker — the Applications page
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


def _wait_for_applications_rows(page, timeout_ms=20000) -> None:
    """Poll until the Applications table actually has data rows.

    The tile click only waits for 'domcontentloaded' (HTML shell ready); the
    table itself loads its rows afterwards via an async call. wait_for_function
    polls fast in-browser and returns the instant rows exist.
    """
    try:
        page.wait_for_function(
            """() => document.querySelectorAll('table tbody tr').length > 0 ||
                     [...document.querySelectorAll('[role="row"]')].some(
                         r => !r.querySelector('[role="columnheader"]') && !r.closest('thead'))""",
            timeout=timeout_ms,
        )
    except Exception:
        pass  # fall through — the caller raises its own clear error if still empty


def _company_of(cell_val: str):
    """'MIC' for a Muscat Insurance row, 'NEW_INDIA' for New India, else None."""
    cv = (cell_val or "").lower()
    if "muscat insurance" in cv:
        return "MIC"
    if "new india" in cv:
        return "NEW_INDIA"
    return None


def _list_records(page):
    """Read the Applications table; return the MIC + New India rows (each tagged
    with r['company']) and the total row count on the page."""
    _wait_for_applications_rows(page)
    rows_data = page.evaluate(_READ_ROWS_JS)
    if not rows_data or not rows_data.get("rows"):
        raise RuntimeError("Could not read any table rows on the Applications page.")

    headers  = rows_data.get("headers", [])
    all_rows = rows_data["rows"]
    company_col_idx = next((i for i, h in enumerate(headers[1:], start=0) if "company" in h.lower()), None)

    records = []
    for r in all_rows:
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        comp = _company_of(cell_val)
        if comp is not None:
            r["company"] = comp
            records.append(r)
    return records, len(all_rows)


def tameen_open_next_record(page, done):
    """Auto-pick the next untested MIC/New India row and open it (no prompts).

    Returns ("OK", "<row text>", "MIC"/"NEW_INDIA") after opening the record,
    or ("DONE", None, None) when every row on the page has been tested.
    """
    records, total = _list_records(page)
    pending = [r for r in records if r["text"] not in done]

    print("\n" + "=" * 70)
    print(f"  RECORDS  ({len(records)} MIC/New India of {total} rows — {len(done)} tested, {len(pending)} left)")
    print("=" * 70)

    if not pending:
        return "DONE", None, None

    selected = pending[0]
    tag = "[MIC]      " if selected["company"] == "MIC" else "[New India]"
    print(f"  → Opening {tag}  {selected['text'] or '(no text)'}")
    result = page.evaluate(_CLICK_EYE_JS, selected["domIdx"])
    if not result:
        raise RuntimeError("Could not click the eye icon on that row.")
    page.wait_for_load_state("domcontentloaded")
    print("  ✅  Opened record")
    return "OK", selected["text"], selected["company"]


def _on_applications_table(page) -> bool:
    """True when the Applications records table is showing (a 'company' header + rows)."""
    try:
        return page.evaluate("""() => {
            const heads = [...document.querySelectorAll('table thead th, table thead td, [role="columnheader"]')];
            if (!heads.some(h => (h.innerText || '').toLowerCase().includes('company'))) return false;
            return document.querySelectorAll('table tbody tr, [role="row"]').length > 0;
        }""")
    except Exception:
        return False


def _applications_ready(page) -> bool:
    """On the Applications table WITH its (async) rows loaded — wait, then check."""
    _wait_for_applications_rows(page)
    return _on_applications_table(page)


def tameen_reset_to_applications(page) -> None:
    """Send Tameen back to the Applications table for the next record.

    Tameen is a single-page app: the eye-icon opens the record via client-side
    routing, so browser Back re-renders the list and reloads its rows ASYNCHRONOUSLY.
    Go Back ONCE and actually wait for the rows before judging, then fall back to
    navigating by URL rather than blindly clicking Back again.
    """
    print("\n── Tameen reset: returning to the Applications page ──")

    try:
        page.go_back(wait_until="domcontentloaded")
    except Exception:
        pass
    if _applications_ready(page):
        print("  ✅  Back on the Applications page")
        return

    origin = "/".join(page.url.split("/")[:3])          # e.g. https://mis.tameen.om
    for url in (f"{origin}/dashboard/application", f"{origin}/dashboard"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            continue
        if _applications_ready(page):
            print("  ✅  Back on the Applications page")
            return

    try:
        tameen_click_dashboard_tile(page, "APPLICATIONS")
        if _applications_ready(page):
            print("  ✅  Back on the Applications page")
            return
    except Exception:
        pass
    print("  ⚠️  Could not get back to the Applications page — navigate there by hand.")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW  (Tameen + MIC + New India tabs)
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
            ignore_https_errors=True,          # MIC is on :444 with a custom cert
        )

        print("Opening Tameen website...")
        tameen_page = context.pages[0] if context.pages else context.new_page()
        tameen_page.set_default_timeout(120000)
        tameen_page.goto("https://mis.tameen.om/dashboard/login", timeout=60000)

        print("Opening MIC website...")
        mic_page = context.new_page()
        mic_page.set_default_timeout(120000)

        print("Opening New India website...")
        ni_page = context.new_page()
        ni_page.set_default_timeout(120000)

        # Auto-accept native browser dialogs (e.g. beforeunload "Leave site?").
        tameen_page.on("dialog", lambda dialog: dialog.accept())
        mic_page.on("dialog", lambda dialog: dialog.accept())
        ni_page.on("dialog", lambda dialog: dialog.accept())
        enable_download_dialogs(context)

        mic_page.goto(MIC_HOME_URL, timeout=60000)
        ni_page.goto(NI_LOGIN_URL, timeout=60000)
        tameen_page.bring_to_front()

        done    = set()   # row texts already tested this run
        results = []      # (company, record_text, "PASS"/reason) for the final summary

        try:
            print("\n" + "=" * 60)
            print("⏸  ACTION REQUIRED (one time)")
            print("  1. Switch to the Tameen tab")
            print("  2. Log in and complete the OTP")
            print("  3. Come back here and press ENTER — then it runs UNATTENDED")
            print("=" * 60)
            input("\nPress ENTER once you are logged in to Tameen ▶  ")

            tameen_page.bring_to_front()
            print("\nAutomating Tameen navigation...")
            tameen_click_dashboard_tile(tameen_page, "APPLICATIONS")

            # ══════════════════════════════════════════════════════════════════
            #  PER-RECORD TESTING LOOP — fully automatic, no prompts
            # ══════════════════════════════════════════════════════════════════
            while True:
                record_text = None
                prepared    = None
                company     = None

                try:
                    # read_field reads via the clipboard, which needs this tab in front.
                    tameen_page.bring_to_front()

                    status, record_text, company = tameen_open_next_record(tameen_page, done)
                    if status == "DONE":
                        print("\n🎉  Every MIC + New India row on this page has been tested.")
                        break
                    done.add(record_text)

                    print("\n" + "#" * 70)
                    print(f"#  TEST {len(done)}  —  [{company}]  {record_text or '(no text)'}")
                    print("#" * 70)

                    # ── TAMEEN: read the fields BOTH insurers need ──────────────
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

                    # Policy type: from Product Name; if blank (e.g. Mobileapp), fall
                    # back to a Policy Type field.
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

                    # ══════════════════════════════════════════════════════════
                    #  ROUTE TO THE RIGHT INSURER
                    # ══════════════════════════════════════════════════════════
                    if company == "MIC":
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
                        print("\n📋  VALUES PREPARED FOR MIC")
                        for label, value in prepared.items():
                            print(f"  {label:<14}: {value}")

                        # ── MIC: fill (left as Draft — not approved) ──
                        mic_page.bring_to_front()
                        mic_login_if_needed(mic_page)                                   # step 0
                        mic_open_policy_create(mic_page)                                # steps 1–2
                        is_comprehensive = mic_choose_policy_type_and_create(mic_page, type_source)  # steps 3–4
                        mic_get_licence(mic_page, license_id)                          # steps 5–6
                        mic_fill_policy_info(mic_page, full_name, period_from)         # steps 7–12
                        mic_get_vehicle(mic_page, plate_number, plate_code)           # steps 13–15
                        mic_fill_vehicle_info(mic_page, is_comprehensive, sum_insured, seats)  # steps 16–19
                        mic_calculate_and_check(mic_page, tameen_total)               # steps 20–22

                    else:  # company == "NEW_INDIA"
                        if not any(ch.isalnum() for ch in (vehicle_no or "")):
                            raise _SkipRecord(
                                f"no usable Vehicle Number on this Tameen record "
                                f"(read as '{vehicle_no}')")

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
                        print("\n📋  VALUES PREPARED FOR NEW INDIA")
                        for label, value in prepared.items():
                            print(f"  {label:<16}: {value}")

                        # ── NEW INDIA: fill (stops at review — no submit) ──
                        ni_page.bring_to_front()
                        ni_login_if_needed(ni_page)
                        ni_go_to_motor_policy(ni_page)
                        ni_fill_primary_top(ni_page, reg_no, license_id)
                        ni_fill_primary_client(ni_page, commencing_date, full_name)
                        brand, model, year = ni_fill_previous_policy(ni_page, mileage, color, full_name)
                        ni_fill_vehicle_details(ni_page, brand, model, body_type, seats,
                                                tameen_make, tameen_model)
                        ni_fill_premium_calculation(ni_page, policy_type, seats, addons)

                    results.append((company, record_text, "PASS"))
                    print("\n" + "=" * 60)
                    print(f"✅  TEST {len(done)} PASS  —  [{company}]  {record_text or ''}")
                    print("=" * 60)

                except _SkipRecord as e:
                    results.append((company or "?", record_text, f"SKIP: {e}"))
                    print("\n" + "=" * 60)
                    print(f"⏭️  TEST {len(done)} SKIP  —  [{company or '?'}]  {record_text or ''}")
                    print(f"  Reason: {e}")
                    print("=" * 60)

                except Exception as e:
                    results.append((company or "?", record_text, f"FAIL: {e}"))
                    print("\n" + "=" * 60)
                    print(f"❌  TEST {len(done)} FAIL  —  [{company or '?'}]  {record_text or ''}")
                    print("=" * 60)
                    print(f"  Reason: {e}")
                    if prepared:
                        print("  Details prepared so far:")
                        for label, value in prepared.items():
                            print(f"    {label:<14}: {value}")
                    else:
                        print("  (Failed before the record's details could be read.)")
                    print("=" * 60)

                # Reset the insurer tab we used + Tameen, then straight to the next.
                if company == "NEW_INDIA":
                    ni_reset_to_motor_policy(ni_page)
                elif company == "MIC":
                    mic_reset_to_home(mic_page)
                tameen_reset_to_applications(tameen_page)

        except Exception as e:
            print("\n" + "=" * 60)
            print(f"❌  ERROR:\n{e}")
            print("=" * 60)

        finally:
            if results:
                passed = sum(1 for *_, r in results if r == "PASS")
                print("\n" + "=" * 70)
                print(f"RUN SUMMARY — {passed}/{len(results)} passed")
                print("=" * 70)
                for comp, txt, res in results:
                    mark = "✅" if res == "PASS" else ("⏭️" if res.startswith("SKIP") else "❌")
                    print(f"  {mark} [{comp}] {txt or '(no text)'}")
                    if res != "PASS":
                        print(f"       {res}")
                print("=" * 70)
            input("\nPress ENTER in this terminal to close the browser when you're done ▶  ")
            context.close()
            print("Browser closed.")
