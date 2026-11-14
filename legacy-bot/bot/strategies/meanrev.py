"""Mean reversion: fade z-score extremes vs a rolling mean.

Enter when |z| >= entry_z (long when z <= -entry_z; short when z >= entry_z
and shorting is allowed). Exit when |z| <= exit_z. Fixed max_weight per name.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from bot.strategies.base import Strategy


class MeanRev(Strategy):
    name = "meanrev"

    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        lb = self.params["zscore_lookback_bars"]
        entry_z = self.params["entry_z"]
        exit_z = self.params["exit_z"]
        max_w = self.params["max_weight"]

        mean = closes.rolling(lb).mean()
        std = closes.rolling(lb).std()
        z = ((closes - mean) / std).replace([np.inf, -np.inf], np.nan)

        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        for sym in closes.columns:
            zs = z[sym].to_numpy()
            out = np.zeros(len(zs))
            pos = 0.0
            for i in range(len(zs)):
                zi = zs[i]
                if np.isnan(zi):
                    out[i] = pos
                    continue
                if pos == 0.0:
                    if zi <= -entry_z:
                        pos = max_w
                    elif zi >= entry_z and self.allow_short:
                        pos = -max_w
                elif abs(zi) <= exit_z:
                    pos = 0.0
                out[i] = pos
            w[sym] = out
        return self.clip_direction(w)
