"""Data feeds — all yield an iterable of Candle.

  * synthetic_candles: a seeded geometric-Brownian-motion generator with a
    trend + regime shifts, so backtests run offline and deterministically.
  * coinbase_candles: real historical OHLCV from Coinbase's public API
    (no key required). Used when online.
  * csv_candles: load a saved OHLCV CSV.
"""
from __future__ import annotations

import csv
import json
import math
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterator

from ..core.types import Candle


def synthetic_candles(
    symbol: str = "BTC-USD",
    n: int = 1000,
    start_price: float = 30_000.0,
    seed: int = 42,
    interval_min: int = 60,
) -> Iterator[Candle]:
    rng = random.Random(seed)
    price = start_price
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    drift = 0.0002
    vol = 0.01
    for i in range(n):
        # Occasional regime shift in drift to create real trends to trade.
        if i % 120 == 0:
            drift = rng.uniform(-0.0006, 0.0009)
            vol = rng.uniform(0.006, 0.02)
        ret = drift + vol * rng.gauss(0, 1)
        new_price = max(price * math.exp(ret), 1e-3)
        hi = max(price, new_price) * (1 + abs(rng.gauss(0, vol / 2)))
        lo = min(price, new_price) * (1 - abs(rng.gauss(0, vol / 2)))
        vol_traded = abs(rng.gauss(100, 30))
        yield Candle(ts, price, hi, lo, new_price, vol_traded, symbol)
        price = new_price
        ts += timedelta(minutes=interval_min)


_GRAN = {60: 60, 300: 300, 900: 900, 3600: 3600, 21600: 21600, 86400: 86400}


def coinbase_candles(
    symbol: str = "BTC-USD", granularity: int = 3600, limit: int = 300
) -> list[Candle]:
    """Fetch recent OHLCV from Coinbase Exchange public API (max ~300 bars)."""
    if granularity not in _GRAN:
        raise ValueError(f"granularity must be one of {sorted(_GRAN)}")
    url = f"https://api.exchange.coinbase.com/products/{symbol}/candles?granularity={granularity}"
    req = urllib.request.Request(url, headers={"User-Agent": "tradebot/0.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        rows = json.loads(resp.read().decode())
    # Coinbase returns [time, low, high, open, close, volume], newest first.
    rows.sort(key=lambda r: r[0])
    out = []
    for t, low, high, opn, close, vol in rows[-limit:]:
        out.append(
            Candle(datetime.fromtimestamp(t, tz=timezone.utc),
                   opn, high, low, close, vol, symbol)
        )
    return out


def coinbase_history(
    symbol: str, start: datetime, end: datetime, granularity: int = 3600
) -> list[Candle]:
    """Every bar in [start, end) from Coinbase's public API, paged 300 at a time.

    A fixed window gives the same bars on every run, unlike "the latest N",
    so results computed from it can be reproduced later.
    """
    if granularity not in _GRAN:
        raise ValueError(f"granularity must be one of {sorted(_GRAN)}")
    step = timedelta(seconds=granularity * 300)
    out: dict[datetime, Candle] = {}
    cursor = start
    while cursor < end:
        stop = min(cursor + step, end)
        url = (f"https://api.exchange.coinbase.com/products/{symbol}/candles"
               f"?granularity={granularity}&start={cursor.isoformat()}&end={stop.isoformat()}")
        req = urllib.request.Request(url, headers={"User-Agent": "tradebot/0.1"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode())
        for t, low, high, opn, close, vol in rows:
            ts = datetime.fromtimestamp(t, tz=timezone.utc)
            if start <= ts < end:
                out[ts] = Candle(ts, opn, high, low, close, vol, symbol)
        cursor = stop
    return [out[ts] for ts in sorted(out)]


def write_csv(candles: list[Candle], path: str) -> None:
    """Save candles in the format csv_candles reads back."""
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for c in candles:
            w.writerow([c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume])


def coinbase_latest(symbol: str = "BTC-USD", granularity: int = 60) -> Candle | None:
    """Most recent closed candle — for live polling loops."""
    bars = coinbase_candles(symbol, granularity=granularity, limit=2)
    return bars[-1] if bars else None


def make_synthetic_ticker(symbol: str = "BTC-USD", seed: int = 7, interval_min: int = 1):
    """A callable that yields a fresh synthetic candle each call — for demoing
    the live loop offline without hitting a real feed."""
    gen = synthetic_candles(symbol, n=10_000, seed=seed, interval_min=interval_min)

    def _next(sym: str) -> Candle | None:
        try:
            return next(gen)
        except StopIteration:
            return None
    return _next


def csv_candles(path: str, symbol: str) -> list[Candle]:
    """Load OHLCV from CSV with columns: ts,open,high,low,close,volume."""
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ts = row["ts"]
            dt = (datetime.fromisoformat(ts) if not ts.isdigit()
                  else datetime.fromtimestamp(int(ts), tz=timezone.utc))
            out.append(Candle(dt, float(row["open"]), float(row["high"]),
                              float(row["low"]), float(row["close"]),
                              float(row["volume"]), symbol))
    return out
