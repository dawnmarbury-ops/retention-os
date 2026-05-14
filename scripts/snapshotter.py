import os
import json
from datetime import datetime
from pathlib import Path

# Create the output hanger
OUT_DIR = Path("out/snapshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

def main():
    print("--- ENGINE STARTING (DRY RUN) ---")
    
    # Create the data
    snapshot = {
        "run_id": f"dryrun-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}",
        "timestamp": datetime.utcnow().isoformat(),
        "total_nav_usd": 400000.00,
        "positions": [
            {"symbol": "BTC", "qty": 1.0, "price": 65000},
            {"symbol": "LYX", "qty": 1200, "price": 5.20}
        ],
        "metadata": {"status": "SUCCESS", "mode": "CI_DRY_RUN"}
    }
    
    # Save the files
    latest_path = OUT_DIR / "positions.latest.json"
    archive_name = f"snap_{snapshot['run_id']}.json"
    archive_path = OUT_DIR / archive_name
    
    with open(latest_path, "w") as f:
        json.dump(snapshot, f, indent=2)
    with open(archive_path, "w") as f:
        json.dump(snapshot, f, indent=2)
        
    print(f"Data package saved to: {latest_path}")
    print(f"Archive saved to: {archive_path}")
    print("--- ENGINE SHUTDOWN ---")

if __name__ == "__main__":
    main()
