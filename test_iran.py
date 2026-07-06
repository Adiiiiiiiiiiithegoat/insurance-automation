"""
TESTING script — practice runs against PAST records on Tameen's APPLICATIONS
page (instead of the normal Payments page), IRAN Insurance ONLY.

Mirrors test_ni.py (which does MIC + New India) but for the third insurer:
  • IRAN Insurance — filled to the Summary tab only (nothing submitted/issued).

Why this exists: to build confidence in the IRAN form-fill + document
download/upload by running it over old records ONE AT A TIME, reviewing each
result before moving on so any error can be fixed before the next.

How to run (from this folder):    python test_iran.py
  → Log into Tameen once (OTP), press ENTER, then it fills the NEXT IRAN record
    and STOPS. Review it on screen, then press ENTER to do the next one, or type
    'q' to finish. (IRAN's login has a manual CAPTCHA, so the first IRAN record
    also pauses once for that.)

This script is SEPARATE from the normal journey:
  - production.py / test.py / the control panel are untouched.
  - Do NOT run this at the same time as them — they share the browser profile
    (automation_profile) and Chromium allows one window per profile.

ponytail: reuses the IRAN form helpers from test.py and the Applications-tile
navigation from test_ni.py rather than duplicating either. Both are safe to
import (their run code is under `if __name__ == "__main__"`). If IRAN ever goes
to production.py, move the iran_* helpers into common.py (as MIC/NI already are)
and import from there instead of from test.
"""
from playwright.sync_api import sync_playwright

from common import (
    read_field, parse_tameen_date, expiry_far_off, enable_download_dialogs,
    compute_commencing_date_ni, read_tameen_addons, tameen_click_dashboard_tile,
)
# IRAN form-fill helpers + constants live in test.py.
from test import (
    IRAN_LOGIN_URL, IRAN_DASHBOARD_URL, IRAN_DOC_PRETTY,
    iran_login_if_needed, iran_go_to_motor_form, iran_fill_basic_info,
    iran_fill_plan_details, iran_fill_additional_details,
    tameen_download_iran_documents,
)
# Applications-tile navigation (company-agnostic) lives in test_ni.py.
from test_ni import (
    _READ_ROWS_JS, _CLICK_EYE_JS, _wait_for_applications_rows,
    tameen_reset_to_applications,
)


def _list_iran_records(page):
    """Read the Applications table; return only the IRAN rows (tagged
    r['company']='IRAN') and the total row count on the page."""
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
        if "iran" in (cell_val or "").lower():
            r["company"] = "IRAN"
            records.append(r)
    return records, len(all_rows)


