#!/usr/bin/env python3
"""
Fail-loud validator for positions snapshots (schema 2.1.0).

Usage:
    python scripts/validate_snapshot.py [path-to-snapshot.json]

Default path: out/snapshots/positions.latest.json

Checks applied to every position:
  - JSON Schema (draft-07) shape
  - (symbol, chain) is declared in tokens.json
  - decimals matches tokens.json for that (symbol, chain)
  - contract_address matches tokens.json (with "native" normalized)
  - price_usd > 0
  - price_change_24h_percent is numeric

Additional checks for source_priority="onchain":
  - qty > 0, nav_usd > 0, numeric weight
  - wallet_address is a non-empty string

source_priority="price_only" intentionally skips qty/nav/weight checks
(nullable for Legacy tokens that we price-track but don't balance-track).

Aggregate checks:
  - File exists, size >= 200 bytes
  - JSON parses
  - positions.length > 0
  - total_nav_usd == sum of non-null nav_usd within $0.01
  - non-null weights sum to 1.0 +/- 0.0001
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

VALID_SOURCE_PRIORITIES = {"onchain", "price_only"}


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
        sp = p.get("source_priority")

        if sp not in VALID_SOURCE_PRIORITIES:
            die(f"position[{i}] {sym}@{chain}: unknown source_priority {sp!r}")

        if not isinstance(p.get("price_usd"), (int, float)) or p["price_usd"] <= 0:
            die(f"position[{i}] {sym}@{chain}: price_usd <= 0 (got {p.get('price_usd')!r})")
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

        if sp == "onchain":
            if not isinstance(p.get("qty"), (int, float)) or p["qty"] <= 0:
                die(f"position[{i}] {sym}@{chain}: onchain entry requires qty > 0 (got {p.get('qty')!r})")
            if not isinstance(p.get("nav_usd"), (int, float)) or p["nav_usd"] <= 0:
                die(f"position[{i}] {sym}@{chain}: onchain entry requires nav_usd > 0 (got {p.get('nav_usd')!r})")
            if not isinstance(p.get("weight"), (int, float)):
                die(f"position[{i}] {sym}@{chain}: onchain entry requires numeric weight (got {p.get('weight')!r})")
            wa = p.get("wallet_address")
            if not isinstance(wa, str) or not wa:
                die(f"position[{i}] {sym}@{chain}: onchain entry requires non-empty wallet_address")
        # source_priority="price_only" intentionally skips qty/nav/weight/wallet
        # checks; the schema permits null on those fields for price-only entries.

    nav_sum = sum(p["nav_usd"] for p in positions if p.get("nav_usd") is not None)
    total = data["total_nav_usd"]
    if abs(nav_sum - total) > NAV_TOLERANCE:
        die(
            f"total_nav_usd mismatch: declared {total}, sum of non-null nav_usd {nav_sum}, "
            f"diff {abs(nav_sum - total)} > tolerance {NAV_TOLERANCE}"
        )

    weight_sum = sum(p["weight"] for p in positions if p.get("weight") is not None)
    if abs(weight_sum - 1.0) > WEIGHT_TOLERANCE:
        die(f"non-null weights sum to {weight_sum}, expected 1.0 +/- {WEIGHT_TOLERANCE}")

    onchain_count = sum(1 for p in positions if p.get("source_priority") == "onchain")
    price_only_count = sum(1 for p in positions if p.get("source_priority") == "price_only")
    print(
        f"OK: {path} — {len(positions)} positions "
        f"(onchain={onchain_count}, price_only={price_only_count}), "
        f"total_nav_usd={total:.2f}, weights_sum={weight_sum:.6f}, "
        f"schema={data.get('schema_version')}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
