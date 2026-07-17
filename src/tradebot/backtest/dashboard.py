"""Render a backtest result to a self-contained, theme-aware HTML dashboard.

No external assets — inline SVG equity curve, stat tiles, and trade log.
`render_body` returns just the content (for embedding / Artifacts); `render_page`
wraps it in a full standalone HTML document you can open locally.

Colors use the validated dataviz palette: one blue hue for the single equity
series, reserved green/red status colors for P&L.
"""
from __future__ import annotations

import html
from datetime import datetime

# Validated dataviz palette (light / dark).
_C = {
    "series_l": "#2a78d6", "series_d": "#3987e5",
    "good_l": "#008300", "good_d": "#00a300",
    "bad_l": "#e34948", "bad_d": "#e66767",
}


def _fmt(x: float, pct: bool = False, sign: bool = False) -> str:
    s = f"{x:+.2f}" if sign else f"{x:,.2f}"
    return s + "%" if pct else s


def _equity_svg(equity: list[float], w: int = 900, h: int = 300, pad: int = 8) -> str:
    if len(equity) < 2:
        return "<p>no data</p>"
    lo, hi = min(equity), max(equity)
    rng = (hi - lo) or 1.0
    n = len(equity)
    def x(i): return pad + i / (n - 1) * (w - 2 * pad)
    def y(v): return pad + (1 - (v - lo) / rng) * (h - 2 * pad)
    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(equity))
    base_y = y(equity[0])
    # Area under curve for a soft fill.
    area = f"{x(0):.1f},{h - pad} " + pts + f" {x(n-1):.1f},{h - pad}"
    return f"""<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="none"
     role="img" aria-label="Equity curve" style="display:block">
  <line x1="{pad}" y1="{base_y:.1f}" x2="{w-pad}" y2="{base_y:.1f}"
        stroke="var(--muted)" stroke-width="1" stroke-dasharray="4 4"/>
  <polygon points="{area}" fill="var(--series)" opacity="0.08"/>
  <polyline points="{pts}" fill="none" stroke="var(--series)"
            stroke-width="2" stroke-linejoin="round"/>
</svg>"""


def _tile(label: str, value: str, cls: str = "") -> str:
    return (f'<div class="tile {cls}"><div class="tile-v">{value}</div>'
            f'<div class="tile-l">{html.escape(label)}</div></div>')


def render_body(result, title: str = "Backtest") -> str:
    m = result.metrics
    ret_cls = "good" if m.total_return_pct >= 0 else "bad"
    tiles = "".join([
        _tile("Total return", _fmt(m.total_return_pct, pct=True, sign=True), ret_cls),
        _tile("Sharpe", _fmt(m.sharpe), "good" if m.sharpe >= 1 else ""),
        _tile("Max drawdown", _fmt(m.max_drawdown_pct, pct=True), "bad"),
        _tile("Win rate", _fmt(m.win_rate_pct, pct=True)),
        _tile("Profit factor", _fmt(m.profit_factor)),
        _tile("Trades", str(m.n_trades)),
    ])

    rows = []
    for t in (result.trades or [])[-25:][::-1]:
        pnl_cls = "good" if t.pnl >= 0 else "bad"
        rows.append(
            f"<tr><td>{html.escape(t.symbol)}</td>"
            f"<td>{t.side.value}</td>"
            f"<td>{t.entry_ts:%Y-%m-%d %H:%M}</td>"
            f"<td>{t.entry_price:,.2f}</td><td>{t.exit_price:,.2f}</td>"
            f"<td class='{pnl_cls}'>{_fmt(t.pnl, sign=True)}</td>"
            f"<td>{html.escape(t.reason_exit)}</td></tr>")
    trade_table = "".join(rows) or "<tr><td colspan='7'>no trades</td></tr>"

    eq_start = result.equity_curve[0] if result.equity_curve else 0
    eq_end = result.equity_curve[-1] if result.equity_curve else 0
    generated = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    return f"""
<div class="viz-root">
  <style>{_CSS}</style>
  <header class="hdr">
    <h1>{html.escape(title)}</h1>
    <p class="sub">Equity {eq_start:,.0f} → {eq_end:,.0f} · generated {generated}</p>
  </header>
  <section class="tiles">{tiles}</section>
  <section class="card">
    <h2>Equity curve</h2>
    {_equity_svg(result.equity_curve)}
  </section>
  <section class="card">
    <h2>Recent trades</h2>
    <div class="tbl-wrap"><table>
      <thead><tr><th>Symbol</th><th>Side</th><th>Entry time</th>
        <th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Exit reason</th></tr></thead>
      <tbody>{trade_table}</tbody>
    </table></div>
  </section>
  <footer class="ft">Paper backtest · not financial advice · brain proposes, risk engine disposes</footer>
</div>"""


def render_page(result, title: str = "Backtest") -> str:
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title></head><body>"
            f"{render_body(result, title)}</body></html>")


_CSS = """
.viz-root{--series:#2a78d6;--good:#008300;--bad:#e34948;--surface:#fcfcfb;
  --card:#ffffff;--ink:#0b0b0b;--muted:#8a8a86;--border:#e7e7e2;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  color:var(--ink);background:var(--surface);padding:24px;max-width:960px;margin:0 auto}
@media (prefers-color-scheme:dark){.viz-root:where(:not([data-theme=light])){
  --series:#3987e5;--good:#00a300;--bad:#e66767;--surface:#1a1a19;
  --card:#242422;--ink:#fff;--muted:#8f8f88;--border:#33332f}}
:root[data-theme=dark] .viz-root{--series:#3987e5;--good:#00a300;--bad:#e66767;
  --surface:#1a1a19;--card:#242422;--ink:#fff;--muted:#8f8f88;--border:#33332f}
.viz-root h1{font-size:22px;margin:0}.viz-root h2{font-size:14px;margin:0 0 12px;
  color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.hdr .sub{color:var(--muted);margin:4px 0 20px;font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
  gap:12px;margin-bottom:20px}
.tile{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px}
.tile-v{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.tile-l{font-size:12px;color:var(--muted);margin-top:4px}
.tile.good .tile-v{color:var(--good)}.tile.bad .tile-v{color:var(--bad)}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:18px;margin-bottom:20px}
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:13px;
  font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--border)}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
thead th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase}
td.good{color:var(--good)}td.bad{color:var(--bad)}
.ft{color:var(--muted);font-size:12px;text-align:center;margin-top:8px}
"""
