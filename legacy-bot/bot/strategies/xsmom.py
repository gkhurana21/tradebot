"""Cross-sectional momentum: hold the top-N symbols by trailing return,
equal-weighted, rebalanced every `rebalance_every_bars`."""

from __future__ import annotations

import pandas as pd

from bot.strategies.base import Strategy


class XSMom(Strategy):
    name = "xsmom"

    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        lookback = self._per_universe("lookback_bars")
        every = self._per_universe("rebalance_every_bars")
        top_n = self.params["top_n"]

        momentum = closes.pct_change(lookback)
        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        last = pd.Series(0.0, index=closes.columns)
        for i in range(len(closes)):
            if i >= lookback and i % every == 0:
                row = momentum.iloc[i].dropna()
                winners = row.nlargest(top_n)
                last = pd.Series(0.0, index=closes.columns)
                # only long positive momentum — don't buy the least-bad loser
                for sym, mom in winners.items():
                    if mom > 0:
                        last[sym] = 1.0 / top_n
            w.iloc[i] = last
        return self.clip_direction(w)
