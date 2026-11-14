"""Dataclass models.

Conventions (borrowed from the polymarket bot):
  * Immutable observations are frozen=True; mutable state is not.
  * Timestamps are int milliseconds since the Unix epoch — never datetime.
  * Prices and USD amounts are float; weights are floats in [-1, 1].
  * `id` is None pre-insert and populated by Store on insert.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar for a symbol. ts is the bar OPEN time in ms epoch."""
    symbol: str
    timeframe: str      # e.g. "4h", "1d"
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class TargetWeight:
    """Strategy output: desired portfolio weight for one symbol at one bar.

    Emitted at bar close; the engine/paper trader fills at the NEXT bar's
    open. weight is a fraction of stream equity, sign = direction.
    """
    stream: str         # strategy stream name, e.g. "tsmom"
    universe: str       # "crypto" | "equities"
    symbol: str
    ts: int             # close time (ms) of the bar the signal was computed on
    weight: float


@dataclass
class PaperFill:
    """A simulated fill in the paper book. delta_units may be negative."""
    stream: str
    universe: str
    symbol: str
    ts: int             # fill time ms epoch
    price: float        # fill price before costs
    delta_units: float  # signed change in units held
    notional_usd: float # abs(delta_units) * price
    cost_usd: float     # fees + slippage charged on this fill
    reason: str         # "rebalance" | "kill_switch" | "signal"
    id: int | None = None


@dataclass
class EquitySnapshot:
    """Mark-to-market of one stream at one point in time."""
    stream: str
    ts: int
    equity_usd: float
    cash_usd: float
    gross_exposure: float   # sum |position notional| / equity
    n_positions: int
    id: int | None = None


@dataclass
class Position:
    """Current holding of one symbol in one stream (mutable)."""
    stream: str
    symbol: str
    units: float
    avg_price: float

    @property
    def is_flat(self) -> bool:
        return abs(self.units) < 1e-12


@dataclass(frozen=True)
class ClosedTrade:
    """A round-trip trade (position opened then fully closed), for gate stats."""
    stream: str
    symbol: str
    entry_ts: int
    exit_ts: int
    pnl_usd: float

    @property
    def won(self) -> bool:
        return self.pnl_usd > 0
