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


_MODELS = {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
           "haiku": "claude-haiku-5-5"}


def _brain(a):
    """Builds the research brain named by --brain.

    Both kinds satisfy the same decide() contract, so everything downstream --
    backtester, live loop, risk engine -- is identical either way.
    """
    model = _MODELS[getattr(a, "model", None) or "opus"]
    max_calls = getattr(a, "max_llm_calls", None)

    if getattr(a, "brain", "single") == "graph" and not a.no_llm:
        from .brain.graph import GraphBrain, graph_available
        if not graph_available():
            print("[brain] graph unavailable (needs ANTHROPIC_API_KEY and "
                  '`pip install -e ".[graph]"`); using the single-call brain')
        else:
            return GraphBrain(model=model, max_calls=max_calls)

    return Brain(use_llm=not a.no_llm, model=model, max_calls=max_calls)


def _cost_note(a, brain) -> None:
    """Prints actual spend. Works for either brain kind -- GraphBrain has no
    use_llm flag, since a graph is only ever constructed when LLM mode is on."""
    if getattr(brain, "calls_made", 0) and hasattr(brain, "cost_report"):
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
        f.write(render_page(res, a.title, banner=a.banner))
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
    model = _MODELS[a.model]

    def progress(made, cap):
        # A run with no output is indistinguishable from a hung one, which is
        # exactly the confusion this avoids.
        total = f"/{cap}" if cap else ""
        print(f"\r  querying Claude... {made}{total} calls", end="", flush=True)

    llm = Brain(use_llm=llm_available, model=model,
                max_calls=a.max_llm_calls, progress=progress)
    if llm_available:
        print(f"Running the Claude brain ({model}, capped at "
              f"{a.max_llm_calls} calls)...")
    r_llm = run(llm) if llm_available else None
    if llm_available:
        print()  # close the progress line

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


SNAPSHOT_SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def cmd_snapshot(a):
    """Download a fixed Coinbase window to CSV so later runs see the same bars."""
    from datetime import datetime, timezone
    import os
    def utc(text):
        dt = datetime.fromisoformat(text)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    start, end = utc(a.start), utc(a.end)
    os.makedirs(a.out, exist_ok=True)
    for sym in _split(a.symbols):
        bars = feed.coinbase_history(sym, start, end, granularity=3600)
        path = os.path.join(a.out, f"{sym}-1h.csv")
        feed.write_csv(bars, path)
        print(f"{sym}: {len(bars)} hourly bars -> {path}")


def _utc(text):
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(text)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def cmd_nulltest(a):
    """Does the strategy (or the Claude brain) beat trading the same moments at random?"""
    import os
    from .sim.nulltest import run_nulltest
    symbols = _split(a.symbols)
    if a.source == "synthetic":
        streams = _load_streams("synthetic", symbols, a.bars)
    else:
        missing = [s for s in symbols if not os.path.exists(os.path.join(a.data, f"{s}-1h.csv"))]
        if missing:
            raise SystemExit(f"No snapshot for {', '.join(missing)} in {a.data!r}. Run from the repo "
                             f"root, pass --data, or create one with `tradebot snapshot`.")
        streams = {s: feed.csv_candles(os.path.join(a.data, f"{s}-1h.csv"), s) for s in symbols}
    if a.start or a.end:
        from .sim.claudetest import slice_streams
        streams = slice_streams(streams, _utc(a.start) if a.start else None,
                                _utc(a.end) if a.end else None)
    if a.brain == "claude":
        return _nulltest_claude(a, streams)
    print(f"Running the strategy once and its coin-flip twin {a.seeds} times...")
    res = run_nulltest(streams, _ensemble, seeds=a.seeds, cash=a.cash)
    print(res.summary())