def tameen_open_next_iran(page, done):
    """Auto-pick the next untested IRAN row and open it (no prompts).

    Returns ("OK", "<row text>") after opening the record, or ("DONE", None)
    when every IRAN row on the page has been tested."""
    records, total = _list_iran_records(page)
    pending = [r for r in records if r["text"] not in done]

    print("\n" + "=" * 70)
    print(f"  IRAN RECORDS  ({len(records)} IRAN of {total} rows — {len(done)} tested, {len(pending)} left)")
    print("=" * 70)

    if not pending:
        return "DONE", None

    selected = pending[0]
    print(f"  → Opening [IRAN]  {selected['text'] or '(no text)'}")
    result = page.evaluate(_CLICK_EYE_JS, selected["domIdx"])
    if not result:
        raise RuntimeError("Could not click the eye icon on that row.")
    page.wait_for_load_state("domcontentloaded")
    print("  ✅  Opened record")
    return "OK", selected["text"]


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN FLOW  (Tameen + IRAN tabs)
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

        print("Opening IRAN Insurance website...")
        iran_page = context.new_page()
        iran_page.set_default_timeout(120000)

        tameen_page.on("dialog", lambda dialog: dialog.accept())
        iran_page.on("dialog", lambda dialog: dialog.accept())
        enable_download_dialogs(context)

        iran_page.goto(IRAN_LOGIN_URL, timeout=60000)
        tameen_page.bring_to_front()

        done    = set()   # row texts already tested this run
        results = []      # (record_text, "PASS"/reason) for the final summary

        try:
            print("\n" + "=" * 60)
            print("⏸  ACTION REQUIRED (one time)")
            print("  1. Switch to the Tameen tab")
            print("  2. Log in and complete the OTP")
            print("  3. Come back here and press ENTER")
            print("     Then it fills ONE IRAN record and stops for you to review.")
            print("=" * 60)
            input("\nPress ENTER once you are logged in to Tameen ▶  ")

            tameen_page.bring_to_front()
            print("\nAutomating Tameen navigation...")
            tameen_click_dashboard_tile(tameen_page, "APPLICATIONS")

            # ══════════════════════════════════════════════════════════════════
            #  PER-RECORD LOOP — ONE record, then pause for review before the next
            # ══════════════════════════════════════════════════════════════════
            while True:
                record_text = None
                prepared    = None

                try:
                    # read_field reads via the clipboard, which needs this tab in front.
                    tameen_page.bring_to_front()

                    status, record_text = tameen_open_next_iran(tameen_page, done)
                    if status == "DONE":
                        print("\n🎉  Every IRAN row on this page has been tested.")
                        break
                    done.add(record_text)

                    print("\n" + "#" * 70)
                    print(f"#  TEST {len(done)}  —  [IRAN]  {record_text or '(no text)'}")
                    print("#" * 70)

                    # ── TAMEEN: read the fields IRAN needs ──────────────────────
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

                    # Policy type: from Product Name; if blank, try a Policy Type field.
                    type_source = product_name
                    if not type_source:
                        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
                            pt = read_field(tameen_page, lbl)
                            if pt:
                                type_source = pt
                                print(f"  → Product Name blank: using Policy Type '{pt}'")
                                break
                    pn = (type_source or "").lower().replace(" ", "")
                    if "thirdparty" in pn:
                        policy_type = "Third Party"
                    elif "comprehensive" in pn:
                        policy_type = "Comprehensive"
                    else:
                        policy_type = "Third Party"
                        print(f"  ⚠️  Could not tell policy type from '{type_source}' — "
                              "defaulting to Third Party.")

                    uae = "uae" in (addons or "").lower()
                    policy_start = compute_commencing_date_ni(prev_expiry)

                    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
                    if expiry_flagged:
                        print(f"\n⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

                    # Download the customer's documents (Tameen is in front).
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
                    print("\n📋  VALUES PREPARED FOR IRAN")
                    for label, value in prepared.items():
                        print(f"  {label:<16}: {value}")

                    # ── IRAN: fill the form (stops at Summary — no submit) ──
                    iran_page.bring_to_front()
                    iran_login_if_needed(iran_page)
                    iran_go_to_motor_form(iran_page, policy_type)
                    iran_fill_basic_info(iran_page, license_id, full_name, chassis, policy_type, uae)
                    iran_fill_plan_details(iran_page, addons)
                    iran_fill_additional_details(iran_page, policy_start, nationality, doc_paths)

                    results.append((record_text, "PASS"))
                    print("\n" + "=" * 60)
                    print(f"✅  TEST {len(done)} PASS  —  [IRAN]  {record_text or ''}")
                    print("=" * 60)

                except Exception as e:
                    results.append((record_text, f"FAIL: {e}"))
                    print("\n" + "=" * 60)
                    print(f"❌  TEST {len(done)} FAIL  —  [IRAN]  {record_text or ''}")
                    print("=" * 60)
                    print(f"  Reason: {e}")
                    if prepared:
                        print("  Details prepared so far:")
                        for label, value in prepared.items():
                            print(f"    {label:<16}: {value}")
                    else:
                        print("  (Failed before the record's details could be read.)")
                    print("=" * 60)

                # STOP and let the operator review this record before touching
                # anything. Nothing resets until they choose to continue.
                ans = input("\nReview the result on screen. Press ENTER to reset the tabs "
                            "and do the NEXT IRAN record, or type 'q' then ENTER to finish ▶  ")
                if ans.strip().lower() == "q":
                    break

                # Reset the IRAN tab + Tameen, then on to the next record.
                print("\n── IRAN reset: returning to the Dashboard ──")
                try:
                    iran_page.goto(IRAN_DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
                    print("  ✅  IRAN back on the Dashboard")
                except Exception as e:
                    print(f"  ⚠️  Could not reset IRAN ({e}) — open the Dashboard by hand.")
                tameen_reset_to_applications(tameen_page)

        except Exception as e:
            print("\n" + "=" * 60)
            print(f"❌  ERROR:\n{e}")
            print("=" * 60)

        finally:
            if results:
                passed = sum(1 for _, r in results if r == "PASS")
                print("\n" + "=" * 70)
                print(f"RUN SUMMARY — {passed}/{len(results)} passed")
                print("=" * 70)
                for txt, res in results:
                    mark = "✅" if res == "PASS" else "❌"
                    print(f"  {mark} [IRAN] {txt or '(no text)'}")
                    if res != "PASS":
                        print(f"       {res}")
                print("=" * 70)
            input("\nPress ENTER in this terminal to close the browser when you're done ▶  ")
            context.close()
            print("Browser closed.")
