"""
Local web UI that wraps the EXISTING production.py flow so a non-technical
employee drives it by clicking instead of typing in a terminal.

Supports THREE insurers, chosen automatically from each Tameen record's company:
  • Muscat Insurance (MIC) — filled and left as Draft.
  • New India Assurance    — filled and left at the premium-review step.
  • Iran Insurance (IRAN)  — documents downloaded from Tameen + re-uploaded, form
                             filled to the Summary tab. First record pauses for a
                             manual CAPTCHA (Continue button on the progress page).

ARCHITECTURE (the important part):
  Playwright's SYNC api objects may only be touched by the thread that created
  them. Flask serves on many threads. So ONE dedicated worker thread owns the
  browser (persistent context + Tameen tab + MIC tab + New India tab) and is the
  ONLY thread that ever calls Playwright. Flask routes never touch Playwright —
  they talk to the worker through thread-safe queues:
    command_queue  (Flask  -> worker)  one action + args per item
    result_queue   (worker -> Flask)   request/response payloads (route blocks on get)
    progress_queue (worker -> browser) live MIC-fill events, streamed via SSE
  Because no request thread touches Playwright, Flask runs with threaded=True safely.

All browser logic reuses common.py helpers UNCHANGED. The two menu-driven Tameen
readers in production.py/common.py use input()/print(); we do NOT reuse those —
we copy only their table/tile-reading JavaScript into NEW data-returning versions
below (_read_channels / _click_channel / _read_records / _click_record). The MIC
helpers and reset/nav helpers take plain args and don't prompt, so we call them as-is.

The policy is left as DRAFT. There is no approve/save/submit step anywhere here.
"""
import json
import os
import queue
import sys
import threading
import time
import webbrowser

# Windows console defaults to cp1252, which chokes on the ✅/⚠️/🔑 emoji that
# common.py's print()s use. Force UTF-8 so those prints never crash the worker thread.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from flask import Flask, Response, render_template, redirect, url_for
from playwright.sync_api import sync_playwright

# Reuse the shared engine EXACTLY as production.py does (plus read_premium /
# PREMIUM_TOLERANCE so we can report the match outcome on screen instead of just
# printing it). Nothing in common.py is modified.
from common import (
    MIC_HOME_URL, NI_LOGIN_URL, PREMIUM_TOLERANCE,
    read_field, read_premium, parse_tameen_date, compute_period_from, split_plate,
    expiry_far_off,
    tameen_go_to_payments, tameen_click_payments_by_channel,
    tameen_reset_to_payments,
    mic_login_if_needed, mic_open_policy_create, mic_choose_policy_type_and_create,
    mic_get_licence, mic_fill_policy_info, mic_get_vehicle,
    mic_fill_vehicle_info, mic_calculate_and_check, mic_reset_to_home,
    reformat_plate_for_ni, compute_commencing_date_ni, read_tameen_addons,
    ni_login_if_needed, ni_go_to_motor_policy,
    ni_fill_primary_top, ni_fill_primary_client, ni_fill_previous_policy,
    ni_fill_vehicle_details, ni_fill_premium_calculation, ni_reset_to_motor_policy,
    TAMEEN_CHANNELS, TAMEEN_SECTIONS,
    # IRAN insurer (third insurer) — helpers + config live in common.py.
    IRAN_LOGIN_URL, IRAN_DASHBOARD_URL, IRAN_DOC_PRETTY,
    iran_login_if_needed, iran_go_to_motor_form, iran_fill_basic_info,
    iran_fill_plan_details, iran_fill_additional_details,
    tameen_download_iran_documents,
)

app = Flask(__name__)

command_queue: "queue.Queue[dict]" = queue.Queue()
result_queue: "queue.Queue[dict]" = queue.Queue()
progress_queue: "queue.Queue[dict]" = queue.Queue()

worker_ready = threading.Event()
worker_state = {"startup_error": None}

# Tameen dashboard root. A goto() here lands on the dashboard (PAYMENTS tile) from
# ANY page while keeping the logged-in session cookie — used to force a reset back
# to a known state when the gentle in-app reset can't reach the Payments page.
TAMEEN_DASHBOARD_URL = "https://mis.tameen.om/dashboard"

# What the live progress checklist shows, in order. The worker emits a step event
# (start/done) per index, matching the chosen insurer's list. Index 0 is the Tameen
# read; the rest are that insurer's fill helpers (one step per helper).
MIC_STEPS = [
    "Reading the record from Tameen",
    "Signing in to Muscat Insurance",
    "Opening the policy create form",
    "Choosing the policy type",
    "Fetching the licence record",
    "Filling the policy information",
    "Fetching the vehicle record",
    "Filling the vehicle information",
    "Calculating the premium & comparing",
]
NI_STEPS = [
    "Reading the record from Tameen",
    "Signing in to New India",
    "Opening the Motor Policy form",
    "Filling the primary details",
    "Filling the client details",
    "Filling the previous-policy details",
    "Filling the vehicle details",
    "Filling the premium calculation",
]
IRAN_STEPS = [
    "Reading the record + downloading documents from Tameen",
    "Signing in to IRAN (solve the CAPTCHA the first time)",
    "Opening the motor form",
    "Filling the basic information",
    "Filling the plan details",
    "Filling the additional details + uploading documents",
]

# Friendly insurer names, keyed by the company code carried on each record row.
_INSURER_LABELS = {"MIC": "Muscat Insurance", "NEW_INDIA": "New India",
                   "IRAN": "Iran Insurance"}

# Set by the /iran-continue route when the employee has solved the IRAN CAPTCHA.
# The worker's IRAN login step blocks on this instead of a terminal input().
iran_resume = threading.Event()

# Read-only copies of the last results, kept on the Flask side purely so screens
# can be re-rendered (e.g. "Go back" from the confirm screen) without touching the
# browser again. Never the source of truth for browser state.
ui_cache = {"channels": [], "records": {"headers": [], "rows": []},
            "channel_name": "", "prepared": {}, "record_text": ""}


