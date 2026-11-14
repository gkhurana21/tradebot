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
from ..core.types import Candle, Signal
from .gate import Gate, GateStop, LocalGate


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
        risk: RiskEngine | None = None,
        broker: BrokerAdapter | None = None,
        fetch_latest=None,            # callable(symbol) -> Candle | None
        config: LiveConfig | None = None,
        on_event=None,                # callable(dict) for logging/streaming
        gate: Gate | None = None,
    ):
        """With no `gate`, risk and broker are wrapped in a LocalGate: today's
        in-process path. Pass a GoGate to let the Go engine size, approve and
        book trades instead; risk and broker are then unused (and unset)."""
        if gate is None:
            if risk is None or broker is None:
                raise ValueError("LiveTrader needs a gate, or both risk and broker")
            gate = LocalGate(risk, broker)
        if fetch_latest is None:
            raise ValueError("LiveTrader needs fetch_latest")
        self.make_strategy = strategy_factory
        self.brain = brain
        self.gate = gate
        self.risk = getattr(gate, "risk", None)
        self.broker = getattr(gate, "broker", None)
        self.fetch_latest = fetch_latest
        self.cfg = config or LiveConfig()
        self.on_event = on_event or (lambda e: None)
        self._strategies = {s: strategy_factory() for s in self.cfg.symbols}
        self._last_ts: dict[str, datetime] = {}
        self._external: "queue.Queue[Signal]" = queue.Queue()
        self._running = False
        gate.bind(self._emit)

    def submit_external_signal(self, signal: Signal) -> None:
        """Thread-safe: push a signal from a webhook / external source."""
        self._external.put(signal)

    def stop(self) -> None:
        self._running = False

    def run(self, max_iterations: int | None = None) -> None:
        """Runs until stopped. Raises GateStop (exit code 2 at the CLI) when
        the gate cannot be trusted any more; a `stop` event is emitted first."""
        self._running = True
        try:
            self._emit("start", **self.gate.start(), symbols=self.cfg.symbols)
            i = 0
            while self._running:
                self._tick()
                i += 1
                if max_iterations is not None and i >= max_iterations:
                    break
                time.sleep(self.cfg.poll_seconds)
        except GateStop as e:
            self._emit("stop", reason=e.reason, exit_code=e.code,
                       equity=self.gate.equity(), **self.gate.stop_fields())
            raise
        self._emit("stop", equity=self.gate.equity(), **self.gate.stop_fields())

    def tick_once(self) -> None:
        """Process one polling cycle (handy for tests / cron-driven runs)."""
        self._tick()

    # --- internals ---------------------------------------------------------
    def _tick(self) -> None:
        # 1) Drain any externally-pushed signals (e.g. TradingView). A gate
        #    that prices from its own marks wants the ticks to land first.
        if not self.gate.signals_after_ticks:
            self._drain_external()
        # 2) Poll each symbol for a fresh candle and run the strategy.
        for sym in self.cfg.symbols:
            candle = self._safe_fetch(sym)
            if candle is None or not self._is_new(candle):
                continue
            applied = self.gate.on_candle(candle)
            sig = self._strategies[sym].on_candle(candle)
            if sig is not None and applied:
                self._process_signal(sig, source="strategy", price=candle.close)
        if self.gate.signals_after_ticks:
            self._drain_external()
        # 3) Risk housekeeping.
        self.gate.end_cycle()

    def _drain_external(self) -> None:
        while not self._external.empty():
            try:
                sig = self._external.get_nowait()
            except queue.Empty:
                break
            self.gate.before_external(sig)
            self._process_signal(sig, source="external")

    def _process_signal(self, signal: Signal, source: str, price: float | None = None) -> None:
        acct = self.gate.account()
        if acct is None:
            return  # gate could not give us the account: no trade
        try:
            px = price if price is not None else self.gate.price(signal.symbol)
        except KeyError:
            return  # no mark yet for this symbol
        extra = self._context(signal.symbol)
        decision = self.brain.decide(signal, acct, px, extra)
        self.gate.decide(signal, decision, acct, px, source)

    def broker_mark(self, candle: Candle) -> None:
        """Local gate only: mark the paper broker (kept for callers of the old API)."""
        mark = getattr(self.gate, "broker_mark", None)
        if callable(mark):
            mark(candle)

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

    def _emit(self, event_name: str, /, **data) -> None:
        # Positional-only so an event may carry its own `kind` field.
        event = {"ts": datetime.now(tz=timezone.utc).isoformat(), "event": event_name, **data}
        self.on_event(event)

