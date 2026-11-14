"""The gate seam: who sizes, approves and books a trade in the live loop.

`LiveTrader` finds trades; a `Gate` decides what happens to them.

* `LocalGate` is the original in-process path: RiskEngine sizes the order, a
  BrokerAdapter fills it, Python checks stops and targets, flips positions and
  runs the kill switch. Nothing about it changed when it moved behind the seam.
* `GoGate` hands all of that to the Go engine over HTTP. On that path Python
  never sizes an order, never checks an exit, never closes a flip leg and keeps
  no kill switch or book. It sends each candle as a tick, then every decision
  (FLAT and vetoes included), and records only what Go answers.

Failure model for GoGate (see docs/DESIGN.md records 2 and 3):

* A failure *before the request body is sent* (connection refused, DNS, connect
  timeout) is a `gate_error`: nothing reached the engine, so no trade.
* A failure *after the body is sent* (read timeout, dropped connection, 504,
  any other 5xx) means the outcome is unknown: the engine may have filled it.
  The decision is re-sent with the SAME client_id, which the engine dedupes,
  until a final answer arrives or 30 s pass; then the run stops with exit 2.
* A restarted engine (boot_id changed), a live or non-sim venue, an old API
  version, or 5 errors in a row stop the run with exit code 2.
"""
from __future__ import annotations

import http.client
import inspect
import json
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from urllib.parse import urlsplit

from ..brokers.base import BrokerAdapter
from ..core.risk import RiskEngine
from ..core.types import Account, Candle, Decision, Position, Side, Signal

EXIT_GATE_STOP = 2
API_VERSION = 2


class GateStop(Exception):
    """The run must stop now, and the process should exit with `code`."""

    def __init__(self, reason: str, code: int = EXIT_GATE_STOP):
        super().__init__(reason)
        self.reason = reason
        self.code = code


class Gate(ABC):
    """What LiveTrader needs from the thing that owns risk and the book."""

    #: Drain webhook signals after the tick loop (Go) or before it (local).
    signals_after_ticks = False

    def __init__(self):
        self.emit = lambda event_name, /, **data: None

    def bind(self, emit) -> None:
        """LiveTrader hands over its event emitter."""
        self.emit = emit

    @abstractmethod
    def start(self) -> dict:
        """Start-of-run checks. Returns fields for the `start` event
        (must include `equity`). May raise GateStop."""

    @abstractmethod
    def equity(self) -> float:
        """Equity for the final `stop` event. Never touches the network."""

    def stop_fields(self) -> dict:
        return {}

    @abstractmethod
    def before_external(self, signal: Signal) -> None:
        """Called before an externally pushed signal is processed."""

    @abstractmethod
    def on_candle(self, candle: Candle) -> bool:
        """Apply a new bar. False means the bar was skipped: the loop must not
        trade on it."""

    @abstractmethod
    def account(self) -> Account | None:
        """Account view for the brain. None means unavailable: do not trade."""

    @abstractmethod
    def price(self, symbol: str) -> float:
        """Latest mark for a symbol. KeyError when there is none yet."""

    @abstractmethod
    def decide(self, signal: Signal, decision: Decision, acct: Account,
               px: float, source: str) -> None:
        """Turn a brain decision into (at most) a trade and emit its events."""

    @abstractmethod
    def end_cycle(self) -> None:
        """Housekeeping after each polling cycle."""


# --- local ------------------------------------------------------------------

