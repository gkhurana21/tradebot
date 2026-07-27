"""Unified command line: `python -m tradebot <command>`.

Commands:
  backtest    single-symbol backtest
  portfolio   multi-symbol ensemble backtest
  optimize    walk-forward parameter search
  live        live paper-trading loop (synthetic or coinbase feed)
  webhook     run the TradingView webhook receiver -> live paper trader
  dashboard   write an HTML report from a portfolio backtest
  onchain     print a live on-chain snapshot
"""
from __future__ import annotations

import argparse
import json
import sys

from .backtest.dashboard import render_page
from .backtest.engine import Backtester
from .backtest.optimize import walk_forward
from .backtest.portfolio import PortfolioBacktester
from .brain.brain import Brain, _llm_available
from .brokers.paper import PaperBroker
from .core.risk import RiskConfig, RiskEngine
from .core.strategy import (
    BollingerReversion, EmaRsiStrategy, EnsembleStrategy, PatternStrategy)
from .data import feed
from .live.engine import LiveConfig, LiveTrader


def _ensemble():
    return EnsembleStrategy(EmaRsiStrategy(), BollingerReversion(),
                            PatternStrategy(), require=1)


def _risk(a) -> RiskConfig:
    prof = getattr(a, "profile", None)
    cfg = RiskConfig.profile(prof) if prof else RiskConfig()
    n = len(_split(getattr(a, "symbols", "") or "x"))
    cfg.max_open_positions = max(cfg.max_open_positions, n)
    return cfg


_MODELS = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-5",
           "haiku": "claude-haiku-4-5"}


def _brain(a) -> Brain:
    model = _MODELS.get(getattr(a, "model", None) or "opus", "claude-opus-4-8")
    return Brain(use_llm=not a.no_llm, model=model,
                 max_calls=getattr(a, "max_llm_calls", None))


def _cost_note(a, brain: Brain) -> None:
    if brain.use_llm and _llm_available() and brain.calls_made:
        print(brain.cost_report())


def _load_streams(source, symbols, bars):
    streams = {}
    for i, sym in enumerate(symbols):
        if source == "synthetic":
            streams[sym] = list(feed.synthetic_candles(
                sym, n=bars, seed=42 + i, start_price=[30_000, 2_000, 100][i % 3]))
        else:
            streams[sym] = feed.coinbase_candles(sym, granularity=3600,
                                                 limit=min(bars, 300))
    return streams


def cmd_backtest(a):
    candles = (list(feed.synthetic_candles(a.symbol, n=a.bars))
               if a.source == "synthetic"
               else feed.coinbase_candles(a.symbol, granularity=3600, limit=min(a.bars, 300)))
    print(f"Loaded {len(candles)} {a.symbol} candles ({a.source})")
    brain = _brain(a)
    bt = Backtester(_ensemble(), brain,
                    RiskEngine(_risk(a)), PaperBroker(a.cash), verbose=a.verbose)
    res = bt.run(candles)
    _print_mode(a)
    print(res.summary()); print(res.metrics.table()); _cost_note(a, brain)


def cmd_portfolio(a):
    symbols = _split(a.symbols)
    streams = _load_streams(a.source, symbols, a.bars)
    for s, c in streams.items():
        print(f"Loaded {len(c)} {s} candles ({a.source})")
    ctx = None
    if a.onchain:
        from .data.onchain import fetch_snapshot
        snap = fetch_snapshot(); feats = snap.features()
        print(f"On-chain: {feats} ok={snap.ok}")
        ctx = (lambda sym: {"onchain": feats}) if snap.ok else None
    brain = _brain(a)
    bt = PortfolioBacktester(_ensemble, brain,
                             RiskEngine(_risk(a)),
                             PaperBroker(a.cash), context_provider=ctx, verbose=a.verbose)
    res = bt.run(streams)
    _print_mode(a)
    print(res.summary()); print(res.metrics.table()); _cost_note(a, brain)


