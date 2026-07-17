"""Monte Carlo risk-of-ruin simulator.

Answers the honest version of "can I compound $X into a target?" by running
thousands of simulated traders with a given edge and bet-sizing, then reporting
how many actually reach the target versus how many go bust first.

Each trade is a Bernoulli outcome with your assumed win probability and
reward:risk ratio, sized as a fraction of current equity, net of fees, subject
to a broker minimum stake. This is the standard way quants stress-test a betting
scheme — the result is a distribution, not a promise.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class MCParams:
    start: float = 1.0
    target: float = 1_000_000.0
    win_prob: float = 0.55          # your assumed edge (0.5 = no edge)
    reward_risk: float = 1.0        # win pays reward_risk × the amount risked
    risk_frac: float = 0.5          # fraction of equity risked per trade
    fee_frac: float = 0.001         # fee as fraction of the amount risked
    min_stake: float = 1.0          # broker minimum tradable amount
    max_trades: int = 5_000
    ruin_frac: float = 0.5          # "bust" if equity falls below this × min_stake


@dataclass
class MCResult:
    n_traders: int
    p_target: float
    p_ruin: float
    p_stuck: float                  # neither hit target nor busted in max_trades
    median_final: float
    p10_final: float
    p90_final: float
    best_final: float
    median_trades_to_target: float | None

    def report(self, p: MCParams) -> str:
        def pct(x): return f"{x*100:.2f}%"
        lines = [
            f"Monte Carlo: {self.n_traders:,} simulated traders",
            f"  start ${p.start:g} → target ${p.target:,.0f}  "
            f"({p.target / p.start:,.0f}× = compounding goal)",
            f"  edge: win_prob={p.win_prob:.0%}, reward:risk={p.reward_risk:.2f}, "
            f"risk/trade={p.risk_frac:.0%}, fee={p.fee_frac:.1%}, "
            f"min_stake=${p.min_stake:g}",
            "",
            f"  reached target : {pct(self.p_target)}",
            f"  went bust      : {pct(self.p_ruin)}",
            f"  stuck (neither): {pct(self.p_stuck)}",
            "",
            f"  final equity  median ${self.median_final:,.2f}   "
            f"p10 ${self.p10_final:,.2f}   p90 ${self.p90_final:,.2f}",
            f"  luckiest run  ${self.best_final:,.2f}",
        ]
        if self.median_trades_to_target is not None:
            lines.append(f"  median trades to target (winners only): "
                         f"{self.median_trades_to_target:.0f}")
        return "\n".join(lines)


def simulate(p: MCParams, n_traders: int = 10_000, seed: int = 0) -> MCResult:
    rng = random.Random(seed)
    finals: list[float] = []
    n_target = n_ruin = n_stuck = 0
    trades_to_target: list[int] = []
    ruin_level = p.ruin_frac * p.min_stake

    for _ in range(n_traders):
        equity = p.start
        outcome = "stuck"
        for t in range(1, p.max_trades + 1):
            if equity >= p.target:
                outcome = "target"
                trades_to_target.append(t)
                break
            stake = equity * p.risk_frac
            # Can't place a bet below the broker minimum -> effectively stuck/bust.
            if stake < p.min_stake:
                if equity < p.min_stake:
                    outcome = "ruin"
                break
            fee = stake * p.fee_frac
            if rng.random() < p.win_prob:
                equity += stake * p.reward_risk - fee
            else:
                equity -= stake + fee
            if equity <= ruin_level:
                outcome = "ruin"
                break
        if outcome == "target":
            n_target += 1
        elif outcome == "ruin":
            n_ruin += 1
        else:
            n_stuck += 1
        finals.append(equity)

    finals.sort()

    def q(frac: float) -> float:
        i = min(len(finals) - 1, int(frac * len(finals)))
        return finals[i]

    return MCResult(
        n_traders=n_traders,
        p_target=n_target / n_traders,
        p_ruin=n_ruin / n_traders,
        p_stuck=n_stuck / n_traders,
        median_final=q(0.5),
        p10_final=q(0.10),
        p90_final=q(0.90),
        best_final=finals[-1],
        median_trades_to_target=(
            sorted(trades_to_target)[len(trades_to_target) // 2]
            if trades_to_target else None),
    )
