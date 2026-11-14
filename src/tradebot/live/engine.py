"""Live trading loop — the same strategy -> brain -> risk -> broker path as the
backtester, driven by a real-time feed instead of a replay.

Runs paper by default (PaperBroker). Swap in a live BrokerAdapter to go real —
that switch is yours to flip, with your eyes on the risk. Signals can come from
the built-in strategy OR be pushed in from an external source (e.g. the
TradingView webhook receiver) via `submit_external_signal`.
"""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..brain.brain import Brain
from ..brokers.base import BrokerAdapter
from ..core.risk import RiskEngine
from ..core.strategy import Strategy
from ..core.types import Candle, Side, Signal


@dataclass
class LiveConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC-USD"])
    poll_seconds: float = 60.0
    onchain_context: bool = False


class LiveTrader:
    def __init__(
        self,
        strategy_factory,
        brain: Brain,
        risk: RiskEngine,
        broker: BrokerAdapter,
        fetch_latest,                 # callable(symbol) -> Candle | None
        config: LiveConfig | None = None,
        on_event=None,                # callable(dict) for logging/streaming
    ):
        self.make_strategy = strategy_factory
        self.brain = brain
        self.risk = risk
        self.broker = broker
        self.fetch_latest = fetch_latest
        self.cfg = config or LiveConfig()
        self.on_event = on_event or (lambda e: None)
        self._strategies = {s: strategy_factory() for s in self.cfg.symbols}
        self._last_ts: dict[str, datetime] = {}
        self._external: "queue.Queue[Signal]" = queue.Queue()
        self._running = False

    def submit_external_signal(self, signal: Signal) -> None:
        """Thread-safe: push a signal from a webhook / external source."""
        self._external.put(signal)

    def stop(self) -> None:
        self._running = False

    def run(self, max_iterations: int | None = None) -> None:
        self._running = True
        self.risk.start_day(self.broker.account().equity)
        self._emit("start", equity=self.broker.account().equity,
                   broker=self.broker.name(), symbols=self.cfg.symbols)
        i = 0
        while self._running:
            self._tick()
            i += 1
            if max_iterations is not None and i >= max_iterations:
                break
            time.sleep(self.cfg.poll_seconds)
        self._emit("stop", equity=self.broker.account().equity)

    def tick_once(self) -> None:
        """Process one polling cycle (handy for tests / cron-driven runs)."""
        self._tick()

    # --- internals ---------------------------------------------------------
    def _tick(self) -> None:
        # 1) Drain any externally-pushed signals (e.g. TradingView).
        while not self._external.empty():
            try:
                self._process_signal(self._external.get_nowait(), source="external")
            except queue.Empty:
                break
        # 2) Poll each symbol for a fresh candle and run the strategy.
        for sym in self.cfg.symbols:
            candle = self._safe_fetch(sym)
            if candle is None or not self._is_new(candle):
                continue
            self.risk.roll_day(candle.ts, self.broker.account().equity)
            self.broker_mark(candle)
            self._check_exits(candle)
            sig = self._strategies[sym].on_candle(candle)
            if sig is not None:
                self._process_signal(sig, source="strategy", price=candle.close)
        # 3) Risk housekeeping.
        if self.risk.check_kill_switch(self.broker.account()):
            self._emit("halt", reason="daily drawdown kill switch",
                       equity=self.broker.account().equity)

    def _process_signal(self, signal: Signal, source: str, price: float | None = None) -> None:
        acct = self.broker.account()
        try:
            px = price if price is not None else self.broker.price(signal.symbol)
        except KeyError:
            return  # no mark yet for this symbol
        extra = self._context(signal.symbol)
        decision = self.brain.decide(signal, acct, px, extra)
        order, reason = self.risk.size_order(decision, acct, px)
        self._emit("signal", source=source, symbol=signal.symbol,
                   side=signal.side.value, strength=signal.strength,
                   confidence=decision.confidence, decision=reason)
        if order is None:
            return
        pos = acct.position(order.symbol)
        if not pos.is_flat and ((pos.qty > 0) != (order.side == Side.BUY)):
            self.broker.close(order.symbol, reason="flip") if _accepts_reason(self.broker) \
                else self.broker.close(order.symbol)
        fill = self.broker.submit(order)
        self._emit("fill", symbol=fill.symbol, side=fill.side.value,
                   qty=fill.qty, price=fill.price,
                   rationale=decision.rationale, equity=self.broker.account().equity)

    def _check_exits(self, candle: Candle) -> None:
        pos = self.broker.account().position(candle.symbol)
        if pos.is_flat:
            return
        long = pos.qty > 0
        if pos.stop_loss is not None and (
                candle.low <= pos.stop_loss if long else candle.high >= pos.stop_loss):
            self._close(candle.symbol, "stop")
        elif pos.take_profit is not None and (
                candle.high >= pos.take_profit if long else candle.low <= pos.take_profit):
            self._close(candle.symbol, "target")

    def _close(self, symbol: str, reason: str) -> None:
        fill = (self.broker.close(symbol, reason=reason) if _accepts_reason(self.broker)
                else self.broker.close(symbol))
        if fill:
            self._emit("exit", symbol=symbol, reason=reason, price=fill.price,
                       equity=self.broker.account().equity)

    def broker_mark(self, candle: Candle) -> None:
        # PaperBroker exposes mark(); live adapters price internally.
        mark = getattr(self.broker, "mark", None)
        if callable(mark):
            mark(candle.symbol, candle.close, candle.ts)

    def _context(self, symbol: str) -> dict | None:
        if not self.cfg.onchain_context:
            return None
        from ..data.onchain import fetch_snapshot
        snap = fetch_snapshot()
        return {"onchain": snap.features()} if snap.ok else None

    def _safe_fetch(self, symbol: str) -> Candle | None:
        try:
            return self.fetch_latest(symbol)
        except Exception as e:
            self._emit("error", where="fetch", symbol=symbol, error=str(e)[:120])
            return None

    def _is_new(self, candle: Candle) -> bool:
        last = self._last_ts.get(candle.symbol)
        if last is not None and candle.ts <= last:
            return False
        self._last_ts[candle.symbol] = candle.ts
        return True

    def _emit(self, kind: str, **data) -> None:
        event = {"ts": datetime.now(tz=timezone.utc).isoformat(), "event": kind, **data}
        self.on_event(event)


def _accepts_reason(broker: BrokerAdapter) -> bool:
    import inspect
    try:
        return "reason" in inspect.signature(broker.close).parameters
    except (ValueError, TypeError):
        return False