def cmd_optimize(a):
    candles = list(feed.synthetic_candles(a.symbol, n=a.bars, seed=1))
    grid = {"fast": [8, 12, 16], "slow": [26, 34, 50], "rsi_period": [14]}
    print(f"Grid-searching {len(candles)} bars, {a.folds} walk-forward folds...")
    results = walk_forward(lambda **p: EmaRsiStrategy(**p), grid, candles, folds=a.folds)
    print(f"\n{'params':<40}{'OOS':>8}{'IS':>8}{'folds':>7}")
    for r in results[:8]:
        print(f"{json.dumps(r.params):<40}{r.oos_score:>8.3f}{r.is_score:>8.3f}{r.folds:>7}")
    if results:
        best = results[0]
        print(f"\nBest out-of-sample: {best.params}  (OOS={best.oos_score:.3f})")


def cmd_live(a):
    symbols = _split(a.symbols)
    if a.source == "synthetic":
        tick = feed.make_synthetic_ticker(symbols[0])
    else:
        tick = lambda sym: feed.coinbase_latest(sym, granularity=60)
    trader = LiveTrader(
        _ensemble, Brain(use_llm=not a.no_llm),
        RiskEngine(_risk(a)),
        PaperBroker(a.cash), fetch_latest=tick,
        config=LiveConfig(symbols=symbols, poll_seconds=a.poll, onchain_context=a.onchain),
        on_event=lambda e: print(json.dumps(e)))
    print(f"Live paper trader starting ({a.source} feed, {len(symbols)} symbols). "
          f"Ctrl-C to stop.")
    try:
        trader.run(max_iterations=a.iterations)
    except KeyboardInterrupt:
        trader.stop()
        print("\nStopped.")


