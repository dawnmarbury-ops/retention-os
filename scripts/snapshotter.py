#!/usr/bin/env python3
"""
Production-friendly snapshotter.

Behavior:
- Loads optional connectors from scripts/connectors/*.py
  Each connector should implement: get_positions() -> list[dict]
  Position dict example:
    {
      "symbol": "ETH",
      "qty": 0.123,
      "wallet_address": "0x123...",
      "location": "coinbase"  # free-form
    }

- If env vars for Coinbase exist, attempts a built-in Coinbase connector.
- Resolves prices via CoinGecko simple/price API (best-effort).
- Writes snapshots to out/snapshots/positions.latest.json and an archive file.

Environment:
- DRY_RUN (default "true") - safe: script won't attempt repo commits
- WALLET_ADDRESSES (optional) - comma separated; connectors are preferred
- COINBASE_API_KEY, COINBASE_API_SECRET, COINBASE_API_PASSPHRASE (optional)
- TIMEOUTS / retries included

Notes:
- For on-chain wallet balances (Phantom/Solana, Ledger/EVM): provide a connector
  file in scripts/connectors that talks to the correct RPC/API (small template below).
"""
from __future__ import annotations
import os
import sys
import json
import time
import hmac
import hashlib
import base64
import logging
import random
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any, Optional
import requests

# -----------------------
# Configuration
# -----------------------
OUT_DIR = Path("out/snapshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

COINGECKO_API = "https://api.coingecko.com/api/v3"
DEFAULT_TIMEOUT = 10
RETRY_COUNT = 3
SLEEP_BASE = 0.8

# Logging
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# -----------------------
# Utilities
# -----------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def retry_http_get(url: str, params: dict = None, headers: dict = None, tries: int = RETRY_COUNT) -> Optional[requests.Response]:
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=DEFAULT_TIMEOUT)
            if r.status_code == 200:
                return r
            # 429 or 5xx -> backoff and retry
            if r.status_code >= 500 or r.status_code == 429:
                logging.warning("HTTP %s for %s (try %d/%d)", r.status_code, url, i+1, tries)
            else:
                logging.debug("HTTP %s for %s (not retrying)", r.status_code, url)
                return r
        except requests.RequestException as e:
            logging.warning("Request error for %s: %s (try %d/%d)", url, e, i+1, tries)
        time.sleep(SLEEP_BASE * (1 + i) + random.random() * 0.2)
    return None

# -----------------------
# Connector loading
# -----------------------
def load_connectors(path: Path) -> List[Any]:
    connectors = []
    if not path.exists():
        return connectors
    for p in sorted(path.glob("*.py")):
        # skip __init__ if present
        if p.name.startswith("__"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            connectors.append((p.name, mod))
            logging.info("Loaded connector: %s", p.name)
        except Exception as e:
            logging.exception("Failed to load connector %s: %s", p.name, str(e))
    return connectors

# -----------------------
# Coinbase connector (basic)
# -----------------------
def coinbase_accounts_from_api(key: str, secret: str, passphrase: str) -> List[Dict[str, Any]]:
    """
    Minimal Coinbase Pro / Exchange REST API accounts listing.
    Returns list of items with 'currency' and 'available'/'balance' fields when possible.
    Note: Coinbase's APIs change; treat this helper as best-effort.
    """
    api_base = "https://api.exchange.coinbase.com"
    endpoint = "/accounts"
    url = api_base + endpoint
    method = "GET"
    timestamp = str(time.time())
    message = timestamp + method + endpoint
    signature = base64.b64encode(hmac.new(base64.b64decode(secret), message.encode(), hashlib.sha256).digest()).decode()
    headers = {
        "CB-ACCESS-KEY": key,
        "CB-ACCESS-SIGN": signature,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "CB-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
        "User-Agent": "retention-os-snapshotter/1.0"
    }
    r = retry_http_get(url, headers=headers)
    if not r:
        logging.error("Coinbase accounts fetch failed (no response).")
        return []
    if r.status_code != 200:
        logging.error("Coinbase accounts fetch failed: %s - %s", r.status_code, r.text[:400])
        return []
    try:
        accounts = r.json()
        positions = []
        for a in accounts:
            # sample a: {"id": "...", "currency": "BTC", "balance": "0.001", "available": "0.001","hold":"0.0"}
            balance = float(a.get("balance") or 0.0)
            if balance <= 0.0:
                continue
            positions.append({
                "symbol": a.get("currency"),
                "qty": balance,
                "location": "coinbase",
                "meta": {"id": a.get("id"), "available": a.get("available"), "hold": a.get("hold")}
            })
        return positions
    except Exception as e:
        logging.exception("JSON parse error from Coinbase: %s", e)
        return []

# -----------------------
# Price resolution (CoinGecko)
# -----------------------
def fetch_coingecko_token_map() -> Dict[str, str]:
    """
    Return a mapping from uppercase symbol -> coingecko id (best-effort).
    Cache locally in OUT_DIR for a short time to avoid repeated heavy queries.
    """
    cache_file = OUT_DIR / "coingecko_coin_list_cache.json"
    # refresh cache every 12 hours
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < 60 * 60 * 12:
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass

    url = f"{COINGECKO_API}/coins/list"
    r = retry_http_get(url, params={"include_platform": "false"})
    mapping = {}
    if r and r.status_code == 200:
        try:
            coins = r.json()
            for c in coins:
                symbol = (c.get("symbol") or "").upper()
                # prefer first-come mapping if duplicates exist
                if symbol and symbol not in mapping:
                    mapping[symbol] = c.get("id")
            cache_file.write_text(json.dumps(mapping))
            return mapping
        except Exception:
            logging.exception("Failed to parse coin list from coingecko")
    # fallback: small map
    fallback = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "HBAR": "hedera-hashgraph"}
    return fallback

