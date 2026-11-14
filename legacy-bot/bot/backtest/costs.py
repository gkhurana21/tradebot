"""Trading cost model. Deliberately pessimistic: every unit of turnover pays
fee + slippage. Round trip therefore costs 2 × (fee_bps + slippage_bps)."""

from __future__ import annotations


def per_side_cost(cfg: dict, universe: str) -> float:
    c = cfg["costs"][universe]
    return (c["fee_bps"] + c["slippage_bps"]) / 1e4
