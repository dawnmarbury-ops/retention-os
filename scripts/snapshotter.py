#!/usr/bin/env python3
"""
Snapshotter v2.1 — fail-loud multi-chain NAV snapshot with price-only Legacy.

Reads wallet addresses from per-chain env vars (EVM_ADDRESSES, BTC_ADDRESSES,
SOL_ADDRESSES) and the token allowlist from tokens.json. Each token carries a
track_balance flag:

  - track_balance: true   -> balance-fetched via the chain's connector and
                             priced via CoinGecko (source_priority="onchain")
  - track_balance: false  -> CoinGecko price only, no balance fetch, emitted
                             with null qty/wallet/nav/weight and
                             source_priority="price_only"

All ERC-20 contracts on ethereum-mainnet (both balance-tracked and price-only)
are symbol-verified on-chain before any snapshot is written. WELL is on Base
and is intentionally NOT verified — its contract_address is informational
metadata only, since no Base connector is in scope this sprint.

Environment:
  ALCHEMY_ETH_KEY    required — Alchemy app key (Solana Mainnet must be enabled)
  EVM_ADDRESSES      required — comma-separated lowercase 0x-prefixed addresses
  BTC_ADDRESSES      required — comma-separated BTC addresses
  SOL_ADDRESSES      required — comma-separated Solana Base58 pubkeys
  COINGECKO_API_KEY  optional — public endpoint works without
  DRY_RUN            if "true"/"1"/"yes", logs intended output and skips writes
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from jsonschema import Draft7Validator

from connectors import bitcoin as btc_connector
from connectors import ethereum as eth_connector
from connectors import solana as sol_connector
from connectors._common import die, http_get_json

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS_PATH = REPO_ROOT / "tokens.json"
SCHEMA_PATH = REPO_ROOT / "positions.schema.json"
OUT_DIR = REPO_ROOT / "out" / "snapshots"
SCHEMA_VERSION = "2.1.0"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"

CHAIN_ETHEREUM = "ethereum-mainnet"
CHAIN_BITCOIN = "bitcoin-mainnet"
CHAIN_SOLANA = "solana-mainnet"


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"missing or empty environment variable: {name}")
    return value


def load_tokens() -> list[dict]:
    if not TOKENS_PATH.exists():
        die(f"tokens.json not found at {TOKENS_PATH}")
    try:
        data = json.loads(TOKENS_PATH.read_text())
    except json.JSONDecodeError as exc:
        die(f"tokens.json is not valid JSON: {exc}")
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, list) or not tokens:
        die("tokens.json must contain a non-empty 'tokens' array")
    for i, token in enumerate(tokens):
        for field in ("symbol", "contract_address", "decimals", "coingecko_id", "chain", "track_balance"):
            if field not in token:
                die(f"tokens.json entry {i} missing required field: {field}")
        if not isinstance(token["track_balance"], bool):
            die(f"tokens.json entry {i}: track_balance must be boolean (got {token['track_balance']!r})")
    return tokens


def verify_ethereum_erc20_symbols(tokens: list[dict], alchemy_key: str) -> None:
    """For every Ethereum-mainnet ERC-20 token in tokens.json (track_balance true
    OR false), verify the on-chain symbol matches the declared symbol.

    WELL is on chain=base-mainnet and is naturally excluded by the chain filter,
    so no special-case carve-out is needed."""
    url = eth_connector.ALCHEMY_URL_TEMPLATE.format(key=alchemy_key)
    for token in tokens:
        if token["chain"] != CHAIN_ETHEREUM:
            continue
        if token["contract_address"].lower() == "native":
            continue
        # Print the (symbol, chain) context BEFORE the call. If verify_symbol
        # dies, this line is the last thing in stdout — triage-friendly.
        print(
            f"[verify] checking {token['symbol']} @ {token['chain']} "
            f"({token['contract_address']}), track_balance={token['track_balance']}"
        )
        eth_connector.verify_symbol(url, token["contract_address"].lower(), token["symbol"])
        print(f"[verify] Confirmed on-chain symbol for {token['symbol']} ({token['contract_address']})")


def fetch_prices(coingecko_ids: Iterable[str], api_key: str) -> dict[str, dict]:
    """Returns {coingecko_id: {"usd": float, "usd_24h_change": float|None}}."""
    ids = sorted({c for c in coingecko_ids if c})
    if not ids:
        return {}
    params = {
        "ids": ",".join(ids),
        "vs_currencies": "usd",
        "include_24hr_change": "true",
    }
    if api_key:
        # TODO(v2.1 cleanup): handle demo key vs pro key parameter correctly per
        # CoinGecko docs. Demo-tier keys use x_cg_demo_api_key against the public
        # endpoint; pro-tier keys use x_cg_pro_api_key against pro-api.coingecko.com.
        # Current code is half-wired and only honors pro keys at the wrong URL.
        params["x_cg_pro_api_key"] = api_key
    data = http_get_json(COINGECKO_PRICE_URL, "coingecko prices", params=params)
    return {cg_id: info for cg_id, info in data.items() if isinstance(info, dict)}


def _require_price(info: dict | None, label: str) -> tuple[float, float]:
    """Returns (price_usd, price_change_24h_percent), dies if either is bad.

    The 24h change requirement applies symmetrically to balance-tracked and
    price-only tokens — the dashboard's Vulture Rule and Legacy tiles both
    depend on this field; a silent null would cascade downstream."""
    if not info:
        die(f"missing CoinGecko price entry for {label}")
    assert info is not None
    usd = info.get("usd")
    if not isinstance(usd, (int, float)) or usd <= 0:
        die(f"missing/zero CoinGecko price for {label} (got {usd!r})")
    change = info.get("usd_24h_change")
    if not isinstance(change, (int, float)):
        die(f"missing/null 24h price change for {label} (got {change!r})")
    return float(usd), float(change)


def _internal_sanity(snapshot: dict) -> str:
    """Pre-write sanity check: schema shape + non-empty positions."""
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return f"could not load schema for sanity check: {exc}"
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(snapshot), key=lambda e: list(e.absolute_path))
    if errors:
        return "; ".join(
            f"{list(e.absolute_path) or '<root>'}: {e.message}" for e in errors[:5]
        )
    if not snapshot.get("positions"):
        return "positions array is empty"
    return ""


def _write_atomic(blob: str, now: datetime) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    latest = OUT_DIR / "positions.latest.json"
    tmp = OUT_DIR / "positions.latest.json.tmp"
    archive_ts = now.strftime("%Y%m%dT%H%M%SZ")
    archive = OUT_DIR / f"snap_{archive_ts}.json"

    try:
        tmp.write_text(blob)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        die(f"failed to write {tmp}: {exc}")

    try:
        tmp.replace(latest)
    except OSError as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        die(f"failed to atomically rename {tmp} to {latest}: {exc}")
    print(f"Wrote {latest} ({len(blob)} bytes) via atomic rename")

    try:
        shutil.copyfile(latest, archive)
        print(f"Wrote archive {archive}")
    except OSError as exc:
        print(
            f"WARNING: archive copy to {archive} failed: {exc} "
            f"(latest is correct; archive can be regenerated)",
            file=sys.stderr,
        )


def main() -> int:
    alchemy_key = env_required("ALCHEMY_ETH_KEY")
    evm_raw = env_required("EVM_ADDRESSES")
    btc_raw = env_required("BTC_ADDRESSES")
    sol_raw = env_required("SOL_ADDRESSES")
    coingecko_key = os.environ.get("COINGECKO_API_KEY", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    tokens = load_tokens()
    balance_tokens = [t for t in tokens if t["track_balance"]]
    price_only_tokens = [t for t in tokens if not t["track_balance"]]

    print(
        f"DRY_RUN={dry_run} | Tokens: {len(tokens)} "
        f"(onchain={len(balance_tokens)}, price_only={len(price_only_tokens)})"
    )

    verify_ethereum_erc20_symbols(tokens, alchemy_key)

    by_chain: dict[str, list[dict]] = {}
    for t in balance_tokens:
        by_chain.setdefault(t["chain"], []).append(t)

    evm_addrs = eth_connector.parse_addresses(evm_raw)
    btc_addrs = btc_connector.parse_addresses(btc_raw)
    sol_addrs = sol_connector.parse_addresses(sol_raw)
    print(
        f"Wallets: EVM={len(evm_addrs)} BTC={len(btc_addrs)} SOL={len(sol_addrs)} | "
        f"Chains dispatched: {sorted(by_chain.keys())}"
    )

    raw_positions: list[dict] = []
    if CHAIN_ETHEREUM in by_chain:
        raw_positions.extend(
            eth_connector.fetch_positions(evm_addrs, by_chain[CHAIN_ETHEREUM], alchemy_key)
        )
    if CHAIN_BITCOIN in by_chain:
        raw_positions.extend(
            btc_connector.fetch_positions(btc_addrs, by_chain[CHAIN_BITCOIN])
        )
    if CHAIN_SOLANA in by_chain:
        raw_positions.extend(
            sol_connector.fetch_positions(sol_addrs, by_chain[CHAIN_SOLANA], alchemy_key)
        )

    held = [rp for rp in raw_positions if rp["qty"] > 0]
    if not held:
        die("no non-zero balance-tracked positions found across all chains")

    needed_ids = sorted({t["coingecko_id"] for t in tokens})
    price_info = fetch_prices(needed_ids, coingecko_key)

    onchain_priced: list[tuple[dict, float, float]] = []
    for rp in held:
        label = f"held token {rp['symbol']}@{rp['chain']} (coingecko {rp['coingecko_id']}, wallet {rp['wallet_address']})"
        price, change = _require_price(price_info.get(rp["coingecko_id"]), label)
        onchain_priced.append((rp, price, change))

    price_only_priced: list[tuple[dict, float, float]] = []
    for t in price_only_tokens:
        label = f"price-only token {t['symbol']}@{t['chain']} (coingecko {t['coingecko_id']})"
        price, change = _require_price(price_info.get(t["coingecko_id"]), label)
        price_only_priced.append((t, price, change))

    positions: list[dict] = []
    for rp, price, change in onchain_priced:
        nav = rp["qty"] * price
        positions.append({
            "symbol": rp["symbol"],
            "chain": rp["chain"],
            "qty": rp["qty"],
            "decimals": rp["decimals"],
            "contract_address": rp["contract_address"],
            "price_usd": price,
            "price_change_24h_percent": change,
            "nav_usd": nav,
            "weight": 0.0,
            "source": rp["source"],
            "source_priority": "onchain",
            "wallet_address": rp["wallet_address"],
        })

    for t, price, change in price_only_priced:
        contract = t["contract_address"].lower() if t["contract_address"] != "native" else "native"
        positions.append({
            "symbol": t["symbol"],
            "chain": t["chain"],
            "qty": None,
            "decimals": t["decimals"],
            "contract_address": contract,
            "price_usd": price,
            "price_change_24h_percent": change,
            "nav_usd": None,
            "weight": None,
            "source": "coingecko",
            "source_priority": "price_only",
            "wallet_address": None,
        })

    total_nav = sum(p["nav_usd"] for p in positions if p["nav_usd"] is not None)
    if total_nav <= 0:
        die(f"total_nav_usd <= 0 (got {total_nav})")
    for p in positions:
        if p["source_priority"] == "onchain":
            p["weight"] = p["nav_usd"] / total_nav

    now = datetime.now(timezone.utc)
    snapshot = {
        "run_id": str(uuid.uuid4()),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": SCHEMA_VERSION,
        "total_nav_usd": total_nav,
        "positions": positions,
        "metadata": {
            "wallets": {
                CHAIN_ETHEREUM: evm_addrs,
                CHAIN_BITCOIN: btc_addrs,
                CHAIN_SOLANA: sol_addrs,
            },
            "tokens_tracked": [
                {"symbol": t["symbol"], "chain": t["chain"], "track_balance": t["track_balance"]}
                for t in tokens
            ],
            "chains_onchain": sorted(by_chain.keys()),
            "data_sources": ["alchemy", "blockstream", "coingecko"],
            "dry_run": dry_run,
        },
    }

    blob = json.dumps(snapshot, indent=2)

    sanity_err = _internal_sanity(snapshot)
    if sanity_err:
        die(f"internal sanity check failed before write: {sanity_err}")

    if dry_run:
        print("[DRY_RUN] would have written:")
        print(blob)
        onchain_count = sum(1 for p in positions if p["source_priority"] == "onchain")
        price_only_count = sum(1 for p in positions if p["source_priority"] == "price_only")
        print(
            f"[DRY_RUN] total_nav_usd={total_nav:.2f} positions={len(positions)} "
            f"(onchain={onchain_count}, price_only={price_only_count})"
        )
        return 0

    _write_atomic(blob, now)
    onchain_count = sum(1 for p in positions if p["source_priority"] == "onchain")
    price_only_count = sum(1 for p in positions if p["source_priority"] == "price_only")
    print(
        f"total_nav_usd={total_nav:.2f} positions={len(positions)} "
        f"(onchain={onchain_count}, price_only={price_only_count})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