def _nulltest_claude(a, streams):
    """The Claude coin-flip test. Everything here is free except --submit-batch
    and --collect, which are the only paths that touch the network."""
    import os
    from decimal import Decimal
    from .sim import claudetest as ct
    model = _MODELS[a.model]
    window = (f"{a.start or 'snapshot start'} to {a.end or 'snapshot end'} (end exclusive), "
              f"{', '.join(streams)}")
    prompts_path, cache_path = a.prompts, a.cache
    cap = Decimal(a.cap)
    try:
        ct.require_testable(model)
        if a.build_prompts:
            prompts = ct.build_prompts(streams, _ensemble, model, cash=a.cash)
            sha = ct.write_prompts(prompts, prompts_path)
            print(f"Built {len(prompts)} account-free prompts ({model}, prompt {ct.PROMPT_VERSION}, "
                  f"hash {ct.PROMPT_HASH}) -> {prompts_path}\n  sha256 {sha}")
            print("Cost bound: " + ct.cost_bound(prompts, model).summary())
            if a.fill_prereg:
                n = ct.fill_preregistration(a.fill_prereg, model, prompts, sha, window, cap)
                print(f"Filled {n} placeholders in {a.fill_prereg}")
            return
        if a.fill_prereg:
            raise SystemExit("--fill-prereg goes with --build-prompts")
        if a.submit_batch or a.collect or a.dry_run:
            if not os.path.exists(prompts_path):
                raise SystemExit(f"No prompts at {prompts_path}; run with --build-prompts first.")
            prompts = ct.read_prompts(prompts_path)
        if a.submit_batch or a.dry_run:
            todo = ct.pending_prompts(prompts, cache_path) if os.path.exists(cache_path) else prompts
            if a.pilot is not None:
                todo = todo[:a.pilot]
            bound = ct.cost_bound(todo, model)
            print("Cost bound: " + bound.summary() + f" · cap ${cap}")
            if a.dry_run:
                ct.enforce_cap(bound, cap)
                print("Dry run: within the cap, nothing sent.")
                return
            res = ct.submit_batch(prompts, cache_path, ct.AnthropicBatchClient(), model,
                                  cap=cap, pilot=a.pilot)
            print(f"Submitted batch {res.batch_id}: {res.n_requests} requests"
                  f"{' (pilot)' if res.pilot else ''}. Run --collect when it has ended.")
            return
        if a.collect:
            res = ct.collect(prompts, cache_path, ct.AnthropicBatchClient(), model,
                             wait=not a.no_wait)
            print(f"{res.status}: {res.n_results} results · spend so far ${res.spend_usd:.6f} "
                  f"(tool counter)")
            if res.status == "collected":
                print(f"Cache sha256 {res.cache_sha256}\n" + ct.rates_report(res.rates))
            return
        if not os.path.exists(cache_path):
            raise SystemExit(f"No decision cache at {cache_path}; build prompts, submit and collect first.")
        print(f"Replaying {cache_path} against {a.seeds} + {a.seeds} twins (no API calls)...")
        print(ct.run_claude_test(streams, _ensemble, cache_path, model,
                                 seeds=a.seeds, cash=a.cash).summary())
    except (ct.CapExceeded, ct.InvalidRun, ct.CacheMiss, ct.CacheMismatch, ValueError) as e:
        raise SystemExit(str(e))


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
        sp.add_argument("--brain", choices=["single", "graph"], default="single",
                        help="single = one Claude call; graph = thesis/critique/judge "
                             "pipeline (2-3x the calls)")

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
    d.add_argument("--title", default="tradebot — portfolio backtest")
    d.add_argument("--banner", default=None, help="note shown above the report")
    d.set_defaults(func=cmd_dashboard)

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

    sn = sub.add_parser("snapshot", help="save a fixed Coinbase window to CSV")
    sn.add_argument("--start", required=True, help="UTC date, e.g. 2026-08-15")
    sn.add_argument("--end", required=True, help="UTC date, exclusive")
    sn.add_argument("--symbols", default=",".join(SNAPSHOT_SYMBOLS))
    sn.add_argument("--out", default="docs/data")
    sn.set_defaults(func=cmd_snapshot)

    nt = sub.add_parser("nulltest", help="does the strategy beat a coin flip?")
    nt.add_argument("--source", choices=["synthetic", "snapshot"], default="snapshot")
    nt.add_argument("--data", default="docs/data", help="folder written by `snapshot`")
    nt.add_argument("--symbols", default=",".join(SNAPSHOT_SYMBOLS))
    nt.add_argument("--bars", type=int, default=1500, help="synthetic only")
    nt.add_argument("--seeds", type=int, default=199)
    nt.add_argument("--cash", type=float, default=25_000.0)
    nt.add_argument("--brain", choices=["heuristic", "claude"], default="heuristic")
    nt.add_argument("--model", choices=sorted(_MODELS), default="haiku", help="claude brain only")
    nt.add_argument("--start", default=None, help="UTC date, inclusive")
    nt.add_argument("--end", default=None, help="UTC date, exclusive")
    nt.add_argument("--prompts", default="docs/claude/prompts.jsonl")
    nt.add_argument("--cache", default="docs/claude/decisions.jsonl",
                    help="decision cache (JSONL); the ledger and sha256 sit beside it")
    nt.add_argument("--build-prompts", action="store_true", dest="build_prompts",
                    help="free: build every prompt offline, write --prompts, print the cost bound")
    nt.add_argument("--pilot", type=int, nargs="?", const=50, default=None,
                    help="submit only the first N prompts (default 50)")
    nt.add_argument("--submit-batch", action="store_true", dest="submit_batch",
                    help="PAID: submit one Message Batch (refuses above --cap)")
    nt.add_argument("--collect", action="store_true", help="PAID path: fetch batch results into --cache")
    nt.add_argument("--no-wait", action="store_true", dest="no_wait",
                    help="with --collect: check once instead of polling until the batch ends")
    nt.add_argument("--cap", default="1.00", help="refuse to submit above this many USD")
    nt.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print the cost bound and check the cap; no network")
    nt.add_argument("--fill-prereg", default=None, dest="fill_prereg",
                    help="with --build-prompts: fill placeholders in the pre-registration doc")
    nt.set_defaults(func=cmd_nulltest)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
