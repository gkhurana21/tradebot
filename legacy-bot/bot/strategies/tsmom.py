"""Time-series momentum: sign of k-bar trailing return, vol-targeted size."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.strategies.base import Strategy


class TSMom(Strategy):
    name = "tsmom"

    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        lookback = self._per_universe("lookback_bars")
        vol_lb = self.params["vol_lookback_bars"]
        target_vol = self.params["target_vol_annual"]

        momentum = closes.pct_change(lookback)
        direction = np.sign(momentum)

        rets = closes.pct_change()
        realized_vol = rets.rolling(vol_lb).std() * np.sqrt(self.bars_per_year)
        # size each position so it contributes ~target_vol; cap leverage at 1x
        size = (target_vol / realized_vol).clip(upper=1.0)

        w = (direction * size).fillna(0.0)
        return self.clip_direction(w)
