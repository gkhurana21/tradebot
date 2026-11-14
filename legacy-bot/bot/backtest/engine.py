"""Vectorized backtest engine.

Execution model (no lookahead):
  * Strategy decides weights at the CLOSE of bar t.
  * The order fills at the OPEN of bar t+1.
  * The position is held from open(t+1) until the next fill at open(t+2).

So with fwd_oret[t] = open(t+1)/open(t) - 1 (the return earned DURING bar t
by capital deployed at its open), the weights in force during bar t are
w.shift(1), and:

    gross[t] = Σ_symbols  w[t-1, s] · fwd_oret[t, s]
    cost[t]  = Σ_symbols |w[t-1, s] − w[t-2, s]| · per_side_cost
    net[t]   = gross[t] − cost[t]

Turnover ignores intra-position drift — a small, conservative simplification.
"""

from __future__ import annotations

import pandas as pd

from bot.backtest.costs import per_side_cost
from bot.backtest.metrics import compute_metrics
from bot.risk.sizing import apply_risk_caps
from bot.strategies.base import Strategy


def run_backtest(strategy: Strategy, closes: pd.DataFrame,
                 opens: pd.DataFrame, cfg: dict) -> dict:
    """Returns {"net_returns": Series, "metrics": dict, "weights": DataFrame}."""
    w = strategy.weights(closes)
    w = apply_risk_caps(w, cfg["risk"])

    hold = w.shift(1).fillna(0.0)                    # in force during bar t
    fwd_oret = opens.shift(-1) / opens - 1           # earned during bar t

    gross = (hold * fwd_oret).sum(axis=1)
    turnover = hold.diff().abs().sum(axis=1).fillna(0.0)
    cost = turnover * per_side_cost(cfg, strategy.universe)
    net = (gross - cost).iloc[:-1]                   # last bar's fwd ret unknown

    metrics = compute_metrics(net, strategy.bars_per_year)
    bars_per_year = strategy.bars_per_year
    n = max(len(net), 1)
    metrics["turnover_annual"] = round(
        float(turnover.sum() / n * bars_per_year), 2)
    metrics["avg_gross_exposure"] = round(
        float(hold.abs().sum(axis=1).mean()), 3)
    return {"net_returns": net, "metrics": metrics, "weights": w}