class LocalGate(Gate):
    """The in-process path: RiskEngine + BrokerAdapter, exactly as before."""

    def __init__(self, risk: RiskEngine, broker: BrokerAdapter):
        super().__init__()
        self.risk = risk
        self.broker = broker

    def start(self) -> dict:
        self.risk.start_day(self.broker.account().equity)
        return {"equity": self.broker.account().equity, "broker": self.broker.name()}

    def equity(self) -> float:
        return self.broker.account().equity

    def before_external(self, signal: Signal) -> None:
        # An alert that arrives after midnight UTC belongs to the new day:
        # roll the session first, so yesterday's halt doesn't block it.
        self.risk.roll_day(signal.ts, self.broker.account().equity)

    def on_candle(self, candle: Candle) -> bool:
        self.risk.roll_day(candle.ts, self.broker.account().equity)
        self.broker_mark(candle)
        self._check_exits(candle)
        return True

    def account(self) -> Account:
        return self.broker.account()

    def price(self, symbol: str) -> float:
        return self.broker.price(symbol)

    def decide(self, signal, decision, acct, px, source) -> None:
        order, reason = self.risk.size_order(decision, acct, px)
        self.emit("signal", source=source, symbol=signal.symbol,
                  side=signal.side.value, strength=signal.strength,
                  confidence=decision.confidence, decision=reason)
        if order is None:
            return
        pos = acct.position(order.symbol)
        if not pos.is_flat and ((pos.qty > 0) != (order.side == Side.BUY)):
            self.broker.close(order.symbol, reason="flip") if _accepts_reason(self.broker) \
                else self.broker.close(order.symbol)
        fill = self.broker.submit(order)
        self.emit("fill", symbol=fill.symbol, side=fill.side.value,
                  qty=fill.qty, price=fill.price,
                  rationale=decision.rationale, equity=self.broker.account().equity)

    def end_cycle(self) -> None:
        if self.risk.check_kill_switch(self.broker.account()):
            self.emit("halt", reason="daily drawdown kill switch",
                      equity=self.broker.account().equity)

    # --- local-only internals (the engine's old private methods) -----------
    def broker_mark(self, candle: Candle) -> None:
        # PaperBroker exposes mark(); live adapters price internally.
        mark = getattr(self.broker, "mark", None)
        if callable(mark):
            mark(candle.symbol, candle.close, candle.ts)

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
            self.emit("exit", symbol=symbol, reason=reason, price=fill.price,
                      equity=self.broker.account().equity)


def _accepts_reason(broker: BrokerAdapter) -> bool:
    try:
        return "reason" in inspect.signature(broker.close).parameters
    except (ValueError, TypeError):
        return False


# --- Go engine over HTTP ----------------------------------------------------

