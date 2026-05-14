#!/usr/bin/env python3
"""
Minimal safe snapshotter stub for dry-run testing.

This does NOT call external APIs. It simulates a snapshot so you can test
the CI / DRY_RUN flow and produce the required artifacts.
Replace with the full snapshotter when ready.
"""
import os
import json
import time
from datetime import datetime
from pathlib import Path
import random

OUT_DIR = Path("out/snapshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def make_dummy_positions():
    # deterministic-ish ordering by symbol
    symbols = ["BTC", "ETH", "SOL", "HBAR", "LYX", "RSR", "WELL"]
    positions = []
    total = 0.0
    for s in symbols:
        qty = round(random.uniform(0.01, 2.0), 6)
        price = round(random.uniform(1.0, 40000.0) if s in ("BTC","ETH") else random.uniform(0.01, 10.0), 6)
        nav = round(qty * price, 6)
        total += nav
        positions.append({
            "symbol": s,
            "id": s.lower(),
            "qty": qty,
            "price_usd": price,
            "nav_usd": nav,
            "dirty": False,
            "wallet_address": None,
            "location": "sim"
        })
    # compute weights
    for p in positions:
        p["weight"] = round(p["nav_usd"] / total, 12) if total else 0.0
    return positions, total

def run_snapshot():
    rid = f"snap_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    positions, total_nav = make_dummy_positions()
    # small simulated coingecko metrics
    price_sources = {
        "coingecko": {
            "call_count": 10,
            "ms_per_call_avg": 150
        }
    }
    snapshot = {
        "run_id": rid,
        "generated_at": now_iso(),
        "total_nav_usd": round(total_nav, 6),
        "positions": positions,
        "price_sources": price_sources,
        "metadata": {
            "missing_prices": 0,
            "notes": "simulated dry-run snapshot"
        }
    }
    latest_path = OUT_DIR / "positions.latest.json"
    archive_path = OUT_DIR / f"{rid}.json"
    with open(latest_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(archive_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"[DRY_RUN] Would commit {latest_path} (dry-run={os.environ.get('DRY_RUN')})")
    print(f"Snapshot written: {latest_path} (archive: {archive_path})")
    return snapshot

if __name__ == "__main__":
    print("Starting minimal dry-run snapshotter (simulated).")
    snap = run_snapshot()
    print("Total NAV:", snap["total_nav_usd"])
    print("Positions:", len(snap["positions"]))
    print("Price sources:", snap["price_sources"])
    print("Done.")
