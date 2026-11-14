"""Strategy ABC.

Contract: weights(closes) returns a DataFrame aligned to `closes` where row t
holds the target portfolio weights decided AT THE CLOSE of bar t, computed
using data up to and including bar t only. Execution at bar t+1's open is the
engine's job, not the strategy's.

The no-lookahead property is testable: for any k,
    strategy.weights(closes).iloc[:k]  ==  strategy.weights(closes.iloc[:k])
(modulo NaN warm-up rows).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

# Bars per year for vol annualization, by (universe, timeframe).
BARS_PER_YEAR = {
    ("crypto", "1h"): 24 * 365,
    ("crypto", "4h"): 6 * 365,
    ("crypto", "1d"): 365,
    ("equities", "1d"): 252,
}


class Strategy(ABC):
    name: str

    def __init__(self, params: dict, universe: str, universe_cfg: dict):
        self.params = params
        self.universe = universe
        self.allow_short = bool(universe_cfg.get("allow_short", False))
        self.timeframe = universe_cfg["timeframe"]
        self.bars_per_year = BARS_PER_YEAR[(universe, self.timeframe)]

    def _per_universe(self, key: str):
        """Params like lookback_bars may be scalar or per-universe dicts."""
        v = self.params[key]
        return v[self.universe] if isinstance(v, dict) else v

    @abstractmethod
    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        """Target weights per symbol; NaN-free (0.0 = flat)."""

    def clip_direction(self, w: pd.DataFrame) -> pd.DataFrame:
        """Zero out shorts when the universe is long-only."""
        return w if self.allow_short else w.clip(lower=0.0)
