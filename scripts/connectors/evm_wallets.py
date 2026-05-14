# scripts/connectors/evm_wallets.py
import os
import logging
import requests

ETHERSCAN_API = "https://api.etherscan.io/api"

logging.basicConfig(level=logging.INFO)

def _fetch_eth_balance(address: str, apikey: str) -> float:
    params = {
        "module": "account",
        "action": "balance",
        "address": address,
        "tag": "latest",
        "apikey": apikey
    }
    try:
        r = requests.get(ETHERSCAN_API, params=params, timeout=10)
        if r.status_code != 200:
            logging.error("etherscan: HTTP %s for %s", r.status_code, address)
            return 0.0
        data = r.json()
        wei = int(data.get("result", 0))
        return wei / 1e18
    except Exception:
        logging.exception("etherscan: request failed for %s", address)
        return 0.0

def get_positions():
    """
    Called by snapshotter. Reads WALLET_ADDRESSES and ETHERSCAN_API_KEY from env.
    Returns a list of {symbol, qty, location, wallet_address} dicts.
    """
    apikey = os.environ.get("ETHERSCAN_API_KEY")
    addrs_raw = os.environ.get("WALLET_ADDRESSES", "")

    if not addrs_raw:
        logging.info("evm_wallets: no WALLET_ADDRESSES set")
        return []
    if not apikey:
        logging.info("evm_wallets: no ETHERSCAN_API_KEY set")
        return []

    results = []
    # keep only 0x-prefixed addresses for EVM
    addresses = [a.strip() for a in addrs_raw.split(",") if a.strip().startswith("0x")]
    for addr in addresses:
        qty = _fetch_eth_balance(addr, apikey)
        if qty and qty > 0:
            label = f"Wallet ({addr[:6]}...{addr[-4:]})"
            # OPTIONAL: if you want to label lovedawn.eth specially, add logic here
            results.append({
                "symbol": "ETH",
                "qty": qty,
                "location": label,
                "wallet_address": addr,
                "meta": {"source": "etherscan"}
            })
    return results
