"""Event-driven backtester.

Replays candles one at a time through the full production loop:
  strategy -> brain (Claude) -> risk engine -> paper broker
so a backtest exercises the exact code path a live run would. Also enforces
stops/targets bar-by-bar and produces a performance summary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..brain.brain import Brain
from ..brokers.paper import PaperBroker
from ..core import metrics as m
from ..core.risk import RiskEngine
from ..core.strategy import Strategy
from ..core.types import Candle, Side


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list = field(default_factory=list)
    trades: list = field(default_factory=list)   # metrics.Trade round trips
    metrics: m.Metrics = field(default_factory=m.Metrics)
    n_signals: int = 0
    n_orders: int = 0      # entries/flips submitted; closed round trips are metrics.n_trades
    n_rejected: int = 0
    n_stops: int = 0
    n_targets: int = 0

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity - 1) * 100

    @property
    def max_drawdown_pct(self) -> float:
        return m.max_drawdown_pct(self.equity_curve)

    def summary(self) -> str:
        return (
            f"Return: {self.total_return_pct:+.2f}%  "
            f"MaxDD: {self.max_drawdown_pct:.2f}%  "
            f"Sharpe: {self.metrics.sharpe:.2f}  "
            f"Win%: {self.metrics.win_rate_pct:.0f}  "
            f"PF: {self.metrics.profit_factor:.2f}  "
            f"Trades: {self.metrics.n_trades}  Orders: {self.n_orders}  Rejected: {self.n_rejected}  "
            f"Stops: {self.n_stops}  Targets: {self.n_targets}  "
            f"Signals: {self.n_signals}  "
            f"Equity: {self.starting_equity:,.0f} -> {self.ending_equity:,.0f}"
        )


class Backtester:
    def __init__(
        self,
        strategy: Strategy,
        brain: Brain,
        risk: RiskEngine,
        broker: PaperBroker,
        verbose: bool = False,
    ):
        self.strategy = strategy
        self.brain = brain
        self.risk = risk
        self.broker = broker
        self.verbose = verbose

    def run(self, candles: Iterable[Candle]) -> BacktestResult:
        res = BacktestResult(
            starting_equity=self.broker.account().equity,
            ending_equity=self.broker.account().equity,
        )
        self.risk.start_day(self.broker.account().equity)

        for candle in candles:
            self.risk.roll_day(candle.ts, self.broker.account().equity)
            self.broker.mark(candle.symbol, candle.close, candle.ts)
            self._check_exits(candle, res)

            signal = self.strategy.on_candle(candle)
            if signal is not None:
                res.n_signals += 1
                self._act_on_signal(signal, candle, res)

            self.risk.check_kill_switch(self.broker.account())
            res.equity_curve.append(self.broker.account().equity)
            res.timestamps.append(candle.ts)

        # Flatten at end for a clean final equity.
        for sym in list(self.broker.account().positions):
            if not self.broker.account().position(sym).is_flat:
                self.broker.close(sym, reason="end")
        res.ending_equity = self.broker.account().equity
        res.trades = self.broker.trades
        res.metrics = m.compute(res.equity_curve, res.trades)
        return res

    def _act_on_signal(self, signal, candle, res: BacktestResult) -> None:
        acct = self.broker.account()
        decision = self.brain.decide(signal, acct, candle.close)
        order, reason = self.risk.size_order(decision, acct, candle.close)
        if order is None:
            res.n_rejected += 1
            if self.verbose:
                print(f"{candle.ts:%Y-%m-%d %H:%M} REJECT {signal.side.value} "
                      f"{signal.symbol}: {reason}")
            return
        # If flipping direction, close the opposing position first.
        pos = acct.position(order.symbol)
        if not pos.is_flat and ((pos.qty > 0) != (order.side == Side.BUY)):
            self.broker.close(order.symbol, reason="flip")
        fill = self.broker.submit(order)
        res.n_orders += 1
        if self.verbose:
            print(f"{candle.ts:%Y-%m-%d %H:%M} {fill.side.value} "
                  f"{fill.qty:.4f} {fill.symbol} @ {fill.price:,.2f} "
                  f"[{decision.rationale}]")

    def _check_exits(self, candle, res: BacktestResult) -> None:
        pos = self.broker.account().position(candle.symbol)
        if pos.is_flat:
            return
        long = pos.qty > 0
        # Intrabar: check stop then target using low/high extremes.
        if pos.stop_loss is not None:
            hit = candle.low <= pos.stop_loss if long else candle.high >= pos.stop_loss
            if hit:
                self.broker.mark(candle.symbol, pos.stop_loss, candle.ts)
                self.broker.close(candle.symbol, reason="stop")
                res.n_stops += 1
                self.broker.mark(candle.symbol, candle.close, candle.ts)
                return
        if pos.take_profit is not None:
            hit = candle.high >= pos.take_profit if long else candle.low <= pos.take_profit
            if hit:
                self.broker.mark(candle.symbol, pos.take_profit, candle.ts)
                self.broker.close(candle.symbol, reason="target")
                res.n_targets += 1
                self.broker.mark(candle.symbol, candle.close, candle.ts)