def get_prices_usd(symbols: List[str]) -> Dict[str, float]:
    """
    Returns price_by_symbol: e.g. {"BTC": 65000.0}
    Uses CoinGecko simple/price batch calls using resolved IDs.
    """
    if not symbols:
        return {}
    tk_map = fetch_coingecko_token_map()
    id_to_symbols = {}
    ids = []
    for s in sorted(set(symbols)):
        s_up = s.upper()
        gid = tk_map.get(s_up)
        if not gid:
            logging.debug("No coingecko id for symbol %s", s)
            continue
        ids.append(gid)
        id_to_symbols[gid] = s_up

    if not ids:
        return {}

    # batch call
    url = f"{COINGECKO_API}/simple/price"
    params = {"ids": ",".join(ids), "vs_currencies": "usd"}
    r = retry_http_get(url, params=params)
    prices = {}
    if r and r.status_code == 200:
        try:
            data = r.json()
            for gid, info in data.items():
                sym = id_to_symbols.get(gid)
                if sym and isinstance(info, dict):
                    prices[sym] = float(info.get("usd") or 0.0)
        except Exception:
            logging.exception("Failed to parse prices")
    return prices

# -----------------------
# Main snapshot assembly
# -----------------------
def normalize_positions(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in raw:
        sym = (r.get("symbol") or r.get("ticker") or r.get("token") or "").upper()
        try:
            qty = float(r.get("qty") or r.get("balance") or r.get("amount") or 0.0)
        except Exception:
            qty = 0.0
        if not sym or qty <= 0:
            continue
        out.append({
            "symbol": sym,
            "qty": qty,
            "location": r.get("location") or r.get("source") or "unknown",
            "wallet_address": r.get("wallet_address"),
            "meta": r.get("meta", {})
        })
    return out

def write_snapshot(snapshot: Dict[str, Any]) -> None:
    latest = OUT_DIR / "positions.latest.json"
    run_id = snapshot.get("run_id") or f"snap-{int(time.time())}"
    archive = OUT_DIR / f"{run_id}.json"
    with open(latest, "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(archive, "w") as f:
        json.dump(snapshot, f, indent=2)
    logging.info("Wrote latest snapshot: %s (archive: %s)", latest, archive)

def build_snapshot(positions: List[Dict[str, Any]]) -> Dict[str, Any]:
    symbols = [p["symbol"] for p in positions]
    prices = get_prices_usd(symbols)
    total = 0.0
    for p in positions:
        price = prices.get(p["symbol"], 0.0)
        p["price_usd"] = price
        p["nav_usd"] = round(p["qty"] * price, 8)
        total += p["nav_usd"]
    # compute weights
    for p in positions:
        p["weight"] = round((p["nav_usd"] / total) if total else 0.0, 12)
    snapshot = {
        "run_id": f"snap_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": now_iso(),
        "total_nav_usd": round(total, 6),
        "positions": positions,
        "price_sources": {"coingecko": {"queried": True}},
        "metadata": {"notes": "generated by retention-os snapshotter"}
    }
    return snapshot

# -----------------------
# Entrypoint
# -----------------------
def main():
    logging.info("Starting snapshotter (retention-os)")
    dry_run = os.environ.get("DRY_RUN", "true").lower() in ("1", "true", "yes")
    logging.info("DRY_RUN=%s", dry_run)

    collected_raw: List[Dict[str, Any]] = []

    # 1) Load user connectors if present
    conns_path = Path("scripts/connectors")
    connectors = load_connectors(conns_path)
    for name, mod in connectors:
        try:
            if hasattr(mod, "get_positions"):
                logging.info("Running connector: %s", name)
                got = mod.get_positions()
                if isinstance(got, list):
                    collected_raw.extend(got)
                else:
                    logging.warning("Connector %s did not return a list", name)
            else:
                logging.warning("Connector %s has no get_positions()", name)
        except Exception:
            logging.exception("Connector %s failed", name)

    # 2) Coinbase fallback connector (if secrets are set)
    ckey = os.environ.get("COINBASE_API_KEY")
    csecret = os.environ.get("COINBASE_API_SECRET")
    cpass = os.environ.get("COINBASE_API_PASSPHRASE")
    if ckey and csecret and cpass:
        try:
            logging.info("Attempting Coinbase connector using env vars")
            c_positions = coinbase_accounts_from_api(ckey, csecret, cpass)
            collected_raw.extend(c_positions)
        except Exception:
            logging.exception("Coinbase connector failed")

    # 3) If WALLET_ADDRESSES is provided, add them as "manual" placeholders (connectors advised)
    wallets = os.environ.get("WALLET_ADDRESSES", "")
    if wallets:
        for addr in [x.strip() for x in wallets.split(",") if x.strip()]:
            collected_raw.append({
                "symbol": "UNKNOWN",
                "qty": 0.0,
                "location": "wallet_placeholder",
                "wallet_address": addr,
                "meta": {"note": "add on-chain connector to fetch actual balances"}
            })

    # 4) Normalize and build snapshot
    positions = normalize_positions(collected_raw)
    if not positions:
        logging.warning("No positions collected. Writing empty snapshot (use connectors or set COINBASE envs).")

    snapshot = build_snapshot(positions)

    write_snapshot(snapshot)

    # print summary
    logging.info("Snapshot run_id=%s total_nav=%.2f positions=%d", snapshot["run_id"], snapshot["total_nav_usd"], len(snapshot["positions"]))
    logging.info("Done.")

if __name__ == "__main__":
    main()
