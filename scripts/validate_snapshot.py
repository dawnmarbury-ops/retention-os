#!/usr/bin/env python3
"""
Fail-loud validator for positions snapshots.

Usage:
    python scripts/validate_snapshot.py [path-to-snapshot.json]

Default path is out/snapshots/positions.latest.json relative to the repo root.

Checks:
  - File exists, size >= 200 bytes
  - JSON parses
  - Validates against positions.schema.json (draft-07)
  - positions.length > 0
  - Every position: qty > 0, price_usd > 0, nav_usd > 0
  - total_nav_usd equals sum of position nav_usd within $0.01
  - Weights sum to 1.0 +/- 0.0001
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft7Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "out" / "snapshots" / "positions.latest.json"
SCHEMA_PATH = REPO_ROOT / "positions.schema.json"
MIN_BYTES = 200
NAV_TOLERANCE = 0.01
WEIGHT_TOLERANCE = 0.0001


def die(msg: str, code: int = 1) -> None:
    print(f"VALIDATION FAILED: {msg}", file=sys.stderr)
    sys.exit(code)


def main(argv: list[str]) -> int:
    path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_PATH

    if not path.exists():
        die(f"snapshot file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_BYTES:
        die(f"snapshot too small ({size} bytes < {MIN_BYTES})")

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"snapshot is not valid JSON: {exc}")

    if not SCHEMA_PATH.exists():
        die(f"schema not found: {SCHEMA_PATH}")
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
    except json.JSONDecodeError as exc:
        die(f"schema is not valid JSON: {exc}")

    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path))
    if errors:
        details = "; ".join(
            f"{list(e.absolute_path) or '<root>'}: {e.message}" for e in errors
        )
        die(f"schema violations: {details}")

    positions = data.get("positions", [])
    if len(positions) == 0:
        die("positions array is empty")

    for i, p in enumerate(positions):
        sym = p.get("symbol", "?")
        if p["qty"] <= 0:
            die(f"position[{i}] {sym}: qty <= 0 (got {p['qty']})")
        if p["price_usd"] <= 0:
            die(f"position[{i}] {sym}: price_usd <= 0 (got {p['price_usd']})")
        if p["nav_usd"] <= 0:
            die(f"position[{i}] {sym}: nav_usd <= 0 (got {p['nav_usd']})")

    nav_sum = sum(p["nav_usd"] for p in positions)
    total = data["total_nav_usd"]
    if abs(nav_sum - total) > NAV_TOLERANCE:
        die(
            f"total_nav_usd mismatch: declared {total}, sum of positions {nav_sum}, "
            f"diff {abs(nav_sum - total)} > tolerance {NAV_TOLERANCE}"
        )

    weight_sum = sum(p["weight"] for p in positions)
    if abs(weight_sum - 1.0) > WEIGHT_TOLERANCE:
        die(
            f"weights sum to {weight_sum}, expected 1.0 +/- {WEIGHT_TOLERANCE}"
        )

    print(
        f"OK: {path} — {len(positions)} positions, total_nav_usd={total:.2f}, "
        f"weights_sum={weight_sum:.6f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
