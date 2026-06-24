# Production Flow — Reference Map for the UI

Plain-English summary of exactly what the MIC automation script does today, in
order. This is a map for the next prompt, which will wrap this same flow in a
web UI. The flow drives two browser tabs in one window: a **Tameen** tab (source
of the record data) and an **MIC / Muscat Insurance** tab (the form being
filled). Most of the real work lives in helper functions imported from
`common.py`; the script orchestrates them.

---

## Stage 0 — Browser & tabs start-up (one time)
- Launches a single persistent Chromium window (`launch_persistent_context`,
  profile folder `automation_profile`, non-headless, small `slow_mo` delay).
- Opens the **Tameen** tab and navigates to the Tameen login page.
- Opens the **MIC** tab and navigates to `MIC_HOME_URL`.
- Registers a native-dialog auto-accept handler on both tabs (clicks OK on real
  browser "Leave site?" popups so a reset can't get silently blocked).
- Per-action timeout is set to 2 minutes on each tab.

## Stage 1 — Manual login pause (one time)
- Prints an "ACTION REQUIRED" banner and waits on a plain `input()` ENTER prompt
  while the employee manually logs into Tameen and completes the OTP.
- This wait has no Playwright timeout, so it can take as long as needed.

## Stage 2 — Land on the Payments page (one time)
- Brings the Tameen tab to the front.
- `tameen_go_to_payments(page)` — clicks the PAYMENTS tile to reach the payments
  area.

## Stage 3 — Per-record loop begins
Everything below repeats once per policy. At the end of each pass the employee
chooses to process another record or quit (`q`).

### 3a — Pick channel and record
- Brings the Tameen tab to the front (clipboard reads need it focused).
- Inner loop so the employee can back out and re-pick a channel:
  - `tameen_click_payments_by_channel(page)` — opens "Payments by Channel".
  - `tameen_select_channel(page)` — choose a channel; returns the channel name.
  - `tameen_select_and_click_eye(page)` — defined **in this script**. Lists only
    the **Muscat Insurance** rows, asks which row number to open (or `0` to go
    back to channel select), opens the chosen record, and returns its row text.

### 3b — Read fields from the Tameen record
- `read_field(page, "<label>")` is called for each field: First Name, Last Name,
  License ID, Product Name, Previous Expiry, Vehicle Number, Sum Insured,
  Total Premium.
- Seats are read by trying several likely labels in turn (Seats, No. of Seats,
  Seating Capacity, …) and keeping just the digits.

### 3c — Derive the values MIC needs
- Builds full name from first + last.
- `parse_tameen_date(prev_expiry)` then `compute_period_from(...)` → MIC
  "Period From" date.
- `split_plate(vehicle_no)` → plate code + plate number.
- Decides the policy-type source: normally `Product Name`, but for the
  **Mobileapp** channel (where Product Name is blank) it reads a dedicated
  "Policy Type" field, falling back to the type tag in the record row text.
- Collects everything into a `prepared` dict and prints a "VALUES PREPARED FOR
  MIC" summary table.

### 3d — Fill the MIC form (left as Draft, never auto-approved)
- Brings the MIC tab to the front, then calls, in order:
  - `mic_login_if_needed(page)` — log into MIC if not already.
  - `mic_open_policy_create(page)` — open the create-policy screen.
  - `mic_choose_policy_type_and_create(page, type_source)` — pick policy type;
    returns whether it is comprehensive.
  - `mic_get_licence(page, license_id)` — look up the licence.
  - `mic_fill_policy_info(page, full_name, period_from)` — fill policy info.
  - `mic_get_vehicle(page, plate_number, plate_code)` — look up the vehicle.
  - `mic_fill_vehicle_info(page, is_comprehensive, sum_insured, seats)` — fill
    vehicle info.
  - `mic_calculate_and_check(page, tameen_total)` — **the premium comparison**:
    calculates the MIC premium and checks it against the Tameen total.

### 3e — Premium comparison outcome
- The comparison happens inside `mic_calculate_and_check`. A match / mismatch is
  surfaced to the employee (the prominent premium-mismatch warning). The policy
  is intentionally left as **Draft** — there is no auto-approve step.

### 3f — Review and decide
- Prints "MIC FLOW FINISHED" with the record text.
- `input()` prompt: ENTER to reset both tabs and process another record, or `q`
  to finish.

### 3g — Error / flagged-quote path
- If any step in 3a–3f throws, the loop catches it and prints a "FLAGGED FOR
  REVIEW" banner with the reason and whatever record details were prepared so
  far, then waits at the same ENTER-or-`q` prompt.

### 3h — Reset for the next record
- On continue (after success **or** a flagged error):
  - `mic_reset_to_home(page)` — send the MIC tab back to its start.
  - `tameen_reset_to_payments(page)` — send the Tameen tab back to Payments.
- Loop repeats from 3a.

## Stage 4 — Shutdown
- A top-level handler catches problems in the one-time login / first navigation.
- A `finally` block waits on a final ENTER prompt before closing the browser —
  nothing closes automatically.

---

## Key functions by stage (quick index)
| Stage | Functions |
|-------|-----------|
| Start-up / tabs | `sync_playwright`, `launch_persistent_context`, dialog auto-accept |
| Navigate to payments | `tameen_go_to_payments` |
| Pick channel + row | `tameen_click_payments_by_channel`, `tameen_select_channel`, `tameen_select_and_click_eye` (local) |
| Read Tameen fields | `read_field` |
| Derive values | `parse_tameen_date`, `compute_period_from`, `split_plate` |
| Fill MIC form | `mic_login_if_needed`, `mic_open_policy_create`, `mic_choose_policy_type_and_create`, `mic_get_licence`, `mic_fill_policy_info`, `mic_get_vehicle`, `mic_fill_vehicle_info` |
| Premium comparison | `mic_calculate_and_check` |
| Reset / loop | `mic_reset_to_home`, `tameen_reset_to_payments` |
