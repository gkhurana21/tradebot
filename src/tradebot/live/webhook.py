"""TradingView webhook receiver.

TradingView Pine Script alerts POST a JSON body to a URL. This stdlib HTTP
server accepts them, authenticates with a shared secret, converts each into a
`Signal`, and hands it to a callback (typically LiveTrader.submit_external_signal).

Pine alert message (set in the TradingView alert dialog), e.g.:
    {"secret": "YOUR_SECRET", "symbol": "BTC-USD", "side": "buy",
     "strength": 0.8, "reason": "supertrend flip", "price": {{close}}}

Security notes:
  * The shared secret gates every request; set it via env TRADINGVIEW_SECRET.
  * Bind to localhost behind a tunnel/reverse proxy for real use; never expose
    an unauthenticated port. This receiver only ingests signals — it cannot move
    money on its own (the risk engine still gates everything downstream).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from ..core.types import Side, Signal

SignalSink = Callable[[Signal], None]


def _parse_side(raw: str) -> Side:
    s = (raw or "").strip().lower()
    if s in ("buy", "long", "bull"):
        return Side.BUY
    if s in ("sell", "short", "bear"):
        return Side.SELL
    return Side.FLAT


def parse_alert(payload: dict) -> Signal | None:
    symbol = payload.get("symbol") or payload.get("ticker")
    side = _parse_side(payload.get("side") or payload.get("action", ""))
    if not symbol or side == Side.FLAT:
        return None
    try:
        strength = float(payload.get("strength", 0.7))
    except (TypeError, ValueError):
        strength = 0.7
    features = {"source": "tradingview", "strategy": "tv_alert"}
    if "price" in payload:
        try:
            features["price"] = float(payload["price"])
        except (TypeError, ValueError):
            pass
    return Signal(
        ts=datetime.now(tz=timezone.utc),
        symbol=str(symbol),
        side=side,
        strength=max(0.0, min(1.0, strength)),
        reason=str(payload.get("reason", "TradingView alert"))[:200],
        features=features,
    )


def make_handler(sink: SignalSink, secret: str, on_event=None):
    log = on_event or (lambda e: None)

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # health check
            if self.path == "/health":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/webhook":
                return self._reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "invalid JSON"})
            if secret and payload.get("secret") != secret:
                log({"event": "webhook_rejected", "reason": "bad secret"})
                return self._reply(401, {"error": "unauthorized"})
            signal = parse_alert(payload)
            if signal is None:
                return self._reply(422, {"error": "could not parse signal"})
            sink(signal)
            log({"event": "webhook_signal", "symbol": signal.symbol,
                 "side": signal.side.value, "strength": signal.strength})
            return self._reply(200, {"status": "accepted",
                                     "symbol": signal.symbol,
                                     "side": signal.side.value})

        def log_message(self, *args):  # silence default stderr logging
            pass

    return Handler


def serve(
    sink: SignalSink,
    host: str = "127.0.0.1",
    port: int = 8787,
    secret: str | None = None,
    on_event=None,
) -> ThreadingHTTPServer:
    secret = secret if secret is not None else os.environ.get("TRADINGVIEW_SECRET", "")
    handler = make_handler(sink, secret, on_event)
    httpd = ThreadingHTTPServer((host, port), handler)
    return httpd
