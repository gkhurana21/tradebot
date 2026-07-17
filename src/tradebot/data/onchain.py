"""On-chain signals — a data edge equities traders don't have.

Reads public Ethereum JSON-RPC (no API key) for macro-liquidity and
network-activity signals that lead price:

  * gas price       -> network congestion / demand for blockspace
  * stablecoin supply (USDT/USDC) -> dry powder entering/leaving crypto

These become `features` on a Signal so the Claude brain can weigh them.
Validated against the BlockchainQuery extension (USDT supply matches).
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

# Public, keyless RPC endpoints (fallbacks tried in order).
RPC_ENDPOINTS = [
    "https://ethereum-rpc.publicnode.com",
    "https://eth.llamarpc.com",
    "https://cloudflare-eth.com",
]

USDT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"  # 6 decimals
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"  # 6 decimals


@dataclass
class OnChainSnapshot:
    gas_gwei: float | None = None
    usdt_supply_b: float | None = None    # billions
    usdc_supply_b: float | None = None
    ok: bool = False

    def features(self) -> dict:
        return {
            "gas_gwei": self.gas_gwei,
            "usdt_supply_b": self.usdt_supply_b,
            "usdc_supply_b": self.usdc_supply_b,
            "stablecoin_supply_b": (
                round((self.usdt_supply_b or 0) + (self.usdc_supply_b or 0), 3)
                if self.ok else None
            ),
        }


def _rpc(method: str, params: list) -> dict | None:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                          "method": method, "params": params}).encode()
    for url in RPC_ENDPOINTS:
        try:
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "tradebot/0.1"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            if isinstance(data, dict) and "error" in data:
                continue  # JSON-RPC error (e.g. needs API key) -> next endpoint
            return data
        except Exception:
            continue
    return None


def _total_supply(token: str, decimals: int = 6) -> float | None:
    # ERC-20 totalSupply() selector 0x18160ddd.
    res = _rpc("eth_call", [{"to": token, "data": "0x18160ddd"}, "latest"])
    if not res or "result" not in res:
        return None
    try:
        return int(res["result"], 16) / (10 ** decimals)
    except (ValueError, TypeError):
        return None


def fetch_snapshot() -> OnChainSnapshot:
    snap = OnChainSnapshot()
    gas = _rpc("eth_gasPrice", [])
    if gas and "result" in gas:
        try:
            snap.gas_gwei = round(int(gas["result"], 16) / 1e9, 2)
        except (ValueError, TypeError):
            pass
    usdt = _total_supply(USDT)
    usdc = _total_supply(USDC)
    if usdt is not None:
        snap.usdt_supply_b = round(usdt / 1e9, 3)
    if usdc is not None:
        snap.usdc_supply_b = round(usdc / 1e9, 3)
    snap.ok = snap.gas_gwei is not None or usdt is not None
    return snap


if __name__ == "__main__":
    s = fetch_snapshot()
    print("On-chain snapshot:", s.features(), "ok=", s.ok)
