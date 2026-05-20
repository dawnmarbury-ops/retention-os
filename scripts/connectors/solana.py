"""Solana mainnet connector — Alchemy Solana RPC, getBalance (native SOL only).

Address validation: full Base58 decode to a 32-byte public key. Charset-only or
length-only checks would let mistyped strings through. SPL tokens are out of
scope this sprint.
"""
from __future__ import annotations

import base58

from ._common import die, http_post_json

ALCHEMY_URL_TEMPLATE = "https://solana-mainnet.g.alchemy.com/v2/{key}"
CHAIN = "solana-mainnet"
LAMPORTS_PER_SOL = 10 ** 9
PUBKEY_BYTES = 32


def _is_valid_pubkey(address: str) -> bool:
    if any(ch.isspace() for ch in address):
        return False
    try:
        decoded = base58.b58decode(address)
    except ValueError:
        return False
    return len(decoded) == PUBKEY_BYTES


def parse_addresses(raw: str) -> list[str]:
    addrs: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        addr = piece.strip()
        if not addr:
            continue
        if not _is_valid_pubkey(addr):
            die(f"invalid Solana address (Base58 decode to 32-byte pubkey failed): {piece!r}")
        if addr not in seen:
            seen.add(addr)
            addrs.append(addr)
    if not addrs:
        die("SOL_ADDRESSES contained no valid addresses")
    return addrs


def fetch_positions(addresses: list[str], tokens: list[dict], alchemy_key: str) -> list[dict]:
    """Return raw positions for solana-mainnet (native SOL only).

    tokens must include exactly one entry with symbol=SOL, chain=solana-mainnet."""
    sol_token = next((t for t in tokens if t["symbol"] == "SOL"), None)
    if sol_token is None:
        die("solana connector: tokens.json must include a SOL entry for chain solana-mainnet")
    url = ALCHEMY_URL_TEMPLATE.format(key=alchemy_key)

    raw_positions: list[dict] = []
    for wallet in addresses:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]}
        data = http_post_json(url, payload, f"solana getBalance {wallet}")
        result = data.get("result")
        if not isinstance(result, dict) or "value" not in result:
            die(f"solana RPC missing result.value for {wallet}: {data}")
        lamports = result["value"]
        if not isinstance(lamports, int) or lamports < 0:
            die(f"solana balance not a non-negative integer for {wallet}: {lamports!r}")
        qty = lamports / LAMPORTS_PER_SOL
        raw_positions.append({
            "wallet_address": wallet,
            "symbol": sol_token["symbol"],
            "chain": sol_token["chain"],
            "qty": qty,
            "decimals": sol_token["decimals"],
            "contract_address": "native",
            "coingecko_id": sol_token["coingecko_id"],
            "source": "alchemy+coingecko",
        })

    if not any(p["qty"] > 0 for p in raw_positions):
        die(f"no non-zero SOL positions across {len(addresses)} configured SOL address(es)")
    return raw_positions
