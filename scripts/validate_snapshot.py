#!/usr/bin/env python3
"""
Fail-loud validator for positions snapshots (schema 2.0.0).

Usage:
    python scripts/validate_snapshot.py [path-to-snapshot.json]

Default path: out/snapshots/positions.latest.json

Checks:
  - File exists, size >= 200 bytes
  - JSON parses
  - Validates against positions.schema.json (draft-07)
  - positions.length > 0
  - Every position: qty > 0, price_usd > 0, nav_usd > 0
  - Every position: price_change_24h_percent is numeric
  - Every position's (symbol, chain) exists in tokens.json
  - Position decimals match tokens.json for that (symbol, chain)
  - Position contract_address matches tokens.json for that (symbol, chain)
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
TOKENS_PATH = REPO_ROOT / "tokens.json"
MIN_BYTES = 200
NAV_TOLERANCE = 0.01
WEIGHT_TOLERANCE = 0.0001


def die(msg: str, code: int = 1) -> None:
    print(f"VALIDATION FAILED: {msg}", file=sys.stderr)
    sys.exit(code)


def _load_json(path: Path, label: str) -> dict:
    if not path.exists():
        die(f"{label} not found: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"{label} is not valid JSON: {exc}")


def _tokens_index(tokens_doc: dict) -> dict:
    tokens = tokens_doc.get("tokens", [])
    if not isinstance(tokens, list) or not tokens:
        die("tokens.json has no 'tokens' array")
    idx: dict = {}
    for i, t in enumerate(tokens):
        if "symbol" not in t or "chain" not in t:
            die(f"tokens.json entry {i} missing 'symbol' or 'chain'")
        idx[(t["symbol"], t["chain"])] = t
    return idx


def _norm_contract(addr: str) -> str:
    return "native" if addr == "native" else addr.lower()


def main(argv: list[str]) -> int:
    path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_PATH

    if not path.exists():
        die(f"snapshot file does not exist: {path}")
    size = path.stat().st_size
    if size < MIN_BYTES:
        die(f"snapshot too small ({size} bytes < {MIN_BYTES})")

    data = _load_json(path, "snapshot")
    schema = _load_json(SCHEMA_PATH, "schema")
    tokens_doc = _load_json(TOKENS_PATH, "tokens.json")
    tokens_index = _tokens_index(tokens_doc)

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
        chain = p.get("chain", "?")

        if p["qty"] <= 0:
            die(f"position[{i}] {sym}@{chain}: qty <= 0 (got {p['qty']})")
        if p["price_usd"] <= 0:
            die(f"position[{i}] {sym}@{chain}: price_usd <= 0 (got {p['price_usd']})")
        if p["nav_usd"] <= 0:
            die(f"position[{i}] {sym}@{chain}: nav_usd <= 0 (got {p['nav_usd']})")
        change = p.get("price_change_24h_percent")
        if not isinstance(change, (int, float)):
            die(
                f"position[{i}] {sym}@{chain}: "
                f"price_change_24h_percent missing or not numeric (got {change!r})"
            )

        key = (sym, chain)
        if key not in tokens_index:
            die(f"position[{i}] ({sym}, {chain}) is not declared in tokens.json")
        token = tokens_index[key]
        if p["decimals"] != token["decimals"]:
            die(
                f"position[{i}] {sym}@{chain}: decimals mismatch — "
                f"position={p['decimals']}, tokens.json={token['decimals']}"
            )
        expected = _norm_contract(token["contract_address"])
        actual = _norm_contract(p["contract_address"])
        if actual != expected:
            die(
                f"position[{i}] {sym}@{chain}: contract_address mismatch — "
                f"position={p['contract_address']!r}, tokens.json={token['contract_address']!r}"
            )

    nav_sum = sum(p["nav_usd"] for p in positions)
    total = data["total_nav_usd"]
    if abs(nav_sum - total) > NAV_TOLERANCE:
        die(
            f"total_nav_usd mismatch: declared {total}, sum of positions {nav_sum}, "
            f"diff {abs(nav_sum - total)} > tolerance {NAV_TOLERANCE}"
        )

    weight_sum = sum(p["weight"] for p in positions)
    if abs(weight_sum - 1.0) > WEIGHT_TOLERANCE:
        die(f"weights sum to {weight_sum}, expected 1.0 +/- {WEIGHT_TOLERANCE}")

    print(
        f"OK: {path} — {len(positions)} positions, total_nav_usd={total:.2f}, "
        f"weights_sum={weight_sum:.6f}, schema={data.get('schema_version')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
