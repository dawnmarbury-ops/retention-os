#!/usr/bin/env python3
"""
Safe snapshotter stub for CI dry-run.

Creates out/snapshots/positions.latest.json and an archive file.
Designed to always succeed in DRY_RUN mode and produce inspectable artifacts.
"""
import os
import json
import random
from datetime import datetime
from pathlib import Path

OUT_DIR = Path("out/snapshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

def make_dummy_positions():
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
            "qty": qty,
            "price_usd": price,
            "nav_usd": nav,
            "location": "sim"
        })
    for p in positions:
        p["weight"] = round(p["nav_usd"] / total, 12) if total else 0.0
    return positions, total

def main():
    print("Starting safe snapshotter stub.")
    positions, total = make_dummy_positions()
    snapshot = {
        "run_id": f"dryrun-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
        "generated_at": now_iso(),
        "total_nav_usd": round(total, 6),
        "positions": positions,
        "metadata": {"notes": "safe dry-run snapshot"}
    }
    latest = OUT_DIR / "positions.latest.json"
    archive = OUT_DIR / f"{snapshot['run_id']}.json"
    with open(latest, "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(archive, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"Written: {latest}")
    print(f"Archive: {archive}")
    print("Done.")

if __name__ == '__main__':
    main()
