"""Core domain types shared across the whole system.

Everything speaks in these dataclasses so the data feed, strategy, brain,
risk engine, broker, and backtester can be swapped independently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    FLAT = "FLAT"  # no position / close everything


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


@dataclass(frozen=True)
class Candle:
    """A single OHLCV bar."""
    ts: datetime          # bar open time (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str

    @staticmethod
    def utc(ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc)


@dataclass(frozen=True)
class Signal:
    """Raw output of a deterministic strategy, before Claude and risk."""
    ts: datetime
    symbol: str
    side: Side
    strength: float                 # 0..1 conviction from the strategy
    reason: str = ""
    features: dict = field(default_factory=dict)  # indicators for the brain


@dataclass(frozen=True)
class Decision:
    """What the brain (Claude) decides to do with a signal + context."""
    ts: datetime
    symbol: str
    side: Side
    confidence: float               # 0..1
    size_hint: float                # fraction of allowed size, 0..1
    rationale: str
    veto: bool = False              # brain can explicitly stand down


@dataclass(frozen=True)
class Order:
    ts: datetime
    symbol: str
    side: Side
    qty: float
    type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    tag: str = ""


@dataclass
class Position:
    symbol: str
    qty: float = 0.0                # signed: + long, - short
    avg_price: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-12

    def unrealized(self, price: float) -> float:
        return self.qty * (price - self.avg_price)


@dataclass
class Fill:
    ts: datetime
    symbol: str
    side: Side
    qty: float
    price: float
    fee: float
    tag: str = ""


@dataclass
class Account:
    cash: float
    equity: float
    positions: dict[str, Position] = field(default_factory=dict)

    def position(self, symbol: str) -> Position:
        return self.positions.setdefault(symbol, Position(symbol))