# ─────────────────────────────────────────────────────────────────────────────
# NEW data-returning Tameen readers (JS copied from production.py / common.py,
# input()/print() menus removed). These run ON THE WORKER THREAD only.
# ─────────────────────────────────────────────────────────────────────────────
def _tameen_needs_login(page):
    """True if Tameen is still on the login/OTP screen (employee clicked 'I've
    logged in' too early). Fast check so we return a clear message instead of
    hanging ~60s on tiles that will never appear."""
    try:
        if "login" in (page.url or "").lower():
            return True
        pwd = page.locator('input[type="password"]')
        return pwd.count() > 0 and pwd.first.is_visible()
    except Exception:
        return False


def _read_channels(page):
    """Read the channel tiles from the 'Payments by Channel' page as DATA.
    Returns an ordered list of {channel, count, section}. (Tile-reading + menu
    ordering copied from common.tameen_select_channel; no prompting.)"""
    # Wait for the tiles to render.
    for _ in range(20):
        try:
            txt = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            txt = ""
        if "Count" in txt and "PAYMENT DONE" in txt.upper():
            break
        page.wait_for_timeout(500)
    else:
        page.wait_for_timeout(2000)

    data = page.evaluate("""
        (cfg) => {
            const { channels, sectionTitles } = cfg;
            const allEls = [...document.querySelectorAll('*')];
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
                const nameEls = allEls.filter(el => (el.innerText || "").trim().toLowerCase() === ch.toLowerCase());
                for (const nameEl of nameEls) {
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

    menu, included = [], set()
    for section in TAMEEN_SECTIONS:
        for ch in TAMEEN_CHANNELS:
            tile = next((t for t in tiles
                         if (t.get("section") or "").upper() == section.upper()
                         and t["channel"].lower() == ch.lower()), None)
            if tile and id(tile) not in included:
                menu.append(tile)
                included.add(id(tile))
    for t in tiles:
        if id(t) not in included:
            menu.append(t)
            included.add(id(t))
    return menu


def _click_channel(page, channel, section):
    """Click the tile for the given channel+section, then wait for the records
    table. (Click JS + table wait copied from common.tameen_select_channel.)"""
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
    """, {"channel": channel, "section": section})
    if result != "clicked":
        raise RuntimeError(f"Could not click the '{channel}' channel tile.")
    page.wait_for_load_state("domcontentloaded")

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
            table_loaded = True
            break
        except Exception:
            continue
    if not table_loaded:
        page.wait_for_timeout(4000)


def _read_records(page):
    """Read the rows we can process (Muscat Insurance + New India) as DATA. Returns
    row dicts with keys text/domIdx/cells/company. (Reading + company routing copied
    from production.tameen_select_and_click_eye; no prompting.)"""
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
        raise RuntimeError("Could not read any records in this channel.")

    headers = rows_data.get("headers", [])
    all_rows = rows_data["rows"]
    company_col_idx = next((i for i, h in enumerate(headers[1:], start=0) if "company" in h.lower()), None)

    def company_of(r):
        """'MIC' for Muscat Insurance, 'NEW_INDIA' for New India, 'IRAN' for Iran
        Insurance, else None."""
        if company_col_idx is not None and company_col_idx < len(r["cells"]):
            cell_val = r["cells"][company_col_idx]
        else:
            cell_val = r["text"]
        cv = cell_val.lower()
        if "muscat insurance" in cv:
            return "MIC"
        if "new india" in cv:
            return "NEW_INDIA"
        if "iran" in cv:
            return "IRAN"
        return None

    filtered = []
    for r in all_rows:
        comp = company_of(r)
        if comp is not None:
            r["company"] = comp
            filtered.append(r)
    if not filtered:
        # Nothing we recognise — show everything so the operator can see the rows.
        for r in all_rows:
            r["company"] = company_of(r)
        filtered = all_rows
    return {"headers": headers, "rows": filtered}


