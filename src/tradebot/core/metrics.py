"""Performance analytics — the numbers that tell you if a strategy is real.

Computes risk-adjusted returns and trade statistics from an equity curve and
a round-trip trade ledger. All pure functions, no dependencies.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .types import Side


@dataclass
class Trade:
    """A completed round trip (entry -> exit)."""
    symbol: str
    side: Side              # direction of the entry
    entry_ts: datetime
    exit_ts: datetime
    entry_price: float
    exit_price: float
    qty: float
    pnl: float              # net of fees
    fees: float
    reason_exit: str = ""

    @property
    def return_pct(self) -> float:
        notional = self.entry_price * self.qty
        return (self.pnl / notional * 100) if notional else 0.0

    @property
    def won(self) -> bool:
        return self.pnl > 0


@dataclass
class Metrics:
    total_return_pct: float = 0.0
    cagr_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    volatility_pct: float = 0.0
    n_trades: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    expectancy: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    extras: dict = field(default_factory=dict)

    def table(self) -> str:
        rows = [
            ("Total return", f"{self.total_return_pct:+.2f}%"),
            ("CAGR", f"{self.cagr_pct:+.2f}%"),
            ("Max drawdown", f"{self.max_drawdown_pct:.2f}%"),
            ("Sharpe", f"{self.sharpe:.2f}"),
            ("Sortino", f"{self.sortino:.2f}"),
            ("Calmar", f"{self.calmar:.2f}"),
            ("Volatility (ann.)", f"{self.volatility_pct:.2f}%"),
            ("Trades", f"{self.n_trades}"),
            ("Win rate", f"{self.win_rate_pct:.1f}%"),
            ("Profit factor", f"{self.profit_factor:.2f}"),
            ("Expectancy/trade", f"{self.expectancy:+.2f}"),
            ("Avg win / loss", f"{self.avg_win:+.2f} / {self.avg_loss:+.2f}"),
            ("Best / worst", f"{self.best_trade:+.2f} / {self.worst_trade:+.2f}"),
        ]
        w = max(len(k) for k, _ in rows)
        return "\n".join(f"  {k.ljust(w)} : {v}" for k, v in rows)


def _returns(equity: list[float]) -> list[float]:
    out = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            out.append(equity[i] / prev - 1)
    return out


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def max_drawdown_pct(equity: list[float]) -> float:
    peak, mdd = -float("inf"), 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd * 100


def compute(
    equity: list[float],
    trades: list[Trade],
    bars_per_year: float = 24 * 365,   # hourly crypto bars by default
) -> Metrics:
    m = Metrics()
    if len(equity) < 2:
        return m
    start, end = equity[0], equity[-1]
    m.total_return_pct = (end / start - 1) * 100 if start else 0.0

    rets = _returns(equity)
    mean = sum(rets) / len(rets) if rets else 0.0
    sd = _std(rets)
    downside = _std([r for r in rets if r < 0]) if any(r < 0 for r in rets) else 0.0
    ann = math.sqrt(bars_per_year)
    m.volatility_pct = sd * ann * 100
    m.sharpe = (mean / sd * ann) if sd > 0 else 0.0
    m.sortino = (mean / downside * ann) if downside > 0 else 0.0

    years = len(equity) / bars_per_year
    if years > 0 and start > 0 and end > 0:
        m.cagr_pct = ((end / start) ** (1 / years) - 1) * 100
    m.max_drawdown_pct = max_drawdown_pct(equity)
    m.calmar = (m.cagr_pct / m.max_drawdown_pct) if m.max_drawdown_pct > 0 else 0.0

    m.n_trades = len(trades)
    if trades:
        wins = [t.pnl for t in trades if t.won]
        losses = [t.pnl for t in trades if not t.won]
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        m.win_rate_pct = len(wins) / len(trades) * 100
        m.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        m.avg_win = (gross_win / len(wins)) if wins else 0.0
        m.avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        m.expectancy = sum(t.pnl for t in trades) / len(trades)
        m.best_trade = max(t.pnl for t in trades)
        m.worst_trade = min(t.pnl for t in trades)
    return m
