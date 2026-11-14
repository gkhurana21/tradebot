"""A→B decision gate, ported from the polymarket bot's calibration design.

A stream is compared against its null_random control on closed-trade win
rate: two-proportion z-test, falling back to Fisher's exact test when any
contingency cell < 5. Verdicts:

    HALT                 — kill-switched, or significantly WORSE than null
    INCONCLUSIVE         — not enough data, or no significant edge
    CANDIDATE_FOR_LIVE   — all gates green AND beats null at p < threshold

CANDIDATE_FOR_LIVE means live trading may be DISCUSSED. Nothing here places
orders; live mode does not exist.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from scipy import stats

from bot.models import ClosedTrade, PaperFill
from bot.store import Store


def closed_trades_from_fills(fills: list[PaperFill]) -> list[ClosedTrade]:
    """Reconstruct round-trips per symbol (average-cost accounting).

    A trade opens when units leave zero and closes when they return to zero
    (kill-switch liquidations included). Costs are charged into pnl.
    """
    trades: list[ClosedTrade] = []
    state: dict[str, dict] = {}   # sym → {units, avg, entry_ts, realized}
    for f in sorted(fills, key=lambda x: (x.symbol, x.ts, x.id or 0)):
        s = state.setdefault(f.symbol, {"units": 0.0, "avg": 0.0,
                                        "entry_ts": 0, "realized": 0.0})
        old = s["units"]
        new = old + f.delta_units
        if old == 0.0:
            s.update(avg=f.price, entry_ts=f.ts, realized=-f.cost_usd)
        else:
            s["realized"] -= f.cost_usd
            same_side = (old > 0) == (new > 0) or new == 0.0
            if abs(new) < abs(old) or not same_side:
                closed_units = old - (new if same_side else 0.0)
                s["realized"] += (f.price - s["avg"]) * closed_units
            if abs(new) > abs(old) and same_side:
                s["avg"] = ((abs(old) * s["avg"] + abs(f.delta_units) * f.price)
                            / abs(new))
        if abs(new) < 1e-12 and old != 0.0:
            trades.append(ClosedTrade(f.stream, f.symbol, s["entry_ts"],
                                      f.ts, s["realized"]))
            s.update(units=0.0, avg=0.0, realized=0.0)
        else:
            if not ((old > 0) == (new > 0)) and old != 0.0 and new != 0.0:
                # flipped through zero: close old trade, open new lot
                trades.append(ClosedTrade(f.stream, f.symbol, s["entry_ts"],
                                          f.ts, s["realized"]))
                s.update(avg=f.price, entry_ts=f.ts, realized=0.0)
            s["units"] = new
    return trades


def compare_win_rates(wins_a: int, n_a: int, wins_b: int, n_b: int) -> dict:
    """Two-proportion z-test (Fisher's exact if any cell < 5). One-sided:
    H1 = stream A wins more often than stream B."""
    if n_a == 0 or n_b == 0:
        return {"p_value": 1.0, "method": "none", "gap": 0.0}
    p_a, p_b = wins_a / n_a, wins_b / n_b
    cells = [wins_a, n_a - wins_a, wins_b, n_b - wins_b]
    if min(cells) < 5:
        _, p = stats.fisher_exact([[wins_a, n_a - wins_a],
                                   [wins_b, n_b - wins_b]],
                                  alternative="greater")
        method = "fisher_exact"
    else:
        pooled = (wins_a + wins_b) / (n_a + n_b)
        se = math.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
        z = (p_a - p_b) / se if se > 0 else 0.0
        p = 1 - stats.norm.cdf(z)
        method = "two_proportion_z"
    return {"p_value": float(p), "method": method,
            "gap": round(p_a - p_b, 4)}


@dataclass(frozen=True)
class GateReport:
    stream: str
    verdict: str            # HALT | INCONCLUSIVE | CANDIDATE_FOR_LIVE
    checks: dict
    stats: dict


def evaluate_stream(store: Store, stream: str, null_stream: str,
                    gates_cfg: dict) -> GateReport:
    fills = store.fills_for_stream(stream)
    null_fills = store.fills_for_stream(null_stream)
    trades = closed_trades_from_fills(fills)
    null_trades = closed_trades_from_fills(null_fills)
    curve = store.equity_curve(stream)

    state = store.load_stream_state(stream) or {}
    if state.get("halted"):
        return GateReport(stream, "HALT", {"halted": True}, {})

    days = ((curve[-1].ts - curve[0].ts) / 86_400_000) if len(curve) >= 2 else 0
    max_dd = 0.0
    peak = float("-inf")
    for snap in curve:
        peak = max(peak, snap.equity_usd)
        if peak > 0:
            max_dd = min(max_dd, (snap.equity_usd - peak) / peak)

    wins = sum(t.won for t in trades)
    null_wins = sum(t.won for t in null_trades)
    test = compare_win_rates(wins, len(trades), null_wins, len(null_trades))

    checks = {
        "days": {"value": round(days, 1), "min": gates_cfg["min_paper_days"],
                 "pass": days >= gates_cfg["min_paper_days"]},
        "closed_trades": {"value": len(trades),
                          "min": gates_cfg["min_closed_trades"],
                          "pass": len(trades) >= gates_cfg["min_closed_trades"]},
        "max_drawdown": {"value": round(abs(max_dd), 4),
                         "max": gates_cfg["max_drawdown"],
                         "pass": abs(max_dd) <= gates_cfg["max_drawdown"]},
        "beats_null": {"p_value": round(test["p_value"], 4),
                       "threshold": gates_cfg["null_test_p_value"],
                       "win_rate_gap": test["gap"], "method": test["method"],
                       "pass": test["p_value"] < gates_cfg["null_test_p_value"]},
    }
    stats_out = {"wins": wins, "trades": len(trades),
                 "null_wins": null_wins, "null_trades": len(null_trades)}

    # significantly worse than null → halt-worthy finding
    reverse = compare_win_rates(null_wins, len(null_trades), wins, len(trades))
    if (len(trades) >= gates_cfg["min_closed_trades"]
            and reverse["p_value"] < gates_cfg["null_test_p_value"]):
        return GateReport(stream, "HALT", checks, stats_out)

    verdict = ("CANDIDATE_FOR_LIVE"
               if all(c["pass"] for c in checks.values())
               else "INCONCLUSIVE")
    return GateReport(stream, verdict, checks, stats_out)
