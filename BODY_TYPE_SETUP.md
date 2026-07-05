# New India Body Type Mapping Setup

The body type field accuracy was ~0% because the mapping was hardcoded and incomplete. This guide walks you through building a comprehensive, data-driven mapping.

## What This Does

Three new files work together:
- **`ni_body_type_scraper.py`** — extracts all Body Type dropdown options from New India
- **`ni_body_type_mapping.json`** — the mapping dict (Tameen → New India options)
- **`ni_body_type_tracker.py`** — logs unmatched body types during test runs, so you can improve the mapping incrementally

## Step 1: Scrape All Body Type Options from New India

Run this once to see what options are available in the New India form:

```bash
python ni_body_type_scraper.py
```

This will:
1. Log in to New India
2. Open the Motor Policy form
3. Navigate to Vehicle Details
4. Sample a few Make/Model combinations
5. Extract all Body Type options and save to `ni_body_types.json`

Output:
```json
{
  "unique_body_types": [
    "FOUR WHEEL DRIVE (UPTO 15,000)",
    "FOUR WHEEL DRIVE (15001-50000)",
    "SALOON (UPTO 15,000)",
    "SALOON (15001-50000)",
    "HATCH BACK (UPTO 15,000)",
    ...
  ],
  "by_make_model": { ... }
}
```

## Step 2: Review and Update the Mapping

Open `ni_body_type_mapping.json` and review the `"mappings"` section. Each entry maps a **Tameen body type** to a **list of substrings** that must appear in the New India dropdown option.

Example:
```json
{
  "SEDAN": ["SALOON", "UPTO 15"],
  "PICKUP": {
    "description": "PICKUP depends on seating capacity",
    "seats_3": ["PICKUP", "3 TON"],
    "seats_other": ["PICKUP", "4WD"]
  }
}
```

When the automation encounters `SEDAN` from Tameen, it looks for a New India option that contains **both** "SALOON" and "UPTO 15" — e.g. `"SALOON (UPTO 15,000)"` matches.

### How Matching Works

For each Tameen body type + seats combo, the automation tries (in order):

1. **Exact seat-based key**: `"PICKUP_3"` in the mapping
2. **Exact body type key**: `"SEDAN"` in the mapping
3. **Substring match**: any key that appears in the body type (e.g., `"SALOON"` in `"SEDAN SALOON"`)
4. **Fallback**: `None` → user picks it manually

When a key's value is a **dict** (not a list), it must have seat-based branches:
- `"seats_3"` — returned when seats = 3
- `"seats_other"` — returned for other seat counts
- `"seats_unknown"` — fallback when seat count is unavailable

## Step 3: Run Test Cycles and Track Misses

As you test with `test_ni.py` or `test.py`:

1. Every body type that doesn't have a mapping is **automatically logged** to `unmatched_body_types.json`
2. The automation will ask you to pick the Body Type manually
3. After testing, check what was unmatched:

```bash
python ni_body_type_tracker.py
```

Output:
```
UNMATCHED BODY TYPES (need mappings)
================================================================================
  SPORTS CAR                       (  2 times) seats=unknown, seats=4
  SUV HYBRID                       (  1 times) seats=5
  MICROVAN                         (  1 times) seats=7
================================================================================
```

## Step 4: Add Missing Mappings

For each unmatched body type, add an entry to `ni_body_type_mapping.json`:

1. **Check `ni_body_types.json`** to see what options are available in New India
2. **Guess the right option** based on the body type (e.g., "SPORTS CAR" → "SALOON")
3. **Add to the mapping**:

```json
{
  "SPORTS CAR": ["SALOON", "UPTO 15"],
  "SUV HYBRID": ["FOUR WHEEL DRIVE", "UPTO 15"],
  "MICROVAN": ["HATCH BACK", "UPTO 15"]
}
```

## Step 5: Verify Your Mapping

Delete `unmatched_body_types.json` and run tests again. If new unmatched body types appear, repeat Step 4.

Once no unmatched types appear for a full test run, the mapping is complete.

## Mapping Strategy

- **Sedan / Saloon** → `"SALOON"` option
- **SUV / 4WD / Jeep** → `"FOUR WHEEL DRIVE"` option
- **Hatchback / Coupe / Wagon** → `"HATCH BACK"` option
- **Pickup / Truck** → `"PICKUP"` option (varies by seats: 3-ton vs 4WD)
- **Van / Minivan** → `"HATCH BACK"` option

For the **value bracket** (UPTO 15,000 vs 15001-50000):
- Default to **"UPTO 15"** (we don't know the vehicle value from Tameen)
- If you know the value is higher, manually switch to the "15001-50000" bracket during review

## Files

| File | Purpose |
|------|---------|
| `ni_body_type_scraper.py` | Extract all dropdown options from New India (run once, then maybe re-run when NI updates) |
| `ni_body_types.json` | Output from the scraper; reference of all available New India body types |
| `ni_body_type_mapping.json` | **Edit this** — the Tameen → New India mapping dict |
| `ni_body_type_tracker.py` | Helper to track unmatched body types and print a summary |
| `unmatched_body_types.json` | Auto-generated during testing; delete to reset |

## Troubleshooting

**Q: Body type still says "please pick by hand"**
- Check if `ni_body_type_mapping.json` has been created/updated
- Verify the body type key is in the mapping (case doesn't matter, but spaces and punctuation do)
- Run `python ni_body_type_tracker.py` to see what was unmatched

**Q: The option was selected but it's the wrong one**
- The substring matching picked a close match that wasn't quite right
- Update the mapping to be more specific (e.g., add "SEDAN" to distinguish from other SALOONs)
- Or check if a value-bracket option (UPTO 15 vs 15001-50000) was wrong for that vehicle

**Q: How do I know what value bracket to use?**
- For now, we default to "UPTO 15,000" since Tameen doesn't tell us the vehicle value
- If you know a vehicle is worth more, manually switch to "15001-50000" in the New India review
- In the future, we could pull vehicle value from Tameen if it becomes available
