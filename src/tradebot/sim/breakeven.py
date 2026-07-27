"""Break-even analysis: what win rate does a strategy actually need?

Every round trip pays fees and slippage twice -- once entering, once exiting.
That cost is fixed per trade, while the profit per trade scales with how far
price moves. So the smaller your target move, the larger the fraction of it
that costs eat, and the higher the win rate you need just to break even.

This is the arithmetic behind "trade as often as possible": frequency does not
multiply an edge, it multiplies a cost. Past a certain frequency the required
win rate exceeds 100% and the strategy is unprofitable no matter how good the
signal is.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CostModel:
    """Per-side trading costs, as fractions of notional."""
    fee_pct: float = 0.001        # 10 bps taker fee, typical retail crypto
    slippage_pct: float = 0.0005  # 5 bps adverse fill

    @property
    def round_trip(self) -> float:
        """Total cost of entering and exiting one position."""
        return 2 * (self.fee_pct + self.slippage_pct)


def breakeven_win_rate(target_pct: float, reward_risk: float,
                       costs: CostModel) -> float:
    """Win rate required to break even, as a fraction.

    For a strategy targeting `target_pct` with a `reward_risk` payoff ratio:
        p * (target * rr) - (1 - p) * target - cost = 0

    Returns a value > 1.0 when the trade is mathematically unprofitable --
    that is a real answer, not an error: no win rate can overcome the costs.
    """
    if target_pct <= 0:
        return float("inf")
    win = target_pct * reward_risk
    loss = target_pct
    denom = win + loss
    if denom <= 0:
        return float("inf")
    return (loss + costs.round_trip) / denom


def net_expectancy(win_rate: float, target_pct: float, reward_risk: float,
                   costs: CostModel) -> float:
    """Expected return per trade after costs, as a fraction of notional."""
    win = target_pct * reward_risk
    loss = target_pct
    return win_rate * win - (1 - win_rate) * loss - costs.round_trip


def frequency_table(win_rate: float, reward_risk: float, costs: CostModel,
                    targets: list[float] | None = None) -> list[dict]:
    """Compare strategy styles from scalping to position trading.

    Smaller targets mean more trades per unit of price movement -- that is what
    "trade more often" actually means in practice.
    """
    targets = targets or [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.10]
    rows = []
    for t in targets:
        need = breakeven_win_rate(t, reward_risk, costs)
        edge = net_expectancy(win_rate, t, reward_risk, costs)
        rows.append({
            "target_pct": t * 100,
            "breakeven_win_pct": need * 100,
            "achievable": need <= 1.0,
            "net_per_trade_pct": edge * 100,
            "cost_share_of_target_pct": (costs.round_trip / t) * 100,
        })
    return rows


def report(win_rate: float, reward_risk: float, costs: CostModel) -> str:
    rows = frequency_table(win_rate, reward_risk, costs)
    lines = [
        f"Cost model: {costs.fee_pct*1e4:.0f} bps fee + {costs.slippage_pct*1e4:.0f} bps "
        f"slippage per side = {costs.round_trip*100:.2f}% per round trip",
        f"Assumed edge: {win_rate*100:.0f}% win rate at {reward_risk:.2f}:1 reward/risk",
        "",
        f"  {'target move':>12}{'need to win':>13}{'costs eat':>11}"
        f"{'net/trade':>12}   verdict",
        "  " + "-" * 62,
    ]
    for r in rows:
        need = r["breakeven_win_pct"]
        need_s = f"{need:.1f}%" if need <= 999 else ">999%"
        verdict = "viable" if r["achievable"] else "IMPOSSIBLE"
        if r["achievable"] and r["net_per_trade_pct"] <= 0:
            verdict = "loses money"
        lines.append(
            f"  {r['target_pct']:>11.2f}%{need_s:>13}"
            f"{r['cost_share_of_target_pct']:>10.0f}%"
            f"{r['net_per_trade_pct']:>11.3f}%   {verdict}"
        )
    lines += [
        "",
        "Read the 'need to win' column: that is the win rate required just to",
        "break even at that target size. Frequent, small-target trading demands",
        "a win rate no strategy sustains -- the costs are simply larger than the",
        "move being captured. Fewer, larger-target trades is the only regime",
        "where a realistic edge survives contact with fees.",
    ]
    return "\n".join(lines)
