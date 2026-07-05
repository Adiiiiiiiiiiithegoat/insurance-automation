#!/usr/bin/env python3
"""
Standalone test of the body type mapping logic.
Run this to verify the mapping works before running full tests.

    python test_body_type_mapping.py

Tests various Tameen body types to see if they map correctly.
"""

# Import the mapping function from test.py
import sys
import os

# Windows console is cp1252 by default and chokes on the ✅/⚠️/→ characters below.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test import ni_body_type_target


def test_mapping():
    """Test various body type + seat combinations."""
    test_cases = [
        # (body_type, seats, expected_contains)
        ("SEDAN", "5", ["SALOON"]),
        ("SALOON", "4", ["SALOON"]),
        ("SUV", "5", ["FOUR WHEEL DRIVE"]),
        ("4WD", "5", ["FOUR WHEEL DRIVE"]),
        ("JEEP", "5", ["FOUR WHEEL DRIVE"]),
        ("HATCHBACK", "5", ["HATCH BACK"]),
        ("HATCH BACK", "5", ["HATCH BACK"]),
        ("PICKUP", "3", ["PICKUP", "3 TON"]),
        ("PICKUP", "5", ["PICKUP", "4WD"]),
        ("PICKUP", "", ["PICKUP"]),
        ("PICK UP", "3", ["PICKUP", "3 TON"]),          # real Tameen spelling has a space
        ("PICK UP DOUBLE CAB", "5", ["PICKUP", "4WD"]),  # actual value seen from a live record
        ("4 WHEEL DRIVE", "5", ["FOUR WHEEL DRIVE"]),    # real Tameen spelling has a space
        ("FOUR WHEEL", "5", ["FOUR WHEEL DRIVE"]),
        ("COUPE", "2", ["SALOON"]),
        ("VAN", "5", ["HATCH BACK"]),
        ("", "5", None),  # unmapped
        ("UNKNOWN_BODY_TYPE", "5", None),  # unmapped
    ]

    print("\n" + "=" * 80)
    print("  BODY TYPE MAPPING TESTS")
    print("=" * 80)

    passed = 0
    failed = 0

    for body_type, seats, expected in test_cases:
        result = ni_body_type_target(body_type, seats)
        status = "✅"
        reason = ""

        if expected is None:
            if result is None:
                passed += 1
            else:
                failed += 1
                status = "❌"
                reason = f"expected None, got {result}"
        else:
            if result and all(exp in result for exp in expected):
                passed += 1
            else:
                failed += 1
                status = "❌"
                if not result:
                    reason = f"expected {expected}, got None"
                else:
                    reason = f"expected {expected}, got {result}"

        body_display = f"'{body_type}'" if body_type else "(empty)"
        seats_display = f"seats={seats}" if seats else "seats=unknown"
        result_display = str(result) if result else "None (manual pick)"

        print(f"  {status}  {body_display:<20} {seats_display:<15} → {result_display}")
        if reason:
            print(f"      {reason}")

    print("=" * 80)
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 80)

    return failed == 0


if __name__ == "__main__":
    success = test_mapping()
    sys.exit(0 if success else 1)
