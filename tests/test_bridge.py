"""Tests for the Python -> Go bridge (GoGate), against a fake engine on a local
stdlib HTTP server. Run: python3 tests/test_bridge.py

The fake speaks just enough of the engine's api-2 contract (see engine/README.md)
to exercise how GoGate classifies failures: nothing sent (gate_error), sent but
unanswered (gate_unknown, same client_id re-sent), restarted engine (stop), and
a tick the engine refused (skip the bar). The real engine is exercised by the CI
job, not here.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.brain.brain import Brain
from tradebot.core.risk import RiskEngine
from tradebot.core.types import Candle, Side, Signal
from tradebot.live.engine import LiveConfig, LiveTrader
from tradebot.live.gate import GateStop, GoGate

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


class FakeEngine:
    """A scriptable stand-in for the Go engine."""

    def __init__(self):
        self.boot_id = "boot-1"
        self.api, self.venue, self.live = 2, "sim", False
        self.tick_status = 200           # what POST /tick returns
        self.exits: list[dict] = []      # exits reported by the next tick
        self.decision_script: list[str] = []   # per new decision: ok|slow|504|drop|409|503
        self.slow_seconds = 0.6
        self.calls: list[tuple[str, str]] = []
        self.decisions: dict[str, dict] = {}   # client_id -> stored response
        self.decision_requests: list[dict] = []
        self.orders_placed = 0
        self.lock = threading.Lock()
        engine = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def finish(self):
                try:
                    super().finish()
                except OSError:
                    pass      # a test closed the socket on purpose

            def _send(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass      # the client already gave up (read timeout)

            def do_GET(self):
                engine.calls.append(("GET", self.path))
                if self.path == "/health":
                    self._send(200, {"status": "ok", "api": engine.api,
                                     "boot_id": engine.boot_id, "venue": engine.venue,
                                     "live": engine.live})
                elif self.path == "/positions":
                    self._send(200, {"equity": 10000.0, "cash": 10000.0, "positions": {}})
                else:
                    self._send(404, {"error": "no"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                engine.calls.append(("POST", self.path))
                if self.path.startswith("/tick"):
                    if engine.tick_status != 200:
                        return self._send(engine.tick_status, {"error": "tick buffer full"})
                    exits, engine.exits = engine.exits, []
                    return self._send(200, {"equity": 10000.0, "halted": False, "exits": exits})
                if self.path == "/decision":
                    return engine._decision(self, body)
                self._send(404, {"error": "no"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _decision(self, h, body):
        with self.lock:
            self.decision_requests.append(body)
            if body.get("expect_boot_id") not in (None, self.boot_id):
                return h._send(409, {"error": "boot_id mismatch", "boot_id": self.boot_id})
            cid = body["client_id"]
            if cid in self.decisions:       # replay: the stored result, no new order
                return h._send(200, dict(self.decisions[cid], replayed=True))
            mode = self.decision_script.pop(0) if self.decision_script else "ok"
            if mode == "409":
                return h._send(409, {"error": "boot_id mismatch"})
            if mode == "503":
                return h._send(503, {"error": "engine is shutting down"})
            approved = body["confidence"] >= 0.55 and body["side"] != "FLAT" and not body["veto"]
            res = {"verdict": "approved" if approved else "rejected_low_confidence",
                   "approved": approved, "client_id": cid,
                   "reason": "sized 1 BTC-USD" if approved else "confidence below minimum",
                   "fills": []}
            if approved:
                self.orders_placed += 1
                res["order"] = {"symbol": body["symbol"], "side": body["side"], "qty": 1.0}
                res["fills"] = [{"symbol": body["symbol"], "side": body["side"], "qty": 1.0,
                                 "price": 100.0, "fee": 0.1, "reason": ""}]
            self.decisions[cid] = res          # processed: stored even if the reply is lost
        if mode == "slow":
            time.sleep(self.slow_seconds)
        elif mode == "drop":
            h.connection.close()
            return
        elif mode == "504":
            return h._send(504, {"error": "context deadline exceeded"})
        h._send(200, res)

    def count(self, method, prefix):
        return sum(1 for m, p in self.calls if m == method and p.startswith(prefix))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AlwaysBuy:
    """A strategy that signals BUY on every bar, so each bar makes a decision."""

    def on_candle(self, candle):
        return Signal(candle.ts, candle.symbol, Side.BUY, 0.9, "test", {})


def candles(n=20, symbol="BTC-USD"):
    it = iter(Candle(T0 + timedelta(minutes=i), 100, 101, 99, 100, 1, symbol) for i in range(n))
    return lambda sym: next(it, None)


def make(engine, **gate_kw):
    events = []
    gate_kw.setdefault("retry_pause", 0.01)
    gate = GoGate(engine.url, **gate_kw)
    trader = LiveTrader(AlwaysBuy, Brain(use_llm=False), fetch_latest=candles(),
                        config=LiveConfig(symbols=["BTC-USD"], poll_seconds=0),
                        on_event=events.append, gate=gate)
    return trader, gate, events


def kinds(events):
    return [e["event"] for e in events]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_must_stop(engine_kwargs=None, gate_kw=None):
    eng = FakeEngine()
    for k, v in (engine_kwargs or {}).items():
        setattr(eng, k, v)
    try:
        GoGate(eng.url, **(gate_kw or {})).start()
    except GateStop as e:
        assert e.code == 2
        return e.reason
    finally:
        eng.close()
    raise AssertionError("expected GateStop")


# --- the happy path ------------------------------------------------------------

def test_happy_path_records_only_what_the_engine_answers():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        trader.run(max_iterations=3)
        k = kinds(events)
        assert k[0] == "start" and k[-1] == "stop"
        assert k.count("verdict") == 3 and k.count("fill") == 3 and k.count("signal") == 3
        assert eng.orders_placed == 3
        v = next(e for e in events if e["event"] == "verdict")
        assert v["verdict"] == "approved" and v["approved"] and v["qty"] == 1.0
        assert len(v["client_id"]) == 32 and v["rtt_ms"] >= 0
        f = next(e for e in events if e["event"] == "fill")
        assert f["price"] == 100.0 and f["fee"] == 0.1
        # Every request carries a fresh client_id, the boot_id and a tz-aware ts.
        ids = [r["client_id"] for r in eng.decision_requests]
        assert len(set(ids)) == 3
        assert all(r["expect_boot_id"] == "boot-1" for r in eng.decision_requests)
        assert all(r["ts"].endswith("+00:00") for r in eng.decision_requests)
        assert eng.count("POST", "/tick?wait=1") == 3
    finally:
        eng.close()


def test_tick_exits_are_recorded_as_exit_events():
    eng = FakeEngine()
    try:
        eng.exits = [{"symbol": "BTC-USD", "side": "SELL", "qty": 1.0, "price": 98.0,
                      "fee": 0.1, "reason": "stop"}]
        trader, gate, events = make(eng)
        trader.run(max_iterations=1)
        ex = [e for e in events if e["event"] == "exit"]
        assert len(ex) == 1 and ex[0]["reason"] == "stop" and ex[0]["price"] == 98.0
    finally:
        eng.close()


def test_flat_and_veto_decisions_are_sent_too():
    class Flat:
        def on_candle(self, candle):
            return Signal(candle.ts, candle.symbol, Side.SELL, 0.1, "weak", {})
    eng = FakeEngine()
    try:
        events = []
        gate = GoGate(eng.url, retry_pause=0.01)
        trader = LiveTrader(Flat, Brain(use_llm=False), fetch_latest=candles(),
                            config=LiveConfig(symbols=["BTC-USD"], poll_seconds=0),
                            on_event=events.append, gate=gate)
        trader.run(max_iterations=2)
        # Low-confidence decisions still go to the engine; Python does not pre-filter.
        assert len(eng.decision_requests) == 2
        assert [e["approved"] for e in events if e["event"] == "verdict"] == [False, False]
        assert "fill" not in kinds(events)
    finally:
        eng.close()


# --- the seam ------------------------------------------------------------------

def test_size_order_is_never_called_on_the_go_path():
    eng = FakeEngine()
    calls = []
    original = RiskEngine.size_order

    def spy(self, *a, **k):
        calls.append(a)
        raise AssertionError("size_order must not run on the Go path")
    RiskEngine.size_order = spy
    try:
        trader, gate, events = make(eng)
        assert trader.risk is None and trader.broker is None   # no local book at all
        trader.run(max_iterations=5)
        assert calls == []
        assert kinds(events).count("fill") == 5               # trades still happened
    finally:
        RiskEngine.size_order = original
        eng.close()


def test_go_path_has_no_local_exit_check_flip_or_kill_switch():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        # LocalGate-only machinery must not exist on the Go gate.
        for name in ("risk", "broker", "_check_exits", "_close", "broker_mark"):
            assert not hasattr(gate, name), name
        assert gate.end_cycle() is None
    finally:
        eng.close()


# --- startup guards ----------------------------------------------------------------

def test_old_api_version_is_refused():
    assert "api version" in start_must_stop({"api": 1})


def test_live_engine_is_refused_even_with_allow_venue():
    assert "live" in start_must_stop({"live": True})
    assert "live" in start_must_stop({"live": True, "venue": "alpaca-paper"},
                                     {"allow_venue": "alpaca-paper"})


def test_non_sim_venue_needs_allow_flag_and_a_real_feed():
    assert "only 'sim'" in start_must_stop({"venue": "alpaca-paper"})
    assert "synthetic" in start_must_stop({"venue": "alpaca-paper"},
                                          {"allow_venue": "alpaca-paper", "synthetic": True})
    assert "only 'sim'" in start_must_stop({"venue": "mystery"},
                                           {"allow_venue": "alpaca-paper"})
    eng = FakeEngine()
    eng.venue = "alpaca-paper"
    try:
        info = GoGate(eng.url, allow_venue="alpaca-paper", synthetic=False).start()
        assert info["venue"] == "alpaca-paper"
        g = GoGate(eng.url, allow_venue="alpaca-paper")
        g.start()
        assert g.client.read_timeout >= 25          # venue engines get the long timeout
    finally:
        eng.close()


def test_lost_tick_reply_at_a_venue_stops_the_run():
    from tradebot.live.gate import GateStop, PostSendError
    eng = FakeEngine()
    eng.venue = "alpaca-paper"
    try:
        g = GoGate(eng.url, allow_venue="alpaca-paper", synthetic=False)
        g.start()
        assert g.unknown_window >= 2 * g.client.read_timeout   # room for a retry
        real = g.client.request

        def lose_tick_reply(method, path, payload=None, read_timeout=None):
            if path.startswith("/tick"):
                raise PostSendError("read_timeout", "timed out")
            return real(method, path, payload, read_timeout)

        g.client.request = lose_tick_reply
        try:
            g.on_candle(candles(1)("BTC-USD"))
            raise AssertionError("expected GateStop")
        except GateStop as e:
            assert "exit may have happened" in e.reason
    finally:
        eng.close()


def test_sim_engine_uses_the_short_read_timeout():
    eng = FakeEngine()
    try:
        g = GoGate(eng.url)
        g.start()
        assert g.client.read_timeout == 5.0
    finally:
        eng.close()


def test_unreachable_engine_at_startup_stops_with_exit_2():
    try:
        GoGate(f"http://127.0.0.1:{free_port()}").start()
    except GateStop as e:
        assert e.code == 2 and "unreachable" in e.reason
    else:
        raise AssertionError("expected GateStop")


# --- error classification ------------------------------------------------------------

def test_connect_refused_mid_run_is_gate_error_and_no_trade():
    eng = FakeEngine()
    trader, gate, events = make(eng)
    gate.start()
    eng.close()                                   # the engine goes away
    trader.tick_once()
    errs = [e for e in events if e["event"] == "gate_error"]
    assert len(errs) == 1 and errs[0]["kind"] == "refused" and errs[0]["action"] == "skip_bar"
    assert not {"fill", "verdict", "signal"} & set(kinds(events))
    assert "gate_unknown" not in kinds(events)


def test_five_consecutive_errors_stop_with_exit_2():
    eng = FakeEngine()
    trader, gate, events = make(eng)
    gate.start()
    eng.close()
    try:
        for _ in range(10):
            trader.tick_once()
    except GateStop as e:
        assert e.code == 2 and "consecutive" in e.reason
    else:
        raise AssertionError("expected GateStop")
    assert kinds(events).count("gate_error") == 5
    assert "fill" not in kinds(events)


def test_run_emits_stop_and_raises_on_five_errors():
    eng = FakeEngine()
    trader, gate, events = make(eng)
    eng.tick_status = 503
    try:
        trader.run(max_iterations=20)
    except GateStop as e:
        assert e.code == 2
    else:
        raise AssertionError("expected GateStop")
    stop = events[-1]
    assert stop["event"] == "stop" and stop["exit_code"] == 2 and stop["gate_errors"] == 5
    eng.close()


def test_tick_503_skips_the_bar_and_makes_no_decision():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        gate.start()
        eng.tick_status = 503
        trader.tick_once()
        trader.tick_once()
        errs = [e for e in events if e["event"] == "gate_error"]
        assert [e["kind"] for e in errs] == ["tick_busy", "tick_busy"]
        assert all(e["action"] == "skip_bar" for e in errs)
        assert eng.decision_requests == []              # no decision on a stale mark
        eng.tick_status = 200
        trader.tick_once()                              # recovers; the count resets
        assert kinds(events).count("verdict") == 1
        assert gate._consecutive == 0
    finally:
        eng.close()


def test_read_timeout_is_unknown_and_the_replay_returns_the_stored_result():
    eng = FakeEngine()
    try:
        eng.decision_script = ["slow"]
        trader, gate, events = make(eng, read_timeout=0.2)
        trader.run(max_iterations=1)
        unk = [e for e in events if e["event"] == "gate_unknown"]
        assert len(unk) == 1 and unk[0]["kind"] == "read_timeout"
        assert unk[0]["action"] == "pause_and_retry"
        # Same client_id re-sent; the engine placed exactly one order.
        ids = [r["client_id"] for r in eng.decision_requests]
        assert len(ids) == 2 and ids[0] == ids[1] == unk[0]["client_id"]
        assert eng.orders_placed == 1
        v = [e for e in events if e["event"] == "verdict"]
        assert len(v) == 1 and v[0]["replayed"] is True and v[0]["approved"]
        assert kinds(events).count("fill") == 1
    finally:
        eng.close()


def test_http_504_is_unknown_then_replayed_not_a_rejection():
    eng = FakeEngine()
    try:
        eng.decision_script = ["504"]
        trader, gate, events = make(eng)
        trader.run(max_iterations=1)
        unk = [e for e in events if e["event"] == "gate_unknown"]
        assert len(unk) == 1 and unk[0]["kind"] == "http_504"
        assert eng.orders_placed == 1 and len(eng.decision_requests) == 2
        v = next(e for e in events if e["event"] == "verdict")
        assert v["approved"] and v["replayed"]
        assert "gate_error" not in kinds(events)
    finally:
        eng.close()


def test_dropped_connection_is_unknown_not_an_error():
    eng = FakeEngine()
    try:
        eng.decision_script = ["drop"]
        trader, gate, events = make(eng)
        trader.run(max_iterations=1)
        unk = [e for e in events if e["event"] == "gate_unknown"]
        assert len(unk) == 1 and unk[0]["kind"] in ("disconnected", "io_error")
        assert eng.orders_placed == 1
        assert kinds(events).count("fill") == 1
    finally:
        eng.close()


def test_unknown_for_30_seconds_stops_with_exit_2():
    eng = FakeEngine()
    try:
        clock = [0.0]
        eng.decision_script = ["504"]

        def always_504(h, body):          # every retry also fails
            eng.decision_requests.append(body)
            h._send(504, {"error": "context deadline exceeded"})
        eng._decision = always_504
        trader, gate, events = make(
            eng, clock=lambda: clock[0],
            sleep=lambda s: clock.__setitem__(0, clock[0] + 10.0))
        try:
            trader.run(max_iterations=1)
        except GateStop as e:
            assert e.code == 2 and "still unknown" in e.reason
        else:
            raise AssertionError("expected GateStop")
        ids = {r["client_id"] for r in eng.decision_requests}
        assert len(ids) == 1 and len(eng.decision_requests) >= 2   # always the SAME id
        assert "fill" not in kinds(events)
        assert events[-1]["event"] == "stop" and events[-1]["exit_code"] == 2
    finally:
        eng.close()


def test_boot_id_mismatch_409_stops_the_run():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        gate.start()
        eng.decision_script = ["409"]
        try:
            trader.tick_once()
        except GateStop as e:
            assert e.code == 2 and "409" in e.reason
        else:
            raise AssertionError("expected GateStop")
        assert "fill" not in kinds(events)
    finally:
        eng.close()


def test_engine_restart_between_bars_stops_before_any_trade():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        gate.start()
        eng.boot_id = "boot-2"                         # restarted: the book is gone
        try:
            trader.tick_once()
        except GateStop as e:
            assert e.code == 2 and "boot_id changed" in e.reason
        else:
            raise AssertionError("expected GateStop")
        assert eng.decision_requests == []
        assert eng.count("POST", "/tick") == 0
    finally:
        eng.close()


def test_503_on_a_decision_stops_the_run():
    eng = FakeEngine()
    try:
        eng.decision_script = ["503"]
        trader, gate, events = make(eng)
        try:
            trader.run(max_iterations=1)
        except GateStop as e:
            assert e.code == 2 and "shutting down" in e.reason
        else:
            raise AssertionError("expected GateStop")
    finally:
        eng.close()


def test_nan_confidence_fails_closed_locally():
    eng = FakeEngine()
    try:
        trader, gate, events = make(eng)
        gate.start()
        from tradebot.core.types import Decision
        d = Decision(T0, "BTC-USD", Side.BUY, float("nan"), 1.0, "x")
        gate.decide(Signal(T0, "BTC-USD", Side.BUY, 0.9), d, None, 100.0, "strategy")
        err = [e for e in events if e["event"] == "gate_error"]
        assert len(err) == 1 and err[0]["kind"] == "bad_decision" and err[0]["action"] == "no_trade"
        assert eng.decision_requests == []
    finally:
        eng.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
