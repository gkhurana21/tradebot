"""Deterministic strategies + indicator helpers.

A strategy consumes candles and emits Signals. It also packs an indicator
`features` dict onto each signal so the brain (Claude) has structured context
to reason over instead of raw prices.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .types import Candle, Side, Signal


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def stdev(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    window = values[-period:]
    m = sum(window) / period
    return (sum((v - m) ** 2 for v in window) / period) ** 0.5


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class Strategy:
    """Base class. Override on_candle."""
    name = "base"

    def on_candle(self, candle: Candle) -> Signal | None:  # pragma: no cover
        raise NotImplementedError


@dataclass
class EmaRsiStrategy(Strategy):
    """EMA crossover gated by RSI. Trend-following with a momentum filter."""
    fast: int = 12
    slow: int = 26
    rsi_period: int = 14
    lookback: int = 200
    name: str = "ema_rsi"

    def __post_init__(self):
        self._closes: deque[float] = deque(maxlen=self.lookback)
        self._prev_fast: float | None = None
        self._prev_slow: float | None = None

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        closes = list(self._closes)
        fast = ema(closes, self.fast)
        slow = ema(closes, self.slow)
        r = rsi(closes, self.rsi_period)
        if fast is None or slow is None or r is None:
            return None

        side, reason, strength = Side.FLAT, "", 0.0
        crossed_up = (
            self._prev_fast is not None
            and self._prev_fast <= self._prev_slow
            and fast > slow
        )
        crossed_dn = (
            self._prev_fast is not None
            and self._prev_fast >= self._prev_slow
            and fast < slow
        )
        if crossed_up and r < 70:
            side, reason = Side.BUY, "ema fast crossed above slow, rsi not overbought"
            strength = min(1.0, (70 - r) / 40 + 0.4)
        elif crossed_dn and r > 30:
            side, reason = Side.SELL, "ema fast crossed below slow, rsi not oversold"
            strength = min(1.0, (r - 30) / 40 + 0.4)

        self._prev_fast, self._prev_slow = fast, slow

        if side == Side.FLAT:
            return None
        return Signal(
            ts=candle.ts,
            symbol=candle.symbol,
            side=side,
            strength=round(strength, 3),
            reason=reason,
            features={
                "close": round(candle.close, 6),
                "ema_fast": round(fast, 6),
                "ema_slow": round(slow, 6),
                "rsi": round(r, 2),
                "spread_pct": round((fast - slow) / slow * 100, 4),
                "strategy": self.name,
            },
        )


@dataclass
class BollingerReversion(Strategy):
    """Mean-reversion: fade moves that pierce the Bollinger bands.

    Buys when price closes below the lower band (oversold), sells when it
    closes above the upper band (overbought). Complements the trend-following
    EMA strategy — they tend to win in opposite regimes.
    """
    period: int = 20
    num_std: float = 2.0
    lookback: int = 200
    name: str = "bollinger"

    def __post_init__(self):
        self._closes: deque[float] = deque(maxlen=self.lookback)

    def on_candle(self, candle: Candle) -> Signal | None:
        self._closes.append(candle.close)
        closes = list(self._closes)
        mid = sma(closes, self.period)
        sd = stdev(closes, self.period)
        if mid is None or sd is None or sd == 0:
            return None
        upper = mid + self.num_std * sd
        lower = mid - self.num_std * sd
        z = (candle.close - mid) / sd

        side, reason = Side.FLAT, ""
        if candle.close < lower:
            side, reason = Side.BUY, "price below lower band; fade the drop"
        elif candle.close > upper:
            side, reason = Side.SELL, "price above upper band; fade the pop"
        if side == Side.FLAT:
            return None
        strength = min(1.0, abs(z) / (self.num_std + 1))
        return Signal(
            ts=candle.ts,
            symbol=candle.symbol,
            side=side,
            strength=round(strength, 3),
            reason=reason,
            features={
                "close": round(candle.close, 6),
                "bb_mid": round(mid, 6),
                "bb_upper": round(upper, 6),
                "bb_lower": round(lower, 6),
                "zscore": round(z, 3),
                "strategy": self.name,
            },
        )


@dataclass
class PatternStrategy(Strategy):
    """Trades chart patterns in the direction of the trend.

    Longs on bullish reversals / upside breakouts / double bottoms when the
    trend isn't fighting it; shorts the mirror image. Attaches the full chart
    read (trend, S/R, patterns) as features so the brain can reason over it.
    """
    lookback: int = 120
    name: str = "pattern"

    def __post_init__(self):
        self._buf: deque[Candle] = deque(maxlen=self.lookback)

    def on_candle(self, candle: Candle) -> Signal | None:
        from .patterns import analyze
        self._buf.append(candle)
        if len(self._buf) < 30:
            return None
        ca = analyze(list(self._buf))
        bull = {"hammer", "bullish_engulfing", "breakout_up", "double_bottom"}
        bear = {"shooting_star", "bearish_engulfing", "breakout_down", "double_top"}
        hits = set(ca.candles) | set(ca.chart)
        b, s = hits & bull, hits & bear

        side, reason = Side.FLAT, ""
        if b and ca.trend != "down":
            side, reason = Side.BUY, "bullish " + "/".join(sorted(b))
        elif s and ca.trend != "up":
            side, reason = Side.SELL, "bearish " + "/".join(sorted(s))
        if side == Side.FLAT:
            return None

        # Conviction: pattern count + trend agreement + trend strength.
        aligned = (side == Side.BUY and ca.trend == "up") or \
                  (side == Side.SELL and ca.trend == "down")
        strength = min(1.0, 0.4 + 0.15 * len(b | s) + (0.2 if aligned else 0.0)
                       + 0.2 * ca.trend_strength)
        feats = {"strategy": self.name, **ca.features()}
        return Signal(candle.ts, candle.symbol, side, round(strength, 3), reason, feats)


class EnsembleStrategy(Strategy):
    """Runs several strategies; emits a signal only when they agree on a side.

    Agreement is a powerful filter — it cuts trade count but lifts quality.
    Strength is the average of agreeing sub-strategies.
    """
    name = "ensemble"

    def __init__(self, *strategies: Strategy, require: int = 2):
        self.strategies = list(strategies)
        self.require = require

    def on_candle(self, candle: Candle) -> Signal | None:
        sigs = [s for st in self.strategies if (s := st.on_candle(candle))]
        if not sigs:
            return None
        buys = [s for s in sigs if s.side == Side.BUY]
        sells = [s for s in sigs if s.side == Side.SELL]
        winner = buys if len(buys) >= len(sells) else sells
        if len(winner) < self.require:
            return None
        side = winner[0].side
        strength = sum(s.strength for s in winner) / len(winner)
        feats = {"agree": len(winner), "votes": len(sigs), "strategy": self.name}
        for s in winner:
            feats.update({f"{s.features.get('strategy','?')}.{k}": v
                          for k, v in s.features.items() if k != "strategy"})
        return Signal(
            ts=candle.ts, symbol=candle.symbol, side=side,
            strength=round(min(strength, 1.0), 3),
            reason=" + ".join(f"{s.features.get('strategy','?')}:{s.reason}" for s in winner),
            features=feats,
        )
