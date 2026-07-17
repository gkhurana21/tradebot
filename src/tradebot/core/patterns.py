"""Chart analysis — trend, structure, support/resistance, and patterns.

Turns a window of candles into a structured read a human trader would recognize:
  * trend direction + strength (regression slope, R^2, ADX, swing structure)
  * support / resistance levels (clustered swing pivots)
  * candlestick patterns (engulfing, hammer, shooting star, doji)
  * chart patterns (range breakout, double top / double bottom)

Everything is exposed as a `features()` dict so the Claude brain can reason over
recognized patterns, and a `summary()` string for humans. Pure standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import Candle


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------
def _linreg(ys: list[float]) -> tuple[float, float]:
    """Least-squares slope and R^2 of y vs its index. Slope is per-bar."""
    n = len(ys)
    if n < 2:
        return 0.0, 0.0
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r2 = (sxy ** 2) / (sxx * syy)
    return slope, r2


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder smoothing.
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(candles: list[Candle], period: int = 14) -> float | None:
    """Average Directional Index — trend strength 0..100 (regardless of side)."""
    if len(candles) < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        dn = candles[i - 1].low - candles[i].low
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
        h, l, pc = candles[i].high, candles[i].low, candles[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    def _smooth(xs):
        s = sum(xs[:period])
        out = [s]
        for x in xs[period:]:
            s = s - s / period + x
            out.append(s)
        return out

    str_ = _smooth(trs)
    sp = _smooth(plus_dm)
    sm = _smooth(minus_dm)
    dxs = []
    for tr, p, m in zip(str_, sp, sm):
        if tr == 0:
            continue
        pdi = 100 * p / tr
        mdi = 100 * m / tr
        denom = pdi + mdi
        if denom == 0:
            continue
        dxs.append(100 * abs(pdi - mdi) / denom)
    if len(dxs) < period:
        return None
    return sum(dxs[-period:]) / period


# --------------------------------------------------------------------------
# swing structure & levels
# --------------------------------------------------------------------------
def swing_points(candles: list[Candle], w: int = 3) -> tuple[list, list]:
    """Local swing highs and lows using a +/- w bar window. Returns (highs, lows)
    as lists of (index, price)."""
    highs, lows = [], []
    for i in range(w, len(candles) - w):
        window = candles[i - w:i + w + 1]
        hi = candles[i].high
        lo = candles[i].low
        if hi == max(c.high for c in window):
            highs.append((i, hi))
        if lo == min(c.low for c in window):
            lows.append((i, lo))
    return highs, lows


def cluster_levels(points: list, tol: float = 0.01) -> list[float]:
    """Group nearby pivot prices into support/resistance levels (tol = fraction)."""
    prices = sorted(p for _, p in points)
    levels, group = [], []
    for p in prices:
        if group and (p - group[0]) / group[0] > tol:
            levels.append(sum(group) / len(group))
            group = []
        group.append(p)
    if group:
        levels.append(sum(group) / len(group))
    return levels


# --------------------------------------------------------------------------
# candlestick patterns (evaluated on the last 1-2 candles)
# --------------------------------------------------------------------------
def candle_patterns(candles: list[Candle]) -> list[str]:
    if len(candles) < 2:
        return []
    out = []
    c, p = candles[-1], candles[-2]
    body = abs(c.close - c.open)
    rng = max(c.high - c.low, 1e-9)
    upper = c.high - max(c.close, c.open)
    lower = min(c.close, c.open) - c.low

    if body / rng < 0.1:
        out.append("doji")
    if lower > 2 * body and upper < body and c.close >= c.open:
        out.append("hammer")            # bullish reversal
    if upper > 2 * body and lower < body and c.close <= c.open:
        out.append("shooting_star")     # bearish reversal
    # Engulfing.
    p_body_up = p.close > p.open
    c_body_up = c.close > c.open
    if c_body_up and not p_body_up and c.close >= p.open and c.open <= p.close:
        out.append("bullish_engulfing")
    if not c_body_up and p_body_up and c.open >= p.close and c.close <= p.open:
        out.append("bearish_engulfing")
    return out


def chart_patterns(candles: list[Candle], highs: list, lows: list) -> list[str]:
    out = []
    if len(candles) < 20:
        return out
    recent = candles[-20:]
    hh = max(c.high for c in recent[:-1])
    ll = min(c.low for c in recent[:-1])
    last = candles[-1].close
    if last > hh:
        out.append("breakout_up")
    if last < ll:
        out.append("breakout_down")
    # Double top / bottom from the last two same-side pivots.
    if len(highs) >= 2:
        (_, a), (_, b) = highs[-2], highs[-1]
        if abs(a - b) / max(a, b) < 0.02 and last < min(a, b):
            out.append("double_top")
    if len(lows) >= 2:
        (_, a), (_, b) = lows[-2], lows[-1]
        if abs(a - b) / max(a, b) < 0.02 and last > max(a, b):
            out.append("double_bottom")
    return out


# --------------------------------------------------------------------------
# the combined read
# --------------------------------------------------------------------------
@dataclass
class ChartAnalysis:
    symbol: str
    price: float
    trend: str = "sideways"        # up | down | sideways
    trend_strength: float = 0.0    # 0..1 (from ADX)
    slope_pct: float = 0.0         # regression slope as % of price per bar
    r2: float = 0.0
    atr_pct: float = 0.0           # volatility as % of price
    support: list = field(default_factory=list)
    resistance: list = field(default_factory=list)
    candles: list = field(default_factory=list)   # candlestick pattern names
    chart: list = field(default_factory=list)     # chart pattern names

    def features(self) -> dict:
        return {
            "trend": self.trend,
            "trend_strength": round(self.trend_strength, 3),
            "slope_pct": round(self.slope_pct, 4),
            "r2": round(self.r2, 3),
            "atr_pct": round(self.atr_pct, 3),
            "nearest_support": self.support[-1] if self.support else None,
            "nearest_resistance": self.resistance[0] if self.resistance else None,
            "candlestick_patterns": self.candles,
            "chart_patterns": self.chart,
        }

    def summary(self) -> str:
        sup = f"{self.support[-1]:,.2f}" if self.support else "—"
        res = f"{self.resistance[0]:,.2f}" if self.resistance else "—"
        pats = ", ".join(self.candles + self.chart) or "none"
        return (
            f"{self.symbol} @ {self.price:,.2f}\n"
            f"  Trend:      {self.trend.upper()} (strength {self.trend_strength:.2f}, "
            f"R²={self.r2:.2f}, slope {self.slope_pct:+.3f}%/bar)\n"
            f"  Volatility: ATR {self.atr_pct:.2f}% of price\n"
            f"  Support:    {sup}   Resistance: {res}\n"
            f"  Patterns:   {pats}"
        )


def analyze(candles: list[Candle], trend_period: int = 50) -> ChartAnalysis:
    if not candles:
        raise ValueError("no candles to analyze")
    price = candles[-1].close
    ca = ChartAnalysis(symbol=candles[-1].symbol, price=price)

    window = candles[-trend_period:] if len(candles) >= trend_period else candles
    slope, r2 = _linreg([c.close for c in window])
    ca.slope_pct = slope / price * 100 if price else 0.0
    ca.r2 = r2
    a = adx(candles)
    ca.trend_strength = min(1.0, a / 50) if a is not None else 0.0
    at = atr(candles)
    ca.atr_pct = (at / price * 100) if at else 0.0

    # Direction: needs both a slope and enough trend strength / fit.
    strong = ca.trend_strength > 0.4 or r2 > 0.3
    if strong and ca.slope_pct > 0.01:
        ca.trend = "up"
    elif strong and ca.slope_pct < -0.01:
        ca.trend = "down"
    else:
        ca.trend = "sideways"

    highs, lows = swing_points(candles)
    res = [lv for lv in cluster_levels(highs) if lv > price]
    sup = [lv for lv in cluster_levels(lows) if lv < price]
    ca.resistance = sorted(res)[:3]
    ca.support = sorted(sup)[-3:]
    ca.candles = candle_patterns(candles)
    ca.chart = chart_patterns(candles, highs, lows)
    return ca
