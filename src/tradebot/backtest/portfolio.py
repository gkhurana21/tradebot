"""Multi-symbol portfolio backtester.

Interleaves candle streams from several symbols in timestamp order and runs
them through ONE shared account, risk engine, and brain — so position limits,
the daily-drawdown kill switch, and capital are enforced across the whole book,
not per symbol. Each symbol gets its own strategy instance.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..brain.brain import Brain
from ..brokers.paper import PaperBroker
from ..core import metrics as m
from ..core.risk import RiskEngine
from ..core.strategy import Strategy
from ..core.types import Candle, Side


@dataclass
class PortfolioResult:
    starting_equity: float
    ending_equity: float
    equity_curve: list[float] = field(default_factory=list)
    timestamps: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    metrics: m.Metrics = field(default_factory=m.Metrics)
    per_symbol: dict = field(default_factory=dict)   # orders per symbol
    n_signals: int = 0
    n_orders: int = 0      # entries/flips submitted; closed round trips are metrics.n_trades
    n_rejected: int = 0
    halts: list = field(default_factory=list)   # when the kill switch fired

    @property
    def total_return_pct(self) -> float:
        return (self.ending_equity / self.starting_equity - 1) * 100

    def summary(self) -> str:
        by_sym = "  ".join(f"{s}:{n}" for s, n in self.per_symbol.items())
        return (
            f"Return: {self.total_return_pct:+.2f}%  "
            f"Sharpe: {self.metrics.sharpe:.2f}  "
            f"Win%: {self.metrics.win_rate_pct:.0f}  "
            f"PF: {self.metrics.profit_factor:.2f}  "
            f"Trades: {self.metrics.n_trades}  Orders: {self.n_orders} ({by_sym})  Rejected: {self.n_rejected}  "
            f"Equity: {self.starting_equity:,.0f} -> {self.ending_equity:,.0f}"
        )


class PortfolioBacktester:
    def __init__(
        self,
        strategy_factory: Callable[[], Strategy],
        brain: Brain,
        risk: RiskEngine,
        broker: PaperBroker,
        context_provider: Callable[[str], dict] | None = None,
        verbose: bool = False,
    ):
        self.make_strategy = strategy_factory
        self.brain = brain
        self.risk = risk
        self.broker = broker
        self.context_provider = context_provider
        self.verbose = verbose

    def run(self, streams: dict[str, list[Candle]]) -> PortfolioResult:
        strategies = {sym: self.make_strategy() for sym in streams}
        # Merge all candles into one time-ordered event stream.
        events: list[Candle] = [c for candles in streams.values() for c in candles]
        events.sort(key=lambda c: c.ts)

        res = PortfolioResult(
            starting_equity=self.broker.account().equity,
            ending_equity=self.broker.account().equity,
            per_symbol={sym: 0 for sym in streams},
        )
        self.risk.start_day(self.broker.account().equity)

        for candle in events:
            self.risk.roll_day(candle.ts, self.broker.account().equity)
            self.broker.mark(candle.symbol, candle.close, candle.ts)
            self._check_exits(candle)

            signal = strategies[candle.symbol].on_candle(candle)
            if signal is not None:
                res.n_signals += 1
                acct = self.broker.account()
                extra = self.context_provider(candle.symbol) if self.context_provider else None
                decision = self.brain.decide(signal, acct, candle.close, extra)
                order, reason = self.risk.size_order(decision, acct, candle.close)
                if order is None:
                    res.n_rejected += 1
                else:
                    pos = acct.position(order.symbol)
                    if not pos.is_flat and ((pos.qty > 0) != (order.side == Side.BUY)):
                        self.broker.close(order.symbol, reason="flip")
                    self.broker.submit(order)
                    res.n_orders += 1
                    res.per_symbol[candle.symbol] += 1

            was_halted = self.risk.halted
            if self.risk.check_kill_switch(self.broker.account()) and not was_halted:
                res.halts.append(candle.ts)
            res.equity_curve.append(self.broker.account().equity)
            res.timestamps.append(candle.ts)

        for sym in list(self.broker.account().positions):
            if not self.broker.account().position(sym).is_flat:
                self.broker.close(sym, reason="end")
        res.ending_equity = self.broker.account().equity
        res.trades = self.broker.trades
        res.metrics = m.compute(res.equity_curve, res.trades)
        return res

    def _check_exits(self, candle: Candle) -> None:
        pos = self.broker.account().position(candle.symbol)
        if pos.is_flat:
            return
        long = pos.qty > 0
        if pos.stop_loss is not None:
            hit = candle.low <= pos.stop_loss if long else candle.high >= pos.stop_loss
            if hit:
                self.broker.mark(candle.symbol, pos.stop_loss, candle.ts)
                self.broker.close(candle.symbol, reason="stop")
                self.broker.mark(candle.symbol, candle.close, candle.ts)
                return
        if pos.take_profit is not None:
            hit = candle.high >= pos.take_profit if long else candle.low <= pos.take_profit
            if hit:
                self.broker.mark(candle.symbol, pos.take_profit, candle.ts)
                self.broker.close(candle.symbol, reason="target")
                self.broker.mark(candle.symbol, candle.close, candle.ts)
