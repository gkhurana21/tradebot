#!/usr/bin/env python3
"""Run a portfolio backtest and write an HTML dashboard.

  python3 scripts/gen_dashboard.py --out dashboard.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.backtest.dashboard import render_body, render_page
from tradebot.backtest.portfolio import PortfolioBacktester
from tradebot.brain.brain import Brain
from tradebot.brokers.paper import PaperBroker
from tradebot.core.risk import RiskConfig, RiskEngine
from tradebot.core.strategy import BollingerReversion, EmaRsiStrategy, EnsembleStrategy
from tradebot.data import feed


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="dashboard.html")
    p.add_argument("--body-only", action="store_true", help="emit content w/o <html> wrapper")
    p.add_argument("--symbols", default="BTC-USD,ETH-USD,SOL-USD")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--title", default="tradebot — portfolio backtest")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    streams = {
        sym: list(feed.synthetic_candles(sym, n=args.bars, seed=42 + i,
                                         start_price=[30_000, 2_000, 100][i % 3]))
        for i, sym in enumerate(symbols)
    }
    bt = PortfolioBacktester(
        lambda: EnsembleStrategy(EmaRsiStrategy(), BollingerReversion(), require=1),
        Brain(use_llm=False), RiskEngine(RiskConfig(max_open_positions=len(symbols))),
        PaperBroker(25_000),
    )
    res = bt.run(streams)
    html = render_body(res, args.title) if args.body_only else render_page(res, args.title)
    Path(args.out).write_text(html)
    print(f"Wrote {args.out} ({len(html):,} bytes)")
    print(res.summary())


if __name__ == "__main__":
    main()
