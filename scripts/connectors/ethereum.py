"""Ethereum mainnet connector — Alchemy ETH balance + ERC-20 token balances.

verify_symbol() is MOONWELL-class bug prevention: moved verbatim from v1
snapshotter, do not refactor."""
from __future__ import annotations

import re

from ._common import die, http_post_json

ADDRESS_RE = re.compile(r"^0x[a-f0-9]{40}$")
SYMBOL_SELECTOR = "0x95d89b41"
ALCHEMY_URL_TEMPLATE = "https://eth-mainnet.g.alchemy.com/v2/{key}"
CHAIN = "ethereum-mainnet"


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
    # MOONWELL-class bug prevention. Preserved verbatim from v1.
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


def fetch_positions(addresses: list[str], tokens: list[dict], alchemy_key: str) -> list[dict]:
    """Return raw positions (qty, no price) for balance-tracked tokens on ethereum-mainnet.

    Symbol verification is performed by the orchestrator before dispatch — it
    covers both balance-tracked and price-only ERC-20 entries (see v2.1)."""
    url = ALCHEMY_URL_TEMPLATE.format(key=alchemy_key)
    erc20_tokens = [t for t in tokens if t["contract_address"].lower() != "native"]
    erc20_contracts = [t["contract_address"].lower() for t in erc20_tokens]

    raw_positions: list[dict] = []
    for wallet in addresses:
        erc20_balances = (
            fetch_token_balances(url, wallet, erc20_contracts)
            if erc20_contracts else {}
        )
        for token in tokens:
            contract = token["contract_address"].lower()
            if contract == "native":
                raw_balance = fetch_eth_balance(url, wallet)
            else:
                raw_balance = erc20_balances.get(contract, 0)
            qty = raw_balance / (10 ** token["decimals"])
            raw_positions.append({
                "wallet_address": wallet,
                "symbol": token["symbol"],
                "chain": token["chain"],
                "qty": qty,
                "decimals": token["decimals"],
                "contract_address": contract,
                "coingecko_id": token["coingecko_id"],
                "source": "alchemy+coingecko",
            })
    return raw_positions
