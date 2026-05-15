#!/usr/bin/env python3
"""
Snapshotter v1 — fail-loud on-chain NAV snapshot for retention-os.

Reads wallet addresses from EVM_ADDRESSES (comma-separated) and the token
allowlist from tokens.json. For each (wallet, token) pair, fetches the on-chain
balance via Alchemy, verifies the ERC-20 symbol matches tokens.json, prices
each held token via CoinGecko, and writes a positions snapshot to
out/snapshots/.

Environment:
  ALCHEMY_ETH_KEY   required
  EVM_ADDRESSES     required, comma-separated lowercase 0x-prefixed addresses
  COINGECKO_API_KEY optional (public endpoint works without)
  DRY_RUN           if "true"/"1"/"yes", logs intended output and skips writes
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS_PATH = REPO_ROOT / "tokens.json"
OUT_DIR = REPO_ROOT / "out" / "snapshots"
SCHEMA_VERSION = "1.0.0"
ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}$")
SYMBOL_SELECTOR = "0x95d89b41"
ALCHEMY_URL_TEMPLATE = "https://eth-mainnet.g.alchemy.com/v2/{key}"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
HTTP_RETRIES = 3
HTTP_TIMEOUT = 30


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        die(f"missing or empty environment variable: {name}")
    return value


def parse_addresses(raw: str) -> list[str]:
    addrs: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        addr = piece.strip().lower()
        if not addr:
            continue
        if not ADDRESS_RE.match(addr):
            die(f"invalid EVM address: {piece!r} (must match {ADDRESS_RE.pattern})")
        if addr not in seen:
            seen.add(addr)
            addrs.append(addr)
    if not addrs:
        die("EVM_ADDRESSES contained no valid addresses")
    return addrs


def _retryable_status(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def http_post_json(url: str, payload: dict, label: str) -> dict:
    backoff = 1.0
    last_err = "unknown"
    for attempt in range(HTTP_RETRIES):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
            if _retryable_status(resp.status_code):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                die(f"{label} RPC error: {data['error']}")
            return data
        except requests.RequestException as exc:
            last_err = f"request failed: {exc}"
            time.sleep(backoff)
            backoff *= 2
    die(f"{label} failed after {HTTP_RETRIES} attempts: {last_err}")
    return {}  # unreachable, keeps type checkers happy


def http_get_json(url: str, params: dict, label: str) -> dict:
    backoff = 1.0
    last_err = "unknown"
    for attempt in range(HTTP_RETRIES):
        try:
            resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            if _retryable_status(resp.status_code):
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_err = f"request failed: {exc}"
            time.sleep(backoff)
            backoff *= 2
    die(f"{label} failed after {HTTP_RETRIES} attempts: {last_err}")
    return {}


def alchemy_call(url: str, method: str, params: list, label: str) -> dict:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = http_post_json(url, payload, f"alchemy {method} ({label})")
    if "result" not in data:
        die(f"alchemy {method} ({label}) returned no result: {data}")
    return data


def decode_abi_string(hex_data: str) -> str:
    """Decode an ABI-encoded string return value. Handles dynamic string and bytes32."""
    if hex_data.startswith("0x"):
        hex_data = hex_data[2:]
    if not hex_data:
        return ""
    try:
        raw = bytes.fromhex(hex_data)
    except ValueError:
        return ""
    if len(raw) >= 64:
        offset = int.from_bytes(raw[0:32], "big")
        if offset == 0x20:
            length = int.from_bytes(raw[32:64], "big")
            if 0 < length <= len(raw) - 64:
                try:
                    return raw[64:64 + length].decode("utf-8")
                except UnicodeDecodeError:
                    pass
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", errors="replace")
    return ""


def verify_symbol(url: str, contract: str, expected: str) -> None:
    data = alchemy_call(
        url, "eth_call",
        [{"to": contract, "data": SYMBOL_SELECTOR}, "latest"],
        f"symbol() {contract}",
    )
    decoded = decode_abi_string(data["result"]).strip().rstrip("\x00")
    if decoded != expected:
        die(
            f"symbol mismatch for {contract}: on-chain returned {decoded!r}, "
            f"tokens.json declares {expected!r}"
        )


def fetch_eth_balance(url: str, wallet: str) -> int:
    data = alchemy_call(url, "eth_getBalance", [wallet, "latest"], f"eth balance {wallet}")
    return int(data["result"], 16)


def fetch_token_balances(url: str, wallet: str, contracts: list[str]) -> dict[str, int]:
    """Returns {contract_address_lower: raw_integer_balance}."""
    data = alchemy_call(
        url, "alchemy_getTokenBalances", [wallet, contracts],
        f"token balances {wallet}",
    )
    result = data["result"]
    out: dict[str, int] = {}
    for entry in result.get("tokenBalances", []):
        contract = entry["contractAddress"].lower()
        raw_hex = entry.get("tokenBalance") or "0x0"
        if entry.get("error"):
            die(f"alchemy token balance error for {contract} on {wallet}: {entry['error']}")
        try:
            out[contract] = int(raw_hex, 16)
        except (ValueError, TypeError):
            out[contract] = 0
    return out


def fetch_prices(coingecko_ids: Iterable[str], api_key: str) -> dict[str, float]:
    ids = sorted({c for c in coingecko_ids if c})
    if not ids:
        return {}
    params = {"ids": ",".join(ids), "vs_currencies": "usd"}
    if api_key:
        params["x_cg_pro_api_key"] = api_key
    data = http_get_json(COINGECKO_PRICE_URL, params, "coingecko prices")
    prices: dict[str, float] = {}
    for cg_id, info in data.items():
        if not isinstance(info, dict):
            continue
        usd = info.get("usd")
        if isinstance(usd, (int, float)) and usd > 0:
            prices[cg_id] = float(usd)
    return prices


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
        for field in ("symbol", "contract_address", "decimals", "coingecko_id", "chain"):
            if field not in token:
                die(f"tokens.json entry {i} missing required field: {field}")
    return tokens


def main() -> int:
    alchemy_key = env_required("ALCHEMY_ETH_KEY")
    raw_addrs = env_required("EVM_ADDRESSES")
    coingecko_key = os.environ.get("COINGECKO_API_KEY", "").strip()
    dry_run = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")

    wallets = parse_addresses(raw_addrs)
    tokens = load_tokens()
    alchemy_url = ALCHEMY_URL_TEMPLATE.format(key=alchemy_key)

    erc20_tokens = [t for t in tokens if t["contract_address"].lower() != "native"]
    erc20_contracts = [t["contract_address"].lower() for t in erc20_tokens]

    print(f"Wallets: {len(wallets)} | Tokens: {len(tokens)} (ERC-20: {len(erc20_tokens)}) | DRY_RUN={dry_run}")

    for token in erc20_tokens:
        verify_symbol(alchemy_url, token["contract_address"].lower(), token["symbol"])
        print(f"Verified on-chain symbol for {token['symbol']} ({token['contract_address']})")

    raw_positions = []
    for wallet in wallets:
        erc20_balances = (
            fetch_token_balances(alchemy_url, wallet, erc20_contracts)
            if erc20_contracts else {}
        )
        for token in tokens:
            contract = token["contract_address"].lower()
            if contract == "native":
                raw_balance = fetch_eth_balance(alchemy_url, wallet)
            else:
                raw_balance = erc20_balances.get(contract, 0)
            qty = raw_balance / (10 ** token["decimals"])
            raw_positions.append({
                "wallet_address": wallet,
                "token": token,
                "qty": qty,
            })

    held = [rp for rp in raw_positions if rp["qty"] > 0]
    if not held:
        die("no non-zero positions found across all wallets")

    needed_ids = sorted({rp["token"]["coingecko_id"] for rp in held})
    prices = fetch_prices(needed_ids, coingecko_key)
    for rp in held:
        cg = rp["token"]["coingecko_id"]
        if cg not in prices or prices[cg] <= 0:
            die(
                f"missing/zero price for held token {rp['token']['symbol']} "
                f"(coingecko id: {cg}, wallet: {rp['wallet_address']})"
            )

    positions = []
    for rp in held:
        token = rp["token"]
        price = prices[token["coingecko_id"]]
        nav = rp["qty"] * price
        positions.append({
            "symbol": token["symbol"],
            "qty": rp["qty"],
            "decimals": token["decimals"],
            "contract_address": token["contract_address"].lower(),
            "price_usd": price,
            "nav_usd": nav,
            "weight": 0.0,
            "source": "alchemy+coingecko",
            "source_priority": "onchain",
            "wallet_address": rp["wallet_address"],
        })

    total_nav = sum(p["nav_usd"] for p in positions)
    if total_nav <= 0:
        die(f"total_nav_usd <= 0 (got {total_nav})")
    for p in positions:
        p["weight"] = p["nav_usd"] / total_nav

    now = datetime.now(timezone.utc)
    snapshot = {
        "run_id": str(uuid.uuid4()),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_version": SCHEMA_VERSION,
        "total_nav_usd": total_nav,
        "positions": positions,
        "metadata": {
            "wallets": wallets,
            "tokens_tracked": [t["symbol"] for t in tokens],
            "chain": "ethereum-mainnet",
            "data_sources": ["alchemy", "coingecko"],
            "dry_run": dry_run,
        },
    }

    blob = json.dumps(snapshot, indent=2)
    if dry_run:
        print("[DRY_RUN] would have written:")
        print(blob)
        print(f"[DRY_RUN] total_nav_usd={total_nav:.2f} positions={len(positions)} wallets={len(wallets)}")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    latest = OUT_DIR / "positions.latest.json"
    archive_ts = now.strftime("%Y%m%dT%H%M%SZ")
    archive = OUT_DIR / f"snap_{archive_ts}.json"
    latest.write_text(blob)
    archive.write_text(blob)
    print(f"Wrote {latest} ({len(blob)} bytes)")
    print(f"Wrote {archive}")
    print(f"total_nav_usd={total_nav:.2f} positions={len(positions)} wallets={len(wallets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
