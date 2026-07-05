"""
Helper to track and collect unmatched body types from Tameen during testing.
This builds up a JSON file of "seen body types" that don't yet have mappings,
so we can incrementally improve the ni_body_type_mapping.json.

Usage:
  from ni_body_type_tracker import track_body_type

  # In your test loop, after reading body_type and seats from Tameen:
  track_body_type(body_type, seats, target_result)
  # This logs the unmatched ones to unmatched_body_types.json
"""
import json
import os
import sys
from datetime import datetime

# Windows console is cp1252 by default and chokes on the ✅/⚠️ emojis below.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


UNMATCHED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unmatched_body_types.json")


def track_body_type(body_type_raw: str, seats: str, target_result):
    """Log a body type lookup to track what's not in the mapping.

    Args:
      body_type_raw: the raw string read from Tameen
      seats: the seat count
      target_result: the result from ni_body_type_target() (None if unmatched)
    """
    if target_result is not None:
        return  # Only log unmatched ones

    key = (body_type_raw or "").strip().upper()
    if not key:
        return

    # Load existing file
    try:
        with open(UNMATCHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"body_types": {}, "last_updated": None}

    # Add or increment this body type
    if key not in data["body_types"]:
        data["body_types"][key] = {
            "count": 0,
            "examples_with_seats": [],
            "first_seen": datetime.now().isoformat()
        }

    data["body_types"][key]["count"] += 1
    data["body_types"][key]["last_seen"] = datetime.now().isoformat()

    # Keep a few seat examples
    example = f"seats={seats}" if seats else "seats=unknown"
    if example not in data["body_types"][key]["examples_with_seats"]:
        data["body_types"][key]["examples_with_seats"].append(example)
    if len(data["body_types"][key]["examples_with_seats"]) > 5:
        data["body_types"][key]["examples_with_seats"] = data["body_types"][key]["examples_with_seats"][-5:]

    data["last_updated"] = datetime.now().isoformat()

    # Write back
    try:
        with open(UNMATCHED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️  Could not write unmatched body types log: {e}")


def print_summary():
    """Print a summary of all unmatched body types seen so far."""
    try:
        with open(UNMATCHED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        print("No unmatched body types logged yet.")
        return

    body_types = data.get("body_types", {})
    if not body_types:
        print("No unmatched body types logged yet.")
        return

    print("\n" + "=" * 70)
    print("  UNMATCHED BODY TYPES (need mappings)")
    print("=" * 70)
    for bt, info in sorted(body_types.items(), key=lambda x: -x[1]["count"]):
        count = info.get("count", 0)
        examples = ", ".join(info.get("examples_with_seats", []))
        print(f"  {bt:<30} ({count:3} times) {examples}")
    print("=" * 70)
    print(f"\nSee {UNMATCHED_FILE} for full details.")
    print("Update ni_body_type_mapping.json with entries for these body types,")
    print("then delete unmatched_body_types.json to reset tracking.")


if __name__ == "__main__":
    print_summary()