def _click_record(page, dom_idx):
    """Click the eye/open control on the row at the given DOM index.
    (Click-by-index JS copied from production.tameen_select_and_click_eye.)"""
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
    """, dom_idx)
    if not result:
        raise RuntimeError("Could not open that record.")
    page.wait_for_load_state("domcontentloaded")


def _read_tameen_fields(page, channel_name):
    """Read every Tameen field and derive the values MIC needs.
    (Reads + derivations copied from production.py's per-record block.)"""
    page.bring_to_front()
    first_name   = read_field(page, "First Name")
    last_name    = read_field(page, "Last Name")
    license_id   = read_field(page, "License ID")
    product_name = read_field(page, "Product Name")
    prev_expiry  = read_field(page, "Previous Expiry")
    vehicle_no   = read_field(page, "Vehicle Number")
    sum_insured  = read_field(page, "Sum Insured")
    tameen_total = read_field(page, "Total Premium")

    seats_raw = ""
    for seats_label in ("Seats", "No. of Seats", "No Of Seats",
                        "Number of Seats", "Seating Capacity", "Seat Capacity"):
        seats_raw = read_field(page, seats_label)
        if seats_raw:
            break
    seats = "".join(ch for ch in seats_raw if ch.isdigit())

    full_name   = (first_name + " " + last_name).strip()
    parsed_expiry = parse_tameen_date(prev_expiry)
    period_from = compute_period_from(parsed_expiry)
    expiry_flagged = expiry_far_off(parsed_expiry)
    plate_code, plate_number = split_plate(vehicle_no)
    if expiry_flagged:
        print(f"⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

    # Mobileapp channel leaves Product Name blank and uses a Policy Type field.
    type_source = product_name
    if (channel_name or "").lower() == "mobileapp":
        pt = ""
        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
            pt = read_field(page, lbl)
            if pt:
                break
        if pt:
            type_source = pt

    prepared = {
        "Product Name": product_name,
        "Insured Name": full_name,
        "License No": license_id,
        "Period From": f"{period_from}   (from expiry '{prev_expiry}')",
        **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
        "Plate": f"code='{plate_code}'  number='{plate_number}'  (from '{vehicle_no}')",
        "Seats": seats or "(not read — check the Tameen label)",
        "Sum Insured": sum_insured,
        "Tameen Premium": tameen_total,
    }
    fill = {
        "type_source": type_source, "license_id": license_id, "full_name": full_name,
        "period_from": period_from, "plate_code": plate_code, "plate_number": plate_number,
        "seats": seats, "sum_insured": sum_insured, "tameen_total": tameen_total,
    }
    return prepared, fill


def _read_tameen_fields_ni(page, channel_name):
    """Read every Tameen field and derive the values New India needs.
    (Reads + derivations copied from production.py's NEW_INDIA per-record block.)"""
    page.bring_to_front()
    first_name   = read_field(page, "First Name")
    last_name    = read_field(page, "Last Name")
    license_id   = read_field(page, "License ID")
    product_name = read_field(page, "Product Name")
    prev_expiry  = read_field(page, "Previous Expiry")
    vehicle_no   = read_field(page, "Vehicle Number")

    seats_raw = ""
    for seats_label in ("Seats", "No. of Seats", "No Of Seats",
                        "Number of Seats", "Seating Capacity", "Seat Capacity"):
        seats_raw = read_field(page, seats_label)
        if seats_raw:
            break
    seats = "".join(ch for ch in seats_raw if ch.isdigit())

    full_name = (first_name + " " + last_name).strip()
    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
    if expiry_flagged:
        print(f"⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

    # Policy type source. Mobileapp channel leaves Product Name blank and uses a
    # dedicated Policy Type field; fall back to it there (or if blank generally).
    type_source = product_name
    if not type_source or (channel_name or "").lower() == "mobileapp":
        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
            pt = read_field(page, lbl)
            if pt:
                type_source = pt
                break

    # Extra fields only New India needs. Several labels are guesses — try a few
    # spellings and keep the first that returns a value.
    mileage = ""
    for lbl in ("Mileage Est", "Mileage", "Mileage Estimate", "Odometer"):
        mileage = read_field(page, lbl)
        if mileage:
            break
    color = ""
    for lbl in ("Color", "Colour", "Vehicle Color", "Vehicle Colour"):
        color = read_field(page, lbl)
        if color:
            break
    body_type = ""
    for lbl in ("Body Type", "Body", "Vehicle Body Type", "Body Style"):
        body_type = read_field(page, lbl)
        if body_type:
            break
    tameen_make = read_field(page, "Make")
    tameen_model = ""
    for lbl in ("Modal", "Model"):   # 'Modal' is Tameen's real (misspelled) label
        tameen_model = read_field(page, lbl)
        if tameen_model:
            break
    addons = read_tameen_addons(page)

    pn = (type_source or "").lower().replace(" ", "")
    if "thirdparty" in pn:
        policy_type = "Third Party"
    elif "comprehensive" in pn:
        policy_type = "Comprehensive"
    else:
        policy_type = None

    reg_no          = reformat_plate_for_ni(vehicle_no)
    commencing_date = compute_commencing_date_ni(prev_expiry)

    prepared = {
        "Product/Type": f"{product_name}  →  {policy_type or '(unknown)'}",
        "Insured Name": full_name,
        "License/CivilID": license_id,
        "Reg.No": f"{reg_no}   (from '{vehicle_no}')",
        "Commencing": f"{commencing_date}   (from expiry '{prev_expiry}')",
        **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
        "Seats": seats or "(not read — check the Tameen label)",
        "Mileage": mileage or "(not read)",
        "Colour": color or "(not read)",
        "Body Type": body_type or "(not read)",
        "Add-ons": addons or "(none read)",
    }
    fill = {
        "policy_type": policy_type, "license_id": license_id, "full_name": full_name,
        "reg_no": reg_no, "commencing_date": commencing_date, "seats": seats,
        "mileage": mileage, "color": color, "body_type": body_type,
        "tameen_make": tameen_make, "tameen_model": tameen_model, "addons": addons,
    }
    return prepared, fill


def _read_tameen_fields_iran(page, channel_name):
    """Read every Tameen field IRAN needs AND download the customer's documents.
    (Reads + derivations copied from test_iran.py's per-record block.) Tameen must
    be the front tab here — read_field reads via the clipboard and the document
    download reads the open record's Document Details section."""
    page.bring_to_front()
    first_name   = read_field(page, "First Name")
    last_name    = read_field(page, "Last Name")
    license_id   = read_field(page, "License ID")
    product_name = read_field(page, "Product Name")
    prev_expiry  = read_field(page, "Previous Expiry")

    full_name = (first_name + " " + last_name).strip()

    # (No seats read — IRAN auto-fills seats from the chassis number.)
    chassis = ""
    for lbl in ("Chassis Number", "Chassis No", "Chassis"):
        chassis = read_field(page, lbl)
        if chassis:
            break
    nationality = ""
    for lbl in ("Nationality", "Nationality of Insured", "Insured Nationality"):
        nationality = read_field(page, lbl)
        if nationality:
            break
    addons = read_tameen_addons(page)

    # Policy type: from Product Name; if blank, try a Policy Type field; default TP.
    type_source = product_name
    if not type_source:
        for lbl in ("Policy Type", "Policy type", "Cover Type", "Coverage Type"):
            pt = read_field(page, lbl)
            if pt:
                type_source = pt
                break
    pn = (type_source or "").lower().replace(" ", "")
    if "thirdparty" in pn:
        policy_type = "Third Party"
    elif "comprehensive" in pn:
        policy_type = "Comprehensive"
    else:
        policy_type = "Third Party"

    uae = "uae" in (addons or "").lower()
    policy_start = compute_commencing_date_ni(prev_expiry)
    expiry_flagged = expiry_far_off(parse_tameen_date(prev_expiry))
    if expiry_flagged:
        print(f"⚠️  FLAG: policy expiry '{prev_expiry}' is more than a month away — renewing early.")

    # Download the customer's documents (Civil ID + Licence) while Tameen is in front.
    doc_paths = tameen_download_iran_documents(page, full_name or license_id or "record")
    got = [IRAN_DOC_PRETTY[k] for k, v in doc_paths.items() if v]

    prepared = {
        "Product/Type": f"{product_name}  →  {policy_type}",
        "Insured Name": full_name,
        "License/CivilID": license_id,
        "Chassis": chassis or "(not read — check the Tameen label)",
        "Nationality": nationality or "(not read)",
        "Policy Start": f"{policy_start}   (from expiry '{prev_expiry}')",
        **({"⚠ Expiry Flag": f"expiry '{prev_expiry}' is >1 month away — renewing early"} if expiry_flagged else {}),
        "UAE Cover": "Yes" if uae else "No",
        "Add-ons": addons or "(none read)",
        "Documents": (", ".join(got) if got else "(none downloaded — upload by hand on IRAN)"),
    }
    fill = {
        "license_id": license_id, "full_name": full_name, "chassis": chassis,
        "policy_type": policy_type, "uae": uae, "addons": addons,
        "policy_start": policy_start, "nationality": nationality, "doc_paths": doc_paths,
    }
    return prepared, fill


def _compare_premium(net_prem, tameen_total):
    """Same numeric comparison as common.mic_calculate_and_check, but RETURNS the
    outcome so the UI can show it (the helper only prints it)."""
    def num(s):
        return float("".join(ch for ch in str(s) if ch.isdigit() or ch in ".-"))
    try:
        diff = abs(num(net_prem) - num(tameen_total))
        return {"compared": True, "match": diff <= PREMIUM_TOLERANCE,
                "mic": net_prem, "tameen": tameen_total, "diff": round(diff, 2),
                "tolerance": PREMIUM_TOLERANCE}
    except (ValueError, TypeError):
        return {"compared": False, "mic": net_prem, "tameen": tameen_total}


def _fill_new_india(ni_page, fill, prepared):
    """Fill the New India form (steps 1..7), streaming a step event per helper.
    Stops at the premium-review — nothing is saved/submitted. Emits a 'final' event
    tagged insurer=NEW_INDIA (no premium comparison — New India has none here)."""
    ni_page.bring_to_front()
    state = {}
    steps = [
        ("NI login check",   lambda: ni_login_if_needed(ni_page)),
        ("Open Motor Policy", lambda: ni_go_to_motor_policy(ni_page)),
        ("Primary details",  lambda: ni_fill_primary_top(ni_page, fill["reg_no"], fill["license_id"])),
        ("Client details",   lambda: ni_fill_primary_client(ni_page, fill["commencing_date"], fill["full_name"])),
        ("Previous policy",  lambda: state.__setitem__(
            "bmy", ni_fill_previous_policy(ni_page, fill["mileage"], fill["color"], fill["full_name"]))),
        ("Vehicle details",  lambda: ni_fill_vehicle_details(
            ni_page, state["bmy"][0], state["bmy"][1], fill["body_type"], fill["seats"],
            fill["tameen_make"], fill["tameen_model"])),
        ("Premium calc",     lambda: ni_fill_premium_calculation(
            ni_page, fill["policy_type"], fill["seats"], fill["addons"])),
    ]
    for i, (name, step) in enumerate(steps, start=1):
        progress_queue.put({"type": "step", "index": i, "state": "start"})
        t0 = time.time()
        try:
            step()
        except Exception as e:
            progress_queue.put({"type": "error", "index": i, "message": str(e),
                                "record": fill.get("record_text", "")})
            return
        print(f"  ⏱  [TIMING] NI step {i} ({name}): {time.time() - t0:.1f}s")
        progress_queue.put({"type": "step", "index": i, "state": "done"})

    progress_queue.put({"type": "final", "insurer": "NEW_INDIA",
                        "record": fill.get("record_text", ""), "prepared": prepared})


def _iran_captcha_pause():
    """Called by iran_login_if_needed (worker thread) when the Sign In modal is up.
    Tell the browser to show a 'Continue' button, then BLOCK until the employee
    solves the CAPTCHA in the IRAN tab and clicks it (the /iran-continue route sets
    iran_resume). Fires only on the first IRAN record — later ones are already
    logged in and skip this entirely."""
    iran_resume.clear()
    progress_queue.put({"type": "captcha"})
    # Bounded so an abandoned progress page can't wedge the worker forever. If it
    # times out unlogged-in, the next IRAN step fails cleanly (flagged for review).
    if not iran_resume.wait(timeout=900):   # 15 min
        print("  ⚠️  IRAN CAPTCHA wait timed out — continuing (login may be incomplete).")


def _fill_iran(iran_page, fill, prepared):
    """Fill the IRAN form (login → additional details), streaming a step event per
    helper. Stops at the Summary tab — nothing is submitted/issued. Emits a 'final'
    event tagged insurer=IRAN (no premium comparison — IRAN has none here)."""
    iran_page.bring_to_front()
    steps = [
        ("IRAN login",       lambda: iran_login_if_needed(iran_page, pause=_iran_captcha_pause)),
        ("Open motor form",  lambda: iran_go_to_motor_form(iran_page, fill["policy_type"])),
        ("Basic info",       lambda: iran_fill_basic_info(
            iran_page, fill["license_id"], fill["full_name"], fill["chassis"],
            fill["policy_type"], fill["uae"])),
        ("Plan details",     lambda: iran_fill_plan_details(iran_page, fill["addons"], uae=fill["uae"])),
        ("Additional details", lambda: iran_fill_additional_details(
            iran_page, fill["policy_start"], fill["nationality"], fill["doc_paths"])),
    ]
    for i, (name, step) in enumerate(steps, start=1):
        progress_queue.put({"type": "step", "index": i, "state": "start"})
        t0 = time.time()
        try:
            step()
        except Exception as e:
            progress_queue.put({"type": "error", "index": i, "message": str(e),
                                "record": fill.get("record_text", "")})
            return
        print(f"  ⏱  [TIMING] IRAN step {i} ({name}): {time.time() - t0:.1f}s")
        progress_queue.put({"type": "step", "index": i, "state": "done"})

    progress_queue.put({"type": "final", "insurer": "IRAN",
                        "record": fill.get("record_text", ""), "prepared": prepared})


def _process_record(tameen_page, mic_page, ni_page, iran_page, idx, st):
    """One continuous run, started AFTER the employee confirms the row:
      step 0  — open the row in Tameen and read every field (no clicks needed)
      then the chosen insurer's fill steps, one helper per step,
      then a final event with the prepared details (+ premium check for MIC).
    Any failure becomes an error event (the 'flagged for review' path); the worker
    never crashes."""
    rec = st["records"][idx]
    company = rec.get("company") or "MIC"
    st["last_company"] = company     # so a later 'reset' knows which tab to reset

    # ── Step 0: read the record from Tameen ──────────────────────────────────
    run_t0 = time.time()
    progress_queue.put({"type": "step", "index": 0, "state": "start"})
    t0 = time.time()
    try:
        tameen_page.bring_to_front()
        # Match the confirmed row by its CONTENT, not the cached DOM position:
        # the employee may have sorted / paged / reopened the Tameen table since
        # they picked it, which would leave the old index pointing at a DIFFERENT
        # customer. Re-read now and click the row whose cells still match. If none
        # matches, stop and ask them to reselect — never fall back to an index
        # (filling the wrong customer's policy is the one thing we must not do).
        try:
            fresh = _read_records(tameen_page)["rows"]
        except Exception:
            fresh = []
        match = next((r for r in fresh if r["cells"] == rec["cells"]), None)
        if match is None:
            progress_queue.put({"type": "error", "index": 0,
                "message": ("The Tameen record list changed since you picked this "
                            "row, so it can no longer be matched safely. Go back "
                            "and select the record again."),
                "record": rec["text"]})
            return
        _click_record(tameen_page, match["domIdx"])
        if company == "NEW_INDIA":
            prepared, fill = _read_tameen_fields_ni(tameen_page, st["channel_name"])
        elif company == "IRAN":
            prepared, fill = _read_tameen_fields_iran(tameen_page, st["channel_name"])
        else:
            prepared, fill = _read_tameen_fields(tameen_page, st["channel_name"])
        fill["record_text"] = rec["text"]
    except Exception as e:
        progress_queue.put({"type": "error", "index": 0, "message": str(e), "record": ""})
        return
    print(f"  ⏱  [TIMING] Step 0 (read Tameen record): {time.time() - t0:.1f}s")
    progress_queue.put({"type": "step", "index": 0, "state": "done"})

    if company == "NEW_INDIA":
        _fill_new_india(ni_page, fill, prepared)
        return

    if company == "IRAN":
        _fill_iran(iran_page, fill, prepared)
        return

    # ── Steps 1..8: fill MIC (one helper per step, before/after events) ──────
    mic_page.bring_to_front()
    state = {}
    steps = [
        ("MIC login check", lambda: mic_login_if_needed(mic_page)),
        ("Open policy create", lambda: mic_open_policy_create(mic_page)),
        ("Choose type + Create", lambda: state.__setitem__("is_comp", mic_choose_policy_type_and_create(mic_page, fill["type_source"]))),
        ("Get licence", lambda: mic_get_licence(mic_page, fill["license_id"])),
        ("Fill policy info", lambda: mic_fill_policy_info(mic_page, fill["full_name"], fill["period_from"])),
        ("Get vehicle", lambda: mic_get_vehicle(mic_page, fill["plate_number"], fill["plate_code"])),
        ("Fill vehicle info", lambda: mic_fill_vehicle_info(mic_page, state.get("is_comp", False), fill["sum_insured"], fill["seats"])),
        ("Calculate + check", lambda: mic_calculate_and_check(mic_page, fill["tameen_total"])),
    ]
    # ponytail: timing prints are for the current speed investigation only, safe to
    # delete once we've found the real bottleneck.
    for i, (name, step) in enumerate(steps, start=1):
        progress_queue.put({"type": "step", "index": i, "state": "start"})
        t0 = time.time()
        try:
            step()
        except Exception as e:
            progress_queue.put({"type": "error", "index": i, "message": str(e),
                                "record": fill.get("record_text", "")})
            return
        print(f"  ⏱  [TIMING] Step {i} ({name}): {time.time() - t0:.1f}s")
        progress_queue.put({"type": "step", "index": i, "state": "done"})
    print(f"  ⏱  [TIMING] TOTAL record time: {time.time() - run_t0:.1f}s")

    # Re-read the premium so we can report the match/mismatch on screen. (Policy is
    # left as DRAFT — no approve/save step.)
    net_prem = read_premium(mic_page, "Net Prem Incl. VAT")
    outcome = _compare_premium(net_prem, fill["tameen_total"])
    progress_queue.put({"type": "final", "insurer": "MIC",
                        "record": fill.get("record_text", ""),
                        "prepared": prepared, **outcome})


def _skip_debugger_pauses(page):
    """Some Tameen pages (and the License / Mulkiya document viewers that open in a
    new tab) ship anti-debugging `debugger;` statements. In a normal browser those
    are no-ops, but because Playwright drives Chromium over the DevTools Protocol,
    V8 actually PAUSES on them — freezing the tab with a 'Paused in debugger'
    overlay. We attach a CDP session and tell V8 to skip ALL pauses, so those tabs
    stay fully usable. Applied to every tab via the context 'page' event, so the
    document tabs are defused the instant they open."""
    try:
        client = page.context.new_cdp_session(page)
        client.send("Debugger.enable")
        client.send("Debugger.setSkipAllPauses", {"skip": True})
        try:
            client.send("Debugger.resume")   # in case it already paused before we attached
        except Exception:
            pass
    except Exception:
        pass


def _close_extra_tabs(context, keep):
    """Close every tab except the working ones in `keep` (the Tameen + MIC tabs).
    Issuing a policy leaves clutter open — License/Mulkiya document tabs (target=
    _blank) and the Print → PDF viewer tab — so reset wipes them out."""
    for pg in list(context.pages):
        if pg not in keep:
            try:
                pg.close()
            except Exception:
                pass


def _autosave_download(download):
    """Save a Print → Download PDF straight to the Downloads folder, no dialog.
    Runs on the worker thread but returns immediately (just a file write), so the
    worker never blocks the way the old tkinter 'Save As' dialog did. A unique name
    is chosen so a second policy never overwrites the first."""
    try:
        name = download.suggested_filename or "MIC_Policy.pdf"
        if not name.lower().endswith(".pdf"):
            name = "MIC_Policy.pdf"
        folder = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(folder, exist_ok=True)
        stem, ext = os.path.splitext(name)
        target, n = os.path.join(folder, name), 1
        while os.path.exists(target):
            target = os.path.join(folder, f"{stem} ({n}){ext}")
            n += 1
        download.save_as(target)
        print(f"  ✅  Saved policy PDF to: {target}")
    except Exception as e:
        print(f"  ⚠️  Could not save the download automatically: {e}")


def _wire_downloads(context):
    """Attach the auto-save handler to every current and future tab, so a download
    from any tab (incl. the PDF viewer the Print opens) is saved without a dialog."""
    for pg in context.pages:
        pg.on("download", _autosave_download)
    context.on("page", lambda pg: pg.on("download", _autosave_download))


# Home URL each working tab is recreated at if the employee closes it by hand.
# (Tameen goes to the dashboard, not the login page — the persistent-context
# cookie keeps it logged in.)
_TAB_HOME = {
    "tameen": TAMEEN_DASHBOARD_URL,
    "mic": MIC_HOME_URL,
    "ni": NI_LOGIN_URL,
    "iran": IRAN_LOGIN_URL,
}


def _live(context, pages, key):
    """Return a live page for `key`, recreating the tab if the employee closed it.
    New pages inherit the skip-debugger + download handlers from the context
    'page' event, so we only re-attach the per-page dialog handler here. A fresh
    tab is logged out, so the next step just re-hits that insurer's
    login-if-needed helper — the same login already done at startup."""
    pg = pages.get(key)
    if pg is not None and not pg.is_closed():
        return pg
    pg = context.new_page()
    pg.set_default_timeout(120000)
    if key == "ni":
        pg.on("dialog", lambda d: (print(f"  🔔  New India dialog: {d.message}") if d.message else None, d.accept()))
    else:
        pg.on("dialog", lambda d: d.accept())
    try:
        pg.goto(_TAB_HOME[key], timeout=60000)
    except Exception:
        pass
    pages[key] = pg
    return pg


# ─────────────────────────────────────────────────────────────────────────────
# THE WORKER THREAD — owns ALL Playwright objects.
# ─────────────────────────────────────────────────────────────────────────────
def worker_main():
    try:
        with sync_playwright() as p:
            # Same launch settings as production.py (same automation_profile so the
            # saved login persists, non-headless, slow_mo, clipboard, ignore certs).
            context = p.chromium.launch_persistent_context(
                user_data_dir="automation_profile",
                headless=False,
                slow_mo=450,
                locale="en-US",
                # Maximize + let the page fill the whole window. On low-res / high-DPI
                # laptops a non-maximized window has so little height that IRAN's
                # bottom Next/Previous bar falls off-screen after the tall upload
                # previews render. no_viewport makes the CSS viewport = window size.
                # --force-device-scale-factor=1 overrides the laptop's Windows display
                # scaling. Native 150% (dpr 1.5) left only ~720px height and shoved
                # IRAN's Next bar off-screen; 1.0 gives the full physical height so the
                # footer is reachable. (1.25 was tried for a bigger UI but the footer
                # went off-screen again, so we stay at 1.0 — the value that works.)
                args=["--lang=en-US", "--start-maximized", "--window-size=1920,1080",
                      "--force-device-scale-factor=1", "--high-dpi-support=1"],
                no_viewport=True,
                permissions=["clipboard-read", "clipboard-write"],
                ignore_https_errors=True,
            )
            # Defuse anti-debugger pauses on EVERY tab, including the License /
            # Mulkiya document tabs that open via target=_blank links. Registered
            # before any new_page so the two main tabs are covered too.
            context.on("page", _skip_debugger_pauses)

            tameen_page = context.pages[0] if context.pages else context.new_page()
            tameen_page.set_default_timeout(120000)
            _skip_debugger_pauses(tameen_page)
            tameen_page.goto("https://mis.tameen.om/dashboard/login", timeout=60000)

            mic_page = context.new_page()
            mic_page.set_default_timeout(120000)
            _skip_debugger_pauses(mic_page)

            ni_page = context.new_page()
            ni_page.set_default_timeout(120000)
            _skip_debugger_pauses(ni_page)

            iran_page = context.new_page()
            iran_page.set_default_timeout(120000)
            _skip_debugger_pauses(iran_page)

            # Native-dialog auto-accept on every working tab (same as production.py).
            mic_page.on("dialog", lambda d: d.accept())
            # Log New India's Save alert before accepting: SaveClick() pops a native
            # alert (missing required field) and returns false, so no postback fires
            # and the button looks dead. Auto-accepting swallowed the reason silently.
            ni_page.on("dialog", lambda d: (print(f"  🔔  New India dialog: {d.message}") if d.message else None, d.accept()))
            iran_page.on("dialog", lambda d: d.accept())
            tameen_page.on("dialog", lambda d: d.accept())
            # Auto-save Print → Download PDFs to the Downloads folder (no dialog, so
            # the single browser worker never blocks waiting on a Save As window).
            # (IRAN's document download uses context.route interception, not download
            # events, so it needs no wiring here.)
            _wire_downloads(context)
            mic_page.goto(MIC_HOME_URL, timeout=60000)
            ni_page.goto(NI_LOGIN_URL, timeout=60000)
            iran_page.goto(IRAN_LOGIN_URL, timeout=60000)

            # Show the Tameen tab when the browser opens — that's the one the
            # employee needs for the login/OTP. (the insurer tabs were navigated
            # last, so without this one of them would be in front.)
            tameen_page.bring_to_front()

            # Handles for the four working tabs, so _live() can recreate any the
            # employee closes by hand instead of letting a dead handle crash a run.
            pages = {"tameen": tameen_page, "mic": mic_page,
                     "ni": ni_page, "iran": iran_page}

            st = {"channel_name": "", "fill": {}}
            worker_ready.set()

            while True:
                try:
                    cmd = command_queue.get(timeout=0.3)
                except queue.Empty:
                    # Idle between clicks. Pump Playwright's event loop briefly so the
                    # context 'page' handler fires for any document tab the employee
                    # just opened by hand (License / Mulkiya), defusing its
                    # anti-debugger pause instead of letting the tab freeze.
                    try:
                        tameen_page.wait_for_timeout(50)
                    except Exception:
                        pass
                    continue
                action, args = cmd["action"], cmd.get("args", {})

                if action == "shutdown":
                    break

                # Recreate any tab the employee closed by hand, so a dead handle
                # never crashes the run. Cheap when the tabs are open (just an
                # is_closed() check); reassigns the locals used below + passed to
                # _process_record.
                tameen_page = _live(context, pages, "tameen")
                mic_page = _live(context, pages, "mic")
                ni_page = _live(context, pages, "ni")
                iran_page = _live(context, pages, "iran")

                if action == "process_record":
                    # No result_queue reply — the page watches the SSE stream while
                    # this reads Tameen and fills the chosen insurer in one run.
                    _process_record(tameen_page, mic_page, ni_page, iran_page,
                                    args["index"], st)
                    continue

                try:
                    if action == "login_done":
                        tameen_page.bring_to_front()
                        # Employees often reach the payments page BY HAND before
                        # clicking Continue, so we can't assume we're on the
                        # dashboard. goto() the dashboard URL works from ANY page and
                        # keeps the login cookie — a known start every time.
                        # RELIABILITY: the #1 "button just keeps loading" cause is
                        # clicking BEFORE the Tameen login/OTP is finished — the tiles
                        # never appear so the tile-wait sits for ~60s twice. Detect
                        # that instantly and tell them, and retry the dashboard load
                        # once (it often needs a reload right after OTP).
                        done = False
                        last_err = None
                        for _ in range(2):
                            try:
                                tameen_page.goto(TAMEEN_DASHBOARD_URL,
                                                 wait_until="domcontentloaded", timeout=30000)
                                if _tameen_needs_login(tameen_page):
                                    result_queue.put({"ok": False, "error":
                                        "Tameen still shows the login / OTP screen. Finish "
                                        "signing in in the browser window, then click "
                                        "\"I've logged in\" again."})
                                    done = True
                                    break
                                tameen_go_to_payments(tameen_page)
                                tameen_click_payments_by_channel(tameen_page)
                                st["channels"] = _read_channels(tameen_page)
                                result_queue.put({"ok": True, "channels": st["channels"]})
                                done = True
                                break
                            except Exception as e:
                                last_err = e
                        if not done:
                            result_queue.put({"ok": False, "error":
                                f"Could not open Payments by Channel ({last_err}). Make sure "
                                "Tameen is fully logged in, then click \"I've logged in\" again."})

                    elif action == "select_channel":
                        tile = st["channels"][args["index"]]
                        st["channel_name"] = tile["channel"]
                        # No bring_to_front: the tile click + record read use JS /
                        # Playwright clicks that work on the background tab, so we
                        # don't yank the Chromium window in front of the control panel.
                        _click_channel(tameen_page, tile["channel"], tile.get("section"))
                        data = _read_records(tameen_page)
                        st["records"] = data["rows"]
                        result_queue.put({"ok": True, "records": data,
                                          "channel_name": tile["channel"]})

                    elif action in ("reset", "back_to_channels"):
                        if action == "reset":
                            # Close policy clutter first: only the working tabs stay.
                            _close_extra_tabs(context, (tameen_page, mic_page, ni_page, iran_page))
                            # Reset only the insurer tab we actually used this record
                            # (default MIC if we never got that far). Guard each so one
                            # failing doesn't abort the rest.
                            try:
                                if st.get("last_company") == "NEW_INDIA":
                                    ni_reset_to_motor_policy(ni_page)
                                elif st.get("last_company") == "IRAN":
                                    # IRAN has no in-app reset — a goto to the Dashboard
                                    # (keeps the login cookie) returns it to a known state.
                                    iran_page.goto(IRAN_DASHBOARD_URL,
                                                   wait_until="domcontentloaded", timeout=60000)
                                else:
                                    mic_reset_to_home(mic_page)
                            except Exception as e:
                                print(f"  ⚠️  Insurer reset issue: {e}")
                            try:
                                tameen_reset_to_payments(tameen_page)
                            except Exception as e:
                                print(f"  ⚠️  Tameen reset issue: {e}")
                        # No bring_to_front here either — same reason as select_channel.
                        # Land on 'Payments by Channel'. If the gentle reset didn't get
                        # Tameen there (stuck deep after issuing), force a goto to the
                        # dashboard — works from ANY page — and retry, bounded.
                        try:
                            tameen_click_payments_by_channel(tameen_page)
                        except Exception:
                            tameen_page.goto(TAMEEN_DASHBOARD_URL,
                                             wait_until="domcontentloaded", timeout=30000)
                            tameen_go_to_payments(tameen_page)
                            tameen_click_payments_by_channel(tameen_page)
                        st["channels"] = _read_channels(tameen_page)
                        result_queue.put({"ok": True, "channels": st["channels"]})

                    else:
                        result_queue.put({"ok": False, "error": f"Unknown action: {action}"})

                except Exception as e:
                    result_queue.put({"ok": False, "error": str(e)})

            context.close()
    except Exception as e:
        worker_state["startup_error"] = str(e)
        worker_ready.set()


def run_action(action, **args):
    """Send a request/response action to the worker and block for its reply."""
    if worker_state["startup_error"]:
        return {"ok": False, "error": worker_state["startup_error"]}
    command_queue.put({"action": action, "args": args})
    try:
        return result_queue.get(timeout=180)
    except queue.Empty:
        return {"ok": False, "error": (
            "The browser did not respond in time. If a 'Save As' window is open "
            "behind the browser, finish it and try again; otherwise close this "
            "window and relaunch with start.bat.")}


def _channels_for_template(channels):
    return [{"index": i, "name": c["channel"],
             "count": c.get("count"), "section": c.get("section")}
            for i, c in enumerate(channels)]


def _records_for_template(data):
    """Turn the raw Tameen rows into a real table: column titles + per-row cells.
    Tameen's first column is the View/eye control, so headers[1:] line up with the
    cells we kept (the reader already drops the first cell)."""
    headers = data.get("headers", [])
    rows = data.get("rows", [])
    ncol = max((len(r["cells"]) for r in rows), default=0)
    titles = list(headers[1:]) if len(headers) > 1 else []
    if titles:
        titles = (titles + [""] * ncol)[:ncol]          # pad/truncate to the widest row
    else:
        titles = [f"Field {i + 1}" for i in range(ncol)]
    out_rows = [{"index": i, "cells": r["cells"],
                 "company": r.get("company"),
                 "insurer": _INSURER_LABELS.get(r.get("company"), "Other")}
                for i, r in enumerate(rows)]
    return {"headers": titles, "rows": out_rows}


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES — never touch Playwright, only the queues.
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    worker_ready.wait(timeout=90)
    if worker_state["startup_error"]:
        return render_template("error.html", allow_retry=False,
                               message="The automation browser could not start:\n\n"
                                       + worker_state["startup_error"])
    return render_template("index.html")


@app.route("/login-done", methods=["POST"])
def login_done():
    res = run_action("login_done")
    if not res.get("ok"):
        return render_template("error.html", message=res.get("error"), allow_retry=True)
    ui_cache["channels"] = _channels_for_template(res["channels"])
    return render_template("channels.html", channels=ui_cache["channels"])


@app.route("/select-channel/<int:idx>", methods=["POST"])
def select_channel(idx):
    res = run_action("select_channel", index=idx)
    if not res.get("ok"):
        return render_template("error.html", message=res.get("error"), allow_retry=True)
    ui_cache["records"] = _records_for_template(res["records"])
    ui_cache["channel_name"] = res.get("channel_name", "")
    return render_template("records.html", table=ui_cache["records"],
                           channel_name=ui_cache["channel_name"])


@app.route("/records")
def records():
    # Re-render the current channel's record list without touching the browser.
    return render_template("records.html", table=ui_cache["records"],
                           channel_name=ui_cache["channel_name"])


@app.route("/back-to-channels", methods=["POST"])
def back_to_channels():
    res = run_action("back_to_channels")
    if not res.get("ok"):
        return render_template("error.html", message=res.get("error"), allow_retry=True)
    ui_cache["channels"] = _channels_for_template(res["channels"])
    return render_template("channels.html", channels=ui_cache["channels"])


@app.route("/confirm-row/<int:idx>", methods=["POST"])
def confirm_row(idx):
    # Confirm FIRST, using only the row already shown in the table — no browser
    # work yet. The Tameen read + MIC fill happen after the employee clicks Yes.
    rows = ui_cache["records"]["rows"]
    if idx >= len(rows):
        return redirect(url_for("records"))
    pairs = list(zip(ui_cache["records"]["headers"], rows[idx]["cells"]))
    insurer = rows[idx].get("insurer") or "Muscat Insurance"
    return render_template("confirm.html", idx=idx, pairs=pairs, insurer=insurer,
                           channel_name=ui_cache.get("channel_name", ""))


@app.route("/start-processing/<int:idx>", methods=["POST"])
def start_processing(idx):
    # Fire-and-forget: the worker reads Tameen AND fills MIC in one run, streaming
    # progress over SSE. record_text (for the progress header) is rebuilt from the
    # row cells so we don't need a round-trip first.
    rows = ui_cache["records"]["rows"]
    row = rows[idx] if idx < len(rows) else {}
    record_text = "  |  ".join(row.get("cells", []))
    company = row.get("company") or "MIC"
    steps = {"NEW_INDIA": NI_STEPS, "IRAN": IRAN_STEPS}.get(company, MIC_STEPS)
    insurer = _INSURER_LABELS.get(company, "Muscat Insurance")
    # Drop any events left over from a progress page that was abandoned before its
    # stream reached final/error — otherwise this run's SSE stream would read those
    # stale events first. Safe: the worker hasn't received this command yet, so the
    # queue is empty on the normal path and only holds stale events otherwise.
    try:
        while True:
            progress_queue.get_nowait()
    except queue.Empty:
        pass
    command_queue.put({"action": "process_record", "args": {"index": idx}})
    return render_template("progress.html", steps=steps, record_text=record_text,
                           insurer=insurer)


@app.route("/progress-stream")
def progress_stream():
    def gen():
        while True:
            ev = progress_queue.get()
            yield f"data: {json.dumps(ev)}\n\n"
            if ev.get("type") in ("final", "error"):
                break
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/iran-continue", methods=["POST"])
def iran_continue():
    """The employee solved the IRAN CAPTCHA and clicked Continue on the progress
    page. Release the worker's login step (it's blocked in _iran_captcha_pause).
    Touches no Playwright — just sets a thread event."""
    iran_resume.set()
    return ("", 204)


@app.route("/reset", methods=["POST"])
def reset():
    res = run_action("reset")
    if not res.get("ok"):
        return render_template("error.html", message=res.get("error"), allow_retry=True)
    ui_cache["channels"] = _channels_for_template(res["channels"])
    return render_template("channels.html", channels=ui_cache["channels"])


if __name__ == "__main__":
    threading.Thread(target=worker_main, daemon=True).start()
    # use_reloader=False so Flask does not spawn a SECOND process (which would try
    # to launch a second browser). threaded=True is safe — only the worker thread
    # ever touches Playwright.
    def _open_browser():
        time.sleep(1.5)
        webbrowser.open("http://localhost:5000")
    threading.Thread(target=_open_browser, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, threaded=True, use_reloader=False, debug=False)
