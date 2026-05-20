"""Bitcoin mainnet connector — Blockstream Esplora API.

Single-address watch only (no xpub in v2). Address regexes per brief:
  - bech32: ^bc1[a-z0-9]{6,87}$
  - P2PKH:  ^1[a-km-zA-HJ-NP-Z1-9]{25,34}$
  - P2SH:   ^3[a-km-zA-HJ-NP-Z1-9]{25,34}$
"""
from __future__ import annotations

import re

from ._common import die, http_get_json

ESPLORA_URL = "https://blockstream.info/api/address/{address}"
CHAIN = "bitcoin-mainnet"
SATS_PER_BTC = 10 ** 8

BECH32_RE = re.compile(r"^bc1[a-z0-9]{6,87}$")
P2PKH_RE = re.compile(r"^1[a-km-zA-HJ-NP-Z1-9]{25,34}$")
P2SH_RE = re.compile(r"^3[a-km-zA-HJ-NP-Z1-9]{25,34}$")


def _is_valid_address(address: str) -> bool:
    return bool(
        BECH32_RE.match(address)
        or P2PKH_RE.match(address)
        or P2SH_RE.match(address)
    )


def parse_addresses(raw: str) -> list[str]:
    addrs: list[str] = []
    seen: set[str] = set()
    for piece in raw.split(","):
        addr = piece.strip()
        if not addr:
            continue
        if any(ch.isspace() for ch in addr):
            die(f"invalid BTC address (contains whitespace): {piece!r}")
        if not _is_valid_address(addr):
            die(f"invalid BTC address format: {piece!r}")
        if addr not in seen:
            seen.add(addr)
            addrs.append(addr)
    if not addrs:
        die("BTC_ADDRESSES contained no valid addresses")
    return addrs


def fetch_positions(addresses: list[str], tokens: list[dict]) -> list[dict]:
    """Return raw positions for bitcoin-mainnet (BTC native only).

    tokens must include exactly one entry with symbol=BTC, chain=bitcoin-mainnet."""
    btc_token = next((t for t in tokens if t["symbol"] == "BTC"), None)
    if btc_token is None:
        die("bitcoin connector: tokens.json must include a BTC entry for chain bitcoin-mainnet")

    raw_positions: list[dict] = []
    for wallet in addresses:
        url = ESPLORA_URL.format(address=wallet)
        data = http_get_json(url, f"blockstream address {wallet}")
        chain_stats = data.get("chain_stats")
        if not isinstance(chain_stats, dict):
            die(f"blockstream response missing chain_stats for {wallet}: {data}")
        for field in ("funded_txo_sum", "spent_txo_sum"):
            if field not in chain_stats:
                die(f"blockstream response missing chain_stats.{field} for {wallet}")
        funded = chain_stats["funded_txo_sum"]
        spent = chain_stats["spent_txo_sum"]
        if not isinstance(funded, int) or not isinstance(spent, int):
            die(
                f"blockstream chain_stats values not integers for {wallet}: "
                f"funded={funded!r}, spent={spent!r}"
            )
        confirmed_sats = funded - spent
        if confirmed_sats < 0:
            die(f"blockstream returned negative confirmed balance for {wallet}: {confirmed_sats} sats")
        qty = confirmed_sats / SATS_PER_BTC
        raw_positions.append({
            "wallet_address": wallet,
            "symbol": btc_token["symbol"],
            "chain": btc_token["chain"],
            "qty": qty,
            "decimals": btc_token["decimals"],
            "contract_address": "native",
            "coingecko_id": btc_token["coingecko_id"],
            "source": "blockstream+coingecko",
        })

    if not any(p["qty"] > 0 for p in raw_positions):
        die(f"no non-zero BTC positions across {len(addresses)} configured BTC address(es)")
    return raw_positions
