"""
MIC-only automation: read a Tameen record, fill the Muscat Insurance (MIC) policy
form, leave it as Draft for review. Shared MIC/Tameen engine lives in common.py.

Credentials live in .env (never committed):
    MIC_USERNAME=...
    MIC_PASSWORD=...
"""
from playwright.sync_api import sync_playwright
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


def tameen_select_and_click_eye(page):
    """Step 4: list the Muscat Insurance rows and ask which to open.

    Returns a (status, record_text) tuple:
      ("BACK", None)        — user typed 0 (go back to the channel select)
      ("OK", "<row text>")  — the chosen record was opened; the text is kept so
                              the main flow can show it in the success / flagged
                              error summary.
    """
    print("\n── Tameen Step 4: Select which record to open (Muscat Insurance only) ──")
    page.wait_for_timeout(1500)
    COMPANY_FILTER = "muscat insurance"

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

    def is_muscat(r):
        if company_col_idx is not None:
            cell_val = r["cells"][company_col_idx] if company_col_idx < len(r["cells"]) else ""
        else:
            cell_val = r["text"]
        return COMPANY_FILTER in cell_val.lower()

    filtered = [r for r in all_rows if is_muscat(r)]
    if not filtered:
        print(f"  ⚠️  No rows matched '{COMPANY_FILTER}'. Showing all {len(all_rows)} rows.")
        filtered = all_rows

    print("\n" + "=" * 70)
    print(f"  MUSCAT INSURANCE RECORDS  ({len(filtered)} of {len(all_rows)} total rows)")
    print("=" * 70)
    for i, r in enumerate(filtered, start=1):
        print(f"  [{i:>2}]  {r['text'] or '(no text)'}")
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
            return "BACK", None
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
            return "OK", selected["text"]
    except Exception:
        pass
    raise RuntimeError(f"Could not open row {choice}.")


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

    # Restore a normal 'Save As' dialog for the employee's Print → Download step.
    enable_download_dialogs(context)

    mic_page.goto(MIC_HOME_URL, timeout=60000)

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
            prepared    = None     # the values prepared for MIC (filled in below)

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
                    status, record_text = tameen_select_and_click_eye(tameen_page)  # step 4
                    if status != "BACK":
                        break

                # ── TAMEEN: read all needed fields ────────────────────────────
                print("\nReading data from Tameen record...")
                first_name   = read_field(tameen_page, "First Name")
                last_name    = read_field(tameen_page, "Last Name")
                license_id   = read_field(tameen_page, "License ID")
                product_name = read_field(tameen_page, "Product Name")
                prev_expiry  = read_field(tameen_page, "Previous Expiry")
                # ⚠️ CONFIRM these three Tameen labels — I guessed them:
                vehicle_no   = read_field(tameen_page, "Vehicle Number")   # e.g. "B S-4788"
                sum_insured  = read_field(tameen_page, "Sum Insured")
                tameen_total = read_field(tameen_page, "Total Premium")

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

                # ── Derive the values MIC needs ───────────────────────────────
                full_name    = (first_name + " " + last_name).strip()
                parsed_expiry = parse_tameen_date(prev_expiry)
                period_from  = compute_period_from(parsed_expiry)
                expiry_flagged = expiry_far_off(parsed_expiry)
                plate_code, plate_number = split_plate(vehicle_no)
                if expiry_flagged:
                    print(f"\n⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

                # Policy type source. Normally read from Product Name, but the
                # Mobileapp channel leaves Product Name blank and has a dedicated
                # "Policy Type" field (Third Party / Comprehensive) instead. For that
                # channel only, use that field to decide the policy type.
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

                # Keep every prepared value together so BOTH the success summary
                # and the flagged-error summary can show the same details.
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

                # ── MIC: fill in the policy (left as Draft — not approved) ────
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

                # RESET ON DEMAND: nothing is touched until the employee says so.
                ans = input("\nReview the result. Press ENTER to reset both tabs and "
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

                ans = input("\nWhen you are done, press ENTER to reset both tabs and "
                            "continue to the next record, or type 'q' then ENTER to finish ▶  ")
                if ans.strip().lower() == "q":
                    break

            # Reached only when CONTINUING (after a success OR a flagged error):
            # send both tabs back to their starting points for the next record.
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
