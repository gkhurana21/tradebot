"""Null-random control: random direction per symbol at each rebalance,
deterministic given (seed, bar ts, symbol). Same costs, same execution path
as real strategies. Any strategy that can't beat this after costs is noise.
"""

from __future__ import annotations

import hashlib

import pandas as pd

from bot.strategies.base import Strategy


def _draw(seed: int, ts: int, symbol: str) -> float:
    h = hashlib.sha256(f"{seed}|{ts}|{symbol}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2**64  # uniform [0, 1)


class NullRandom(Strategy):
    name = "null_random"

    def weights(self, closes: pd.DataFrame) -> pd.DataFrame:
        seed = self.params["seed"]
        every = self._per_universe("rebalance_every_bars")
        n = len(closes.columns)
        per_name = 1.0 / n if n else 0.0

        w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
        last = pd.Series(0.0, index=closes.columns)
        for i in range(len(closes)):
            if i % every == 0:
                ts = int(closes.index[i])
                for sym in closes.columns:
                    u = _draw(seed, ts, sym)
                    if self.allow_short:
                        # thirds: long / flat / short
                        direction = 1.0 if u < 1/3 else (-1.0 if u < 2/3 else 0.0)
                    else:
                        direction = 1.0 if u < 0.5 else 0.0
                    last[sym] = direction * per_name
            w.iloc[i] = last
        return self.clip_direction(w)