def cmd_webhook(a):
    from .live.webhook import serve
    symbols = _split(a.symbols)
    tick = feed.make_synthetic_ticker(symbols[0]) if a.source == "synthetic" \
        else (lambda sym: feed.coinbase_latest(sym, granularity=60))
    trader = LiveTrader(_ensemble, Brain(use_llm=not a.no_llm),
                        RiskEngine(RiskConfig(max_open_positions=len(symbols))),
                        PaperBroker(a.cash), fetch_latest=tick,
                        config=LiveConfig(symbols=symbols, poll_seconds=a.poll),
                        on_event=lambda e: print(json.dumps(e)))
    import threading
    threading.Thread(target=lambda: trader.run(), daemon=True).start()
    httpd = serve(trader.submit_external_signal, host=a.host, port=a.port,
                  secret=a.secret, on_event=lambda e: print(json.dumps(e)))
    print(f"Webhook receiver on http://{a.host}:{a.port}/webhook  "
          f"(health: /health). Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
        print("\nStopped.")


def cmd_dashboard(a):
    symbols = _split(a.symbols)
    streams = _load_streams("synthetic", symbols, a.bars)
    bt = PortfolioBacktester(_ensemble, Brain(use_llm=False),
                             RiskEngine(RiskConfig(max_open_positions=len(symbols))),
                             PaperBroker(a.cash))
    res = bt.run(streams)
    with open(a.out, "w") as f:
        f.write(render_page(res, a.title))
    print(f"Wrote {a.out}")
    print(res.summary())


def cmd_compare(a):
    """Run the free heuristic and the real Claude brain on identical data."""
    if a.source == "synthetic":
        candles = list(feed.synthetic_candles(a.symbol, n=a.bars, seed=7))
    else:
        candles = feed.coinbase_candles(a.symbol, granularity=3600,
                                        limit=min(a.bars, 300))
    print(f"Comparing brains on {len(candles)} {a.symbol} candles ({a.source})\n")

    def run(brain):
        bt = Backtester(_ensemble(), brain, RiskEngine(_risk(a)),
                        PaperBroker(a.cash))
        return bt.run(list(candles))

    # 1) Free heuristic baseline.
    heur = Brain(use_llm=False)
    r_heur = run(heur)

    # 2) Real Claude brain (cost-guarded).
    llm_available = _llm_available()
    if not llm_available:
        print("Claude brain unavailable (need ANTHROPIC_API_KEY + `pip install "
              "anthropic`). Showing heuristic only.\n")
    model = _MODELS.get(a.model, "claude-opus-4-8")
    llm = Brain(use_llm=llm_available, model=model, max_calls=a.max_llm_calls)
    r_llm = run(llm) if llm_available else None

    def row(label, hv, lv):
        return f"  {label:<16}{hv:>14}{lv:>14}"
    print(row("metric", "heuristic", "Claude"))
    print("  " + "-" * 44)
    pairs = [
        ("Return %", f"{r_heur.total_return_pct:+.2f}",
         f"{r_llm.total_return_pct:+.2f}" if r_llm else "—"),
        ("Sharpe", f"{r_heur.metrics.sharpe:.2f}",
         f"{r_llm.metrics.sharpe:.2f}" if r_llm else "—"),
        ("Max DD %", f"{r_heur.max_drawdown_pct:.2f}",
         f"{r_llm.max_drawdown_pct:.2f}" if r_llm else "—"),
        ("Win %", f"{r_heur.metrics.win_rate_pct:.0f}",
         f"{r_llm.metrics.win_rate_pct:.0f}" if r_llm else "—"),
        ("Profit factor", f"{r_heur.metrics.profit_factor:.2f}",
         f"{r_llm.metrics.profit_factor:.2f}" if r_llm else "—"),
        ("Trades", f"{r_heur.metrics.n_trades}",
         f"{r_llm.metrics.n_trades}" if r_llm else "—"),
    ]
    for label, hv, lv in pairs:
        print(row(label, hv, lv))
    if r_llm:
        print(f"\n{llm.cost_report()}")
        edge = r_llm.total_return_pct - r_heur.total_return_pct
        verdict = ("Claude beat the heuristic" if edge > 0
                   else "the free heuristic held up" if edge < 0
                   else "a tie")
        print(f"\nOn this run, {verdict} ({edge:+.2f}% return difference). "
              f"One run isn't proof — try a few symbols and windows.")


def cmd_analyze(a):
    from .core.patterns import analyze
    if a.source == "synthetic":
        candles = list(feed.synthetic_candles(a.symbol, n=a.bars, seed=a.seed))
    else:
        candles = feed.coinbase_candles(a.symbol, granularity=a.granularity,
                                        limit=min(a.bars, 300))
    print(f"Analyzing {len(candles)} {a.symbol} candles ({a.source})\n")
    print(analyze(candles).summary())
    print("\n(Chart read only — patterns describe the past; they don't guarantee "
          "the next move.)")


def cmd_montecarlo(a):
    from .sim.montecarlo import MCParams, simulate
    p = MCParams(start=a.start, target=a.target, win_prob=a.win_prob,
                 reward_risk=a.reward_risk, risk_frac=a.risk, fee_frac=a.fee,
                 min_stake=a.min_stake, max_trades=a.max_trades)
    res = simulate(p, n_traders=a.traders, seed=a.seed)
    print(res.report(p))
    if res.p_target < 0.01:
        print(f"\n→ Out of {a.traders:,} attempts, essentially none reached the "
              f"target without busting first. That is the honest answer to "
              f"'$1 to a million': the same aggression needed to compound there "
              f"is what makes ruin near-certain.")


def cmd_breakeven(a):
    from .sim.breakeven import CostModel, report
    costs = CostModel(fee_pct=a.fee, slippage_pct=a.slippage)
    print(report(win_rate=a.win_rate, reward_risk=a.reward_risk, costs=costs))


def cmd_onchain(a):
    from .data.onchain import fetch_snapshot
    snap = fetch_snapshot()
    print(json.dumps({"ok": snap.ok, **snap.features()}, indent=2))


def _split(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def _print_mode(a):
    mode = "LLM (Claude)" if (not a.no_llm and _llm_available()) else "heuristic (offline)"
    print(f"Brain mode: {mode}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tradebot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--no-llm", action="store_true")
        sp.add_argument("--cash", type=float, default=10_000.0)
        sp.add_argument("--profile", choices=["conservative", "balanced", "aggressive"],
                        default=None, help="risk profile (aggressive = higher risk of ruin)")
        sp.add_argument("--model", choices=["opus", "sonnet", "haiku"], default="opus",
                        help="Claude model for the brain (haiku is ~5x cheaper for testing)")
        sp.add_argument("--max-llm-calls", type=int, default=None, dest="max_llm_calls",
                        help="cost guard: cap API calls per run, then fall back to heuristic")

    b = sub.add_parser("backtest"); common(b)
    b.add_argument("--source", choices=["synthetic", "coinbase"], default="synthetic")
    b.add_argument("--symbol", default="BTC-USD"); b.add_argument("--bars", type=int, default=1000)
    b.add_argument("--verbose", action="store_true"); b.set_defaults(func=cmd_backtest)

    pf = sub.add_parser("portfolio"); common(pf)
    pf.add_argument("--source", choices=["synthetic", "coinbase"], default="synthetic")
    pf.add_argument("--symbols", default="BTC-USD,ETH-USD,SOL-USD")
    pf.add_argument("--bars", type=int, default=1500); pf.add_argument("--onchain", action="store_true")
    pf.add_argument("--verbose", action="store_true"); pf.set_defaults(func=cmd_portfolio)

    o = sub.add_parser("optimize")
    o.add_argument("--symbol", default="BTC-USD"); o.add_argument("--bars", type=int, default=2000)
    o.add_argument("--folds", type=int, default=4); o.set_defaults(func=cmd_optimize)

    lv = sub.add_parser("live"); common(lv)
    lv.add_argument("--source", choices=["synthetic", "coinbase"], default="synthetic")
    lv.add_argument("--symbols", default="BTC-USD"); lv.add_argument("--poll", type=float, default=2.0)
    lv.add_argument("--iterations", type=int, default=None); lv.add_argument("--onchain", action="store_true")
    lv.set_defaults(func=cmd_live)

    wh = sub.add_parser("webhook"); common(wh)
    wh.add_argument("--source", choices=["synthetic", "coinbase"], default="synthetic")
    wh.add_argument("--symbols", default="BTC-USD"); wh.add_argument("--host", default="127.0.0.1")
    wh.add_argument("--port", type=int, default=8787); wh.add_argument("--secret", default=None)
    wh.add_argument("--poll", type=float, default=5.0); wh.set_defaults(func=cmd_webhook)

    d = sub.add_parser("dashboard")
    d.add_argument("--symbols", default="BTC-USD,ETH-USD,SOL-USD"); d.add_argument("--bars", type=int, default=1500)
    d.add_argument("--cash", type=float, default=25_000.0); d.add_argument("--out", default="dashboard.html")
    d.add_argument("--title", default="tradebot — portfolio backtest"); d.set_defaults(func=cmd_dashboard)

    cmp = sub.add_parser("compare", help="heuristic vs Claude brain on the same data")
    common(cmp)
    cmp.add_argument("--source", choices=["synthetic", "coinbase"], default="coinbase")
    cmp.add_argument("--symbol", default="BTC-USD")
    cmp.add_argument("--bars", type=int, default=300)
    cmp.set_defaults(func=cmd_compare, model="haiku", max_llm_calls=50)

    an = sub.add_parser("analyze")
    an.add_argument("--source", choices=["synthetic", "coinbase"], default="coinbase")
    an.add_argument("--symbol", default="BTC-USD"); an.add_argument("--bars", type=int, default=200)
    an.add_argument("--granularity", type=int, default=3600); an.add_argument("--seed", type=int, default=42)
    an.set_defaults(func=cmd_analyze)

    mc = sub.add_parser("montecarlo", help="risk-of-ruin sim for a compounding goal")
    mc.add_argument("--start", type=float, default=1.0)
    mc.add_argument("--target", type=float, default=1_000_000.0)
    mc.add_argument("--win-prob", type=float, default=0.55, dest="win_prob")
    mc.add_argument("--reward-risk", type=float, default=1.0, dest="reward_risk")
    mc.add_argument("--risk", type=float, default=0.5, help="fraction of equity per trade")
    mc.add_argument("--fee", type=float, default=0.001)
    mc.add_argument("--min-stake", type=float, default=1.0, dest="min_stake")
    mc.add_argument("--max-trades", type=int, default=5000, dest="max_trades")
    mc.add_argument("--traders", type=int, default=10000)
    mc.add_argument("--seed", type=int, default=0)
    mc.set_defaults(func=cmd_montecarlo)

    be = sub.add_parser("breakeven",
                        help="win rate required to overcome fees at each trade size")
    be.add_argument("--win-rate", type=float, default=0.55, dest="win_rate")
    be.add_argument("--reward-risk", type=float, default=1.0, dest="reward_risk")
    be.add_argument("--fee", type=float, default=0.001)
    be.add_argument("--slippage", type=float, default=0.0005)
    be.set_defaults(func=cmd_breakeven)

    oc = sub.add_parser("onchain"); oc.set_defaults(func=cmd_onchain)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
