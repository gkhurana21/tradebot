from __future__ import annotations

import numpy as np
import pandas as pd

from bot.backtest.costs import per_side_cost
from bot.backtest.engine import run_backtest
from bot.risk.sizing import apply_risk_caps
from bot.strategies.base import Strategy


class OneShotLong(Strategy):
    """Goes 100% long SYM0 at bar 10, flat at bar 20. For cost math."""
    name = "oneshot"

    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        w.iloc[10:20, 0] = 0.25   # within max_position_weight
        return w


def test_per_side_cost(cfg):
    assert per_side_cost(cfg, "crypto") == (26 + 10) / 1e4


def test_engine_charges_costs_on_turnover(closes, opens, cfg):
    strat = OneShotLong({}, "crypto", cfg["universes"]["crypto"])
    res = run_backtest(strat, closes, opens, cfg)
    net = res["net_returns"]
    c = per_side_cost(cfg, "crypto")
    # entry fill happens during bar 11 (weight in force), cost = 0.25 * c
    gross_11 = 0.25 * (opens.iloc[12, 0] / opens.iloc[11, 0] - 1)
    assert np.isclose(net.iloc[11], gross_11 - 0.25 * c, atol=1e-12)
    # flat stretch has zero return
    assert net.iloc[5] == 0.0
    # exit cost charged at bar 21
    gross_21 = 0.0
    assert np.isclose(net.iloc[21], gross_21 - 0.25 * c, atol=1e-12)


def test_risk_caps_clip_and_scale(cfg):
    w = pd.DataFrame({"A": [0.9, 0.2], "B": [-0.9, 0.2],
                      "C": [0.9, 0.2], "D": [-0.9, 0.2],
                      "E": [0.9, 0.2], "F": [-0.9, 0.2],
                      "G": [0.9, 0.2], "H": [-0.9, 0.2]})
    capped = apply_risk_caps(w, cfg["risk"])
    assert capped.abs().max().max() <= cfg["risk"]["max_position_weight"] + 1e-12
    assert (capped.abs().sum(axis=1)
            <= cfg["risk"]["max_gross_exposure"] + 1e-9).all()
    # second row: gross = 8 × 0.2 = 1.6 > 1.5 → scaled to 0.2 × 1.5/1.6
    assert np.allclose(capped.iloc[1], 0.2 * 1.5 / 1.6)

    under = pd.DataFrame({"A": [0.2], "B": [0.2], "C": [0.2], "D": [0.2]})
    # gross 0.8 is under every cap → untouched
    assert np.allclose(apply_risk_caps(under, cfg["risk"]), 0.2)
