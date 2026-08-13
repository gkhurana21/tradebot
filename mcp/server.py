#!/usr/bin/env python3
"""
tradebot MCP bridge — lets Claude run backtests, sweep parameters, and draw
charts against the local tradebot engine.

Speaks MCP over stdio (JSON-RPC 2.0). Deliberately stdlib-only: the tradebot
venv already has the engine's dependencies, and adding an SDK here would be one
more thing to keep working.

Nothing in this file can place an order. Every tool is read-only research
against the paper broker and synthetic/historical candles.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CHART_DIR = Path(os.environ.get("TRADEBOT_CHART_DIR", "/tmp/tradebot-charts"))

# ── engine imports (deferred so a broken env reports cleanly, not on startup) ──
def _engine():
    from tradebot.backtest.engine import Backtester
    from tradebot.brain.brain import Brain
    from tradebot.brokers.paper import PaperBroker
    from tradebot.core.risk import RiskConfig, RiskEngine
    from tradebot.core.strategy import EmaRsiStrategy
    from tradebot.data import feed
    return Backtester, Brain, PaperBroker, RiskConfig, RiskEngine, EmaRsiStrategy, feed


def run_backtest(source="synthetic", symbol="BTC-USD", bars=1000, cash=10_000.0,
                 risk_profile="balanced", csv_path="", seed=None,
                 fee_pct=0.001, slippage_pct=0.0005, **risk_over):
    Backtester, Brain, PaperBroker, RiskConfig, RiskEngine, EmaRsiStrategy, feed = _engine()

    if source == "coinbase":
        candles = feed.coinbase_candles(symbol, granularity=3600, limit=min(int(bars), 300))
    elif source == "csv":
        if not csv_path:
            raise ValueError("csv source needs csv_path")
        candles = feed.csv_candles(csv_path, symbol)
    else:
        candles = list(feed.synthetic_candles(symbol, n=int(bars),
                                              seed=42 if seed is None else int(seed)))

    cfg = RiskConfig.profile(risk_profile) if hasattr(RiskConfig, "profile") else RiskConfig()
    for k, v in risk_over.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)

    broker = PaperBroker(starting_cash=float(cash),
                         fee_pct=float(fee_pct), slippage_pct=float(slippage_pct))
    bt = Backtester(EmaRsiStrategy(), Brain(use_llm=False), RiskEngine(cfg), broker)
    res = bt.run(candles)

    m = res.metrics
    return {
        "symbol": symbol, "source": source, "bars": int(bars),
        "risk_profile": risk_profile,
        "starting_equity": round(res.starting_equity, 2),
        "ending_equity": round(res.ending_equity, 2),
        "total_return_pct": round(res.total_return_pct, 4),
        "max_drawdown_pct": round(res.max_drawdown_pct, 4),
        "sharpe": round(getattr(m, "sharpe", 0.0), 4),
        "win_rate_pct": round(getattr(m, "win_rate_pct", 0.0), 2),
        "profit_factor": round(getattr(m, "profit_factor", 0.0), 4),
        "n_trades": res.n_trades, "n_signals": res.n_signals,
        "n_rejected": res.n_rejected, "n_stops": res.n_stops, "n_targets": res.n_targets,
        "equity_curve": [round(x, 2) for x in res.equity_curve],
        "risk_config": {k: getattr(cfg, k) for k in vars(cfg)} if hasattr(cfg, "__dict__") else {},
    }


def sweep(param, values, **base):
    """Run the same backtest across one parameter and rank the outcomes."""
    rows = []
    for v in values:
        try:
            r = run_backtest(**{**base, param: v})
            rows.append({param: v, "return_pct": r["total_return_pct"],
                         "max_dd_pct": r["max_drawdown_pct"], "sharpe": r["sharpe"],
                         "trades": r["n_trades"], "win_rate_pct": r["win_rate_pct"]})
        except Exception as e:  # one bad value must not kill the sweep
            rows.append({param: v, "error": str(e)[:120]})
    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: r["sharpe"], reverse=True)
    return {"param": param, "tested": len(rows), "ranked_by_sharpe": ok,
            "errors": [r for r in rows if "error" in r],
            "best": ok[0] if ok else None}


def _svg_chart(curve, title, subtitle=""):
    """Hand-rolled SVG so charting needs no plotting library."""
    W, H, P = 760, 300, 44
    if len(curve) < 2:
        curve = curve * 2 or [0, 0]
    lo, hi = min(curve), max(curve)
    rng = (hi - lo) or 1.0
    n = len(curve)
    x = lambda i: P + i / (n - 1) * (W - 2 * P)
    y = lambda v: H - P - (v - lo) / rng * (H - 2 * P)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(curve))

    start = curve[0]
    base_y = y(start)
    up = curve[-1] >= start
    stroke = "#4ade80" if up else "#f87171"
    # peak-to-trough drawdown band
    peak, dd_i, dd_peak_i, worst = curve[0], 0, 0, 0.0
    pk_i = 0
    for i, v in enumerate(curve):
        if v > peak:
            peak, pk_i = v, i
        d = (peak - v) / peak * 100 if peak else 0
        if d > worst:
            worst, dd_i, dd_peak_i = d, i, pk_i

    ticks = "".join(
        f'<line x1="{P}" x2="{W-P}" y1="{y(lo+rng*f):.1f}" y2="{y(lo+rng*f):.1f}" '
        f'stroke="#2a2a2a" stroke-width="1"/>'
        f'<text x="{P-8}" y="{y(lo+rng*f)+4:.1f}" fill="#777" font-size="10" '
        f'text-anchor="end" font-family="ui-monospace,monospace">{lo+rng*f:,.0f}</text>'
        for f in (0, .25, .5, .75, 1))

    dd_band = ""
    if worst > 0.01:
        dd_band = (f'<rect x="{x(dd_peak_i):.1f}" y="{P}" width="{max(1,x(dd_i)-x(dd_peak_i)):.1f}" '
                   f'height="{H-2*P}" fill="#f87171" opacity="0.08"/>')

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img">
<title>{title}</title>
<rect width="{W}" height="{H}" fill="#0f0f0f"/>
{ticks}{dd_band}
<line x1="{P}" x2="{W-P}" y1="{base_y:.1f}" y2="{base_y:.1f}" stroke="#666" stroke-dasharray="4 4" stroke-width="1"/>
<polyline points="{pts}" fill="none" stroke="{stroke}" stroke-width="2" stroke-linejoin="round"/>
<text x="{P}" y="24" fill="#e8e4da" font-size="13" font-family="ui-sans-serif,system-ui">{title}</text>
<text x="{P}" y="40" fill="#888" font-size="11" font-family="ui-monospace,monospace">{subtitle}</text>
<text x="{W-P}" y="24" fill="{stroke}" font-size="13" text-anchor="end" font-family="ui-monospace,monospace">{(curve[-1]/start-1)*100:+.2f}%</text>
<text x="{W-P}" y="40" fill="#f87171" font-size="10" text-anchor="end" font-family="ui-monospace,monospace">max DD {worst:.2f}%</text>
</svg>'''


def chart(**kw):
    r = run_backtest(**kw)
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    path = CHART_DIR / f"equity-{r['symbol'].replace('/','-')}-{int(time.time())}.svg"
    sub = (f"{r['source']} · {r['bars']} bars · {r['risk_profile']} · "
           f"Sharpe {r['sharpe']} · {r['n_trades']} trades")
    path.write_text(_svg_chart(r["equity_curve"], f"{r['symbol']} equity curve", sub))
    r.pop("equity_curve", None)
    r["chart_path"] = str(path)
    return r


# ── MCP plumbing ─────────────────────────────────────────────────────────────
NUM = {"type": "number"}
TOOLS = [
    {"name": "backtest_run",
     "description": "Run a tradebot backtest on synthetic or historical candles and return performance metrics (return, max drawdown, Sharpe, win rate, trade counts). Paper only — never places an order.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string", "enum": ["synthetic", "coinbase", "csv"]},
         "symbol": {"type": "string"}, "bars": {"type": "integer"},
         "cash": NUM, "risk_profile": {"type": "string", "enum": ["conservative", "balanced", "aggressive"]},
         "seed": {"type": "integer"}, "csv_path": {"type": "string"},
         "stop_loss_pct": NUM, "take_profit_pct": NUM,
         "risk_per_trade_pct": NUM, "max_position_pct": NUM, "min_confidence": NUM,
         "fee_pct": NUM, "slippage_pct": NUM}}},
    {"name": "backtest_sweep",
     "description": "Run the same backtest across a range of values for one parameter and rank results by Sharpe. Use to find which stop/target/confidence setting actually holds up.",
     "inputSchema": {"type": "object", "required": ["param", "values"], "properties": {
         "param": {"type": "string", "description": "e.g. stop_loss_pct, take_profit_pct, min_confidence, risk_per_trade_pct, bars"},
         "values": {"type": "array", "items": NUM},
         "source": {"type": "string"}, "symbol": {"type": "string"},
         "bars": {"type": "integer"}, "risk_profile": {"type": "string"}, "seed": {"type": "integer"}}}},
    {"name": "backtest_chart",
     "description": "Run a backtest and render its equity curve to an SVG chart (drawdown band shaded). Returns metrics plus the chart file path.",
     "inputSchema": {"type": "object", "properties": {
         "source": {"type": "string"}, "symbol": {"type": "string"},
         "bars": {"type": "integer"}, "risk_profile": {"type": "string"},
         "seed": {"type": "integer"}, "stop_loss_pct": NUM, "take_profit_pct": NUM}}},
    {"name": "market_quote",
     "description": "Live quote for a stock/ETF or crypto symbol via the local QuantCore proxy (must be running on :8080).",
     "inputSchema": {"type": "object", "required": ["symbol"],
                     "properties": {"symbol": {"type": "string"}}}},
]


def market_quote(symbol):
    import urllib.request
    url = os.environ.get("QUANTCORE_PROXY", "http://localhost:8080")
    with urllib.request.urlopen(f"{url}/v1/quote?symbol={symbol}", timeout=8) as r:
        return json.loads(r.read())


HANDLERS = {"backtest_run": run_backtest, "backtest_chart": chart,
            "market_quote": lambda **k: market_quote(k["symbol"]),
            "backtest_sweep": lambda **k: sweep(k.pop("param"), k.pop("values"), **k)}


def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    msg["error" if error else "result"] = error or result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = req.get("method"), req.get("id")
        if method == "initialize":
            reply(mid, {"protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "tradebot", "version": "0.1.0"}})
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            p = req.get("params") or {}
            name, args = p.get("name"), (p.get("arguments") or {})
            try:
                out = HANDLERS[name](**args)
                reply(mid, {"content": [{"type": "text", "text": json.dumps(out, indent=1, default=str)}]})
            except KeyError:
                reply(mid, error={"code": -32601, "message": f"unknown tool: {name}"})
            except Exception as e:
                reply(mid, {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True})
        elif mid is not None:
            reply(mid, {})


if __name__ == "__main__":
    main()
