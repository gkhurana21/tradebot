"""CLI entrypoint.

Commands:
  fetch-data    download & cache OHLCV history (public endpoints, no keys)
  backtest      run strategies over cached history, print metrics table
  walkforward   rolling train/test validation, out-of-sample metrics
  paper         run the paper-trading daemon (Ctrl-C / SIGTERM to stop)
  status        per-stream paper equity + gate progress
  report        write a markdown report to reports/

Live trading is NOT BUILT. config.yaml `dry_run` must be true or every
command refuses to start.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from bot.config import load_config
from bot.data import cache
from bot.data.base import TIMEFRAME_MS


def _universes(cfg: dict, arg: str) -> list[str]:
    return list(cfg["universes"]) if arg == "all" else [arg]


def _strategies(cfg: dict, arg: str) -> list[str]:
    from bot.strategies import enabled_strategies
    return enabled_strategies(cfg) if arg == "all" else [arg]


def _load_frames(cfg: dict, universe: str):
    ucfg = cfg["universes"][universe]
    cache_dir = cfg["bot"]["ohlcv_cache_dir"]
    closes = cache.load_history_df(cache_dir, universe, ucfg["symbols"],
                                   ucfg["timeframe"], "close")
    opens = cache.load_history_df(cache_dir, universe, ucfg["symbols"],
                                  ucfg["timeframe"], "open")
    if closes.empty:
        sys.exit(f"no cached data for universe '{universe}' — "
                 f"run: python main.py fetch-data --universe {universe}")
    return closes, opens


# ── commands ─────────────────────────────────────────────────────────────
def cmd_fetch_data(cfg: dict, args) -> None:
    from bot.data.crypto import CryptoFeed
    from bot.data.equities import EquityFeed
    cache_dir = cfg["bot"]["ohlcv_cache_dir"]
    now_ms = int(time.time() * 1000)
    since = now_ms - int(args.years * 365.25 * 86_400_000)
    for universe in _universes(cfg, args.universe):
        ucfg = cfg["universes"][universe]
        feed = (CryptoFeed(ucfg["exchange"]) if universe == "crypto"
                else EquityFeed())
        tf = ucfg["timeframe"]
        for sym in ucfg["symbols"]:
            bars = feed.fetch_ohlcv(sym, tf, since_ms=since)
            if bars:
                cache.save_bars(cache_dir, universe, bars)
                span_days = (bars[-1].ts - bars[0].ts) / 86_400_000
                print(f"  {universe}/{sym}: {len(bars)} bars "
                      f"({span_days:.0f} days)")
            else:
                print(f"  {universe}/{sym}: NO DATA")


def cmd_backtest(cfg: dict, args) -> None:
    from bot.backtest.engine import run_backtest
    from bot.backtest.metrics import format_metrics_table
    from bot.strategies import build_strategy
    for universe in _universes(cfg, args.universe):
        closes, opens = _load_frames(cfg, universe)
        rows = {}
        for name in _strategies(cfg, args.strategy):
            strat = build_strategy(name, cfg, universe)
            rows[name] = run_backtest(strat, closes, opens, cfg)["metrics"]
        print(f"\n=== backtest: {universe} "
              f"({len(closes)} bars, {len(closes.columns)} symbols) ===")
        print(format_metrics_table(rows))


def cmd_walkforward(cfg: dict, args) -> None:
    from bot.backtest.metrics import format_metrics_table
    from bot.backtest.walkforward import run_walkforward
    for universe in _universes(cfg, args.universe):
        closes, opens = _load_frames(cfg, universe)
        rows, windows_by_strat = {}, {}
        for name in _strategies(cfg, args.strategy):
            res = run_walkforward(name, cfg, universe, closes, opens)
            if "error" in res:
                print(f"{universe}/{name}: {res['error']}")
                continue
            rows[name] = res["oos_metrics"]
            windows_by_strat[name] = res["windows"]
        if not rows:
            continue
        print(f"\n=== walk-forward OOS: {universe} ===")
        print(format_metrics_table(rows))
        if args.verbose:
            for name, windows in windows_by_strat.items():
                print(f"\n{name} windows:")
                for w in windows:
                    print(f"  idx {w['start_idx']:>5}  params={w['params']}  "
                          f"train_sharpe={w['train_sharpe']}  "
                          f"test_sharpe={w['test_sharpe']}")


def cmd_paper(cfg: dict, _args) -> None:
    from bot.paper.trader import PaperTrader
    PaperTrader(cfg).run()


def _status_lines(cfg: dict) -> list[str]:
    from bot.gates.decision import evaluate_stream
    from bot.store import Store
    store = Store(cfg["bot"]["db_path"], cfg["bot"]["audit_log_path"])
    lines = []
    streams = store.streams()
    if not streams:
        return ["no paper streams yet — run: python main.py paper"]
    for stream in streams:
        curve = store.equity_curve(stream)
        state = store.load_stream_state(stream) or {}
        eq = curve[-1].equity_usd if curve else cfg["risk"]["start_equity_usd"]
        universe = stream.rsplit("_", 1)[-1]
        null_stream = f"null_random_{universe}"
        lines.append(f"\n── {stream} ──")
        lines.append(f"  equity: ${eq:,.2f}"
                     f"{'  [HALTED]' if state.get('halted') else ''}"
                     f"   snapshots: {len(curve)}")
        if stream != null_stream:
            rep = evaluate_stream(store, stream, null_stream, cfg["gates"])
            lines.append(f"  verdict: {rep.verdict}")
            for cname, c in rep.checks.items():
                mark = "✓" if c.get("pass") else "✗"
                detail = {k: v for k, v in c.items() if k != "pass"}
                lines.append(f"    {mark} {cname}: {detail}")
    store.close()
    return lines


def cmd_status(cfg: dict, _args) -> None:
    print("\n".join(_status_lines(cfg)))


def cmd_report(cfg: dict, _args) -> None:
    out_dir = Path(cfg["bot"]["reports_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H%M")
    path = out_dir / f"paper_report_{stamp}.md"
    body = "# Paper trading report — " + stamp + "\n\n```\n"
    body += "\n".join(_status_lines(cfg))
    body += "\n```\n"
    path.write_text(body)
    print(f"wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="trading-bot")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch-data")
    p.add_argument("--universe", default="all")
    p.add_argument("--years", type=float, default=4.0)

    p = sub.add_parser("backtest")
    p.add_argument("--universe", default="all")
    p.add_argument("--strategy", default="all")

    p = sub.add_parser("walkforward")
    p.add_argument("--universe", default="all")
    p.add_argument("--strategy", default="all")
    p.add_argument("--verbose", action="store_true")

    sub.add_parser("paper")
    sub.add_parser("status")
    sub.add_parser("report")

    args = parser.parse_args()
    cfg = load_config(args.config)   # dry_run hard guard lives here
    logging.basicConfig(
        level=cfg["bot"]["log_level"],
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    {"fetch-data": cmd_fetch_data,
     "backtest": cmd_backtest,
     "walkforward": cmd_walkforward,
     "paper": cmd_paper,
     "status": cmd_status,
     "report": cmd_report}[args.command](cfg, args)


if __name__ == "__main__":
    main()