class PreSendError(Exception):
    """Failed before any byte of the request body was sent."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class PostSendError(Exception):
    """Failed after the request may have reached the engine."""

    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


class EngineClient:
    """Minimal stdlib HTTP client with separate connect and read timeouts.

    A fresh connection per request keeps the failure phase unambiguous: a stale
    keep-alive socket would blur "never sent" and "sent, no answer". The phase
    is what decides between gate_error and gate_unknown, so it must be exact.
    """

    def __init__(self, base_url: str, connect_timeout: float = 3.0,
                 read_timeout: float = 5.0):
        parts = urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"engine URL must look like http://host:port, got {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.host = parts.hostname
        self.port = parts.port or 80
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout

    def request(self, method: str, path: str, payload: dict | None = None,
                read_timeout: float | None = None):
        """Returns (status, parsed JSON or None). Raises PreSendError or
        PostSendError; never returns for a transport failure."""
        body = None
        headers = {"Accept": "application/json", "Connection": "close"}
        if payload is not None:
            body = json.dumps(payload, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=self.connect_timeout)
        try:
            try:
                conn.connect()
            except ConnectionRefusedError as e:
                raise PreSendError("refused", str(e)) from e
            except TimeoutError as e:
                raise PreSendError("connect_timeout", str(e)) from e
            except OSError as e:
                # DNS failure (socket.gaierror), unreachable network, ...
                raise PreSendError("connect_error", f"{type(e).__name__}: {e}") from e
            conn.sock.settimeout(self.read_timeout if read_timeout is None else read_timeout)
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except TimeoutError as e:
                raise PostSendError("read_timeout", str(e) or "timed out") from e
            except http.client.RemoteDisconnected as e:
                raise PostSendError("disconnected", str(e)) from e
            except (OSError, http.client.HTTPException) as e:
                raise PostSendError("io_error", f"{type(e).__name__}: {e}") from e
        finally:
            conn.close()
        try:
            obj = json.loads(raw.decode()) if raw else None
        except ValueError:
            obj = None
        return resp.status, obj


class GoGate(Gate):
    """Drives the Go engine. The engine is the only authority on this path."""

    signals_after_ticks = True

    def __init__(self, url: str, *, allow_venue: str | None = None,
                 synthetic: bool = False, connect_timeout: float = 3.0,
                 read_timeout: float | None = None, unknown_window: float = 30.0,
                 retry_pause: float = 0.5, max_consecutive_errors: int = 5,
                 clock=time.monotonic, sleep=time.sleep,
                 id_factory=lambda: uuid.uuid4().hex):
        super().__init__()
        self.client = EngineClient(url, connect_timeout=connect_timeout)
        self.url = self.client.base_url
        self.allow_venue = allow_venue
        self.synthetic = synthetic
        self._read_timeout = read_timeout        # None: pick from the venue
        self.unknown_window = unknown_window
        self.retry_pause = retry_pause
        self.max_consecutive_errors = max_consecutive_errors
        self._clock, self._sleep, self._new_id = clock, sleep, id_factory
        self.boot_id: str | None = None
        self.venue: str | None = None
        self._equity = 0.0
        self._halted = False
        self._last_close: dict[str, float] = {}
        self._consecutive = 0
        self.counts = {"gate_error": 0, "gate_unknown": 0}

    # --- startup -----------------------------------------------------------
    def start(self) -> dict:
        try:
            status, health = self.client.request("GET", "/health")
        except (PreSendError, PostSendError) as e:
            raise GateStop(f"engine unreachable at {self.url} ({e}); "
                           "never falling back to the local gate") from e
        if status != 200 or not isinstance(health, dict):
            raise GateStop(f"engine /health returned HTTP {status}")
        self._check_health(health)
        self.boot_id = health["boot_id"]
        self.venue = health["venue"]
        self.client.read_timeout = (self._read_timeout if self._read_timeout is not None
                                    else (5.0 if self.venue == "sim" else 25.0))
        try:
            status, pos = self.client.request("GET", "/positions")
        except (PreSendError, PostSendError) as e:
            raise GateStop(f"engine /positions failed at startup ({e})") from e
        if status != 200 or not isinstance(pos, dict) or "equity" not in pos:
            raise GateStop(f"engine /positions returned HTTP {status}")
        self._equity = float(pos["equity"])
        return {"equity": self._equity, "broker": "go-engine", "engine": self.url,
                "venue": self.venue, "boot_id": self.boot_id}

    def _check_health(self, h: dict) -> None:
        if h.get("api") != API_VERSION:
            raise GateStop(f"engine api version is {h.get('api')!r}, need {API_VERSION}")
        if h.get("live") is not False:
            raise GateStop("engine reports live=true (or no live flag); "
                           "Python never drives a live engine")
        venue = h.get("venue")
        if venue != "sim":
            if self.allow_venue is None or venue != self.allow_venue:
                raise GateStop(f"engine venue is {venue!r}; only 'sim' is allowed "
                               "(or pass --allow-venue alpaca-paper with a real feed)")
            if self.synthetic:
                raise GateStop(f"venue {venue!r} refuses synthetic prices: they would "
                               "place real orders at fake prices")
        if not h.get("boot_id"):
            raise GateStop("engine /health has no boot_id")

    def equity(self) -> float:
        return self._equity

    def stop_fields(self) -> dict:
        return {"gate_errors": self.counts["gate_error"],
                "gate_unknowns": self.counts["gate_unknown"]}

    # --- error bookkeeping -------------------------------------------------
    def _count_error(self, event: str, **data) -> None:
        """Emit a gate_error/gate_unknown event; stop after too many in a row."""
        self.counts[event] += 1
        self._consecutive += 1
        self.emit(event, **data)
        if self._consecutive >= self.max_consecutive_errors:
            raise GateStop(f"{self._consecutive} consecutive gate errors; stopping")

    def _ok(self) -> None:
        self._consecutive = 0

    # --- candles -----------------------------------------------------------
    def before_external(self, signal: Signal) -> None:
        return None

    def on_candle(self, candle: Candle) -> bool:
        sym = candle.symbol
        self._last_close[sym] = candle.close
        try:
            status, health = self.client.request("GET", "/health")
            if status == 200 and isinstance(health, dict):
                self._verify_boot(health)
            else:
                raise PostSendError("http_status", f"/health returned HTTP {status}")
            status, res = self.client.request(
                "POST", "/tick?wait=1",
                {"symbol": sym, "price": candle.close,
                 "high": candle.high, "low": candle.low})
        except PreSendError as e:
            self._count_error("gate_error", where="tick", symbol=sym, kind=e.kind,
                              detail=e.detail[:120], action="skip_bar")
            return False
        except PostSendError as e:
            # Ticks carry no client_id and are idempotent for pricing, so the
            # safe response to a lost reply is to skip the bar, not to retry.
            self._count_error("gate_error", where="tick", symbol=sym, kind=e.kind,
                              detail=e.detail[:120], action="skip_bar")
            return False
        except ValueError as e:     # NaN/inf in a candle: json refuses it
            self._count_error("gate_error", where="tick", symbol=sym,
                              kind="bad_candle", detail=str(e)[:120], action="skip_bar")
            return False
        if status != 200 or not isinstance(res, dict) or "exits" not in res:
            kind = "tick_busy" if status == 503 else f"tick_http_{status}"
            self._count_error("gate_error", where="tick", symbol=sym, kind=kind,
                              detail=_error_text(res), action="skip_bar")
            return False
        self._ok()
        self._equity = float(res.get("equity", self._equity))
        for f in res["exits"] or []:
            self.emit("exit", symbol=f.get("symbol", sym), side=f.get("side"),
                      qty=f.get("qty"), price=f.get("price"), fee=f.get("fee"),
                      reason=f.get("reason"), equity=self._equity)
        halted = bool(res.get("halted"))
        if halted and not self._halted:
            self.emit("halt", reason="engine kill switch tripped", equity=self._equity)
        self._halted = halted
        return True

    def _verify_boot(self, health: dict) -> None:
        if health.get("boot_id") != self.boot_id:
            raise GateStop("engine boot_id changed: it restarted mid-run "
                           f"({self.boot_id} -> {health.get('boot_id')})")
        if health.get("live") is not False:
            raise GateStop("engine now reports live=true")

    # --- account / price ---------------------------------------------------
    def account(self) -> Account | None:
        try:
            status, pos = self.client.request("GET", "/positions")
        except (PreSendError, PostSendError) as e:
            self._count_error("gate_error", where="account", kind=e.kind,
                              detail=e.detail[:120], action="no_trade")
            return None
        if status != 200 or not isinstance(pos, dict) or "equity" not in pos:
            self._count_error("gate_error", where="account", kind=f"http_{status}",
                              detail=_error_text(pos), action="no_trade")
            return None
        self._ok()
        self._equity = float(pos["equity"])
        positions = {}
        for sym, p in (pos.get("positions") or {}).items():
            positions[sym] = Position(
                sym, qty=float(p.get("qty", 0.0)), avg_price=float(p.get("avg_price", 0.0)),
                stop_loss=(p.get("stop_loss") or None), take_profit=(p.get("take_profit") or None))
        return Account(cash=float(pos.get("cash", 0.0)), equity=self._equity,
                       positions=positions)

    def price(self, symbol: str) -> float:
        return self._last_close[symbol]

    # --- decisions ---------------------------------------------------------
    def decide(self, signal, decision, acct, px, source) -> None:
        client_id = self._new_id()
        try:
            payload = {
                "ts": _aware(decision.ts).isoformat(),
                "symbol": decision.symbol,
                "side": decision.side.value,
                "confidence": decision.confidence,
                "size_hint": decision.size_hint,
                "rationale": decision.rationale,
                "veto": decision.veto,
                "client_id": client_id,
                "expect_boot_id": self.boot_id,
            }
            json.dumps(payload, allow_nan=False)
        except ValueError as e:
            self._count_error("gate_error", where="decision", symbol=decision.symbol,
                              kind="bad_decision", detail=str(e)[:120], action="no_trade")
            return
        got = self._send_decision(payload, client_id, decision.symbol)
        if got is None:
            return
        res, rtt_ms, replayed = got
        reason = res.get("reason", "")
        self.emit("signal", source=source, symbol=signal.symbol,
                  side=signal.side.value, strength=signal.strength,
                  confidence=decision.confidence, decision=reason, client_id=client_id)
        order = res.get("order") or {}
        self.emit("verdict", symbol=decision.symbol, side=decision.side.value,
                  verdict=res["verdict"], approved=bool(res["approved"]), reason=reason,
                  qty=order.get("qty"), client_id=client_id,
                  rtt_ms=round(rtt_ms, 3), replayed=replayed)
        fills = res.get("fills") or []
        for f in fills:
            self.emit("fill", symbol=f.get("symbol", decision.symbol), side=f.get("side"),
                      qty=f.get("qty"), price=f.get("price"), fee=f.get("fee"),
                      reason=f.get("reason"), rationale=decision.rationale,
                      client_id=client_id)
        if res["approved"] and not fills:
            # A venue accepted the order but it has not filled yet.
            self.emit("accepted_unfilled", symbol=decision.symbol, client_id=client_id)

    def _send_decision(self, payload: dict, client_id: str, symbol: str):
        """POST /decision, re-sending the same client_id while the outcome is
        unknown. Returns (response, rtt_ms, replayed), or None for a clean
        no-trade error. Raises GateStop when the run must end."""
        started = self._clock()
        deadline = started + self.unknown_window
        attempt = 0
        while True:
            failure: tuple[str, str] | None = None
            remaining = deadline - self._clock()
            timeout = self.client.read_timeout if attempt == 0 else \
                max(1.0, min(self.client.read_timeout, remaining))
            t0 = time.perf_counter()
            try:
                status, res = self.client.request("POST", "/decision", payload,
                                                  read_timeout=timeout)
            except PreSendError as e:
                if attempt == 0:
                    self._count_error("gate_error", where="decision", symbol=symbol,
                                      kind=e.kind, detail=e.detail[:120],
                                      client_id=client_id, action="no_trade")
                    return None
                failure = (e.kind, e.detail)   # the original may still be live
            except PostSendError as e:
                failure = (e.kind, e.detail)
            else:
                rtt_ms = (time.perf_counter() - t0) * 1000.0
                if status == 200 and isinstance(res, dict) and "verdict" in res \
                        and "approved" in res:
                    if res.get("client_id", client_id) != client_id:
                        raise GateStop("engine answered a different client_id")
                    self._ok()
                    return res, rtt_ms, bool(res.get("replayed")) or attempt > 0
                if status == 409:
                    raise GateStop("engine boot_id mismatch (409): it restarted since "
                                   "the run began")
                if status == 503:
                    raise GateStop("engine is shutting down (503); outcome of "
                                   f"decision {client_id} is unknown")
                if status in (400, 422):
                    self._count_error("gate_error", where="decision", symbol=symbol,
                                      kind=f"bad_request_{status}", detail=_error_text(res),
                                      client_id=client_id, action="no_trade")
                    return None
                failure = (f"http_{status}", _error_text(res))   # 504, 5xx, junk 200
            # Unknown outcome: the request may have been processed.
            if attempt == 0:
                self._count_error("gate_unknown", symbol=symbol, client_id=client_id,
                                  kind=failure[0], detail=failure[1][:120],
                                  action="pause_and_retry")
            attempt += 1
            if self._clock() + self.retry_pause >= deadline:
                raise GateStop(f"outcome of decision {client_id} on {symbol} still "
                               f"unknown after {self.unknown_window:.0f}s "
                               f"(last: {failure[0]})")
            self._sleep(self.retry_pause)

    def end_cycle(self) -> None:
        return None


def _aware(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _error_text(obj) -> str:
    if isinstance(obj, dict):
        return str(obj.get("error", obj))[:120]
    return ""
