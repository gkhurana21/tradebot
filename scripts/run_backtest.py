#!/usr/bin/env python3
"""Run a backtest of the full loop. Works fully offline on synthetic data.

Examples:
  python3 scripts/run_backtest.py                 # synthetic, offline
  python3 scripts/run_backtest.py --source coinbase --symbol ETH-USD
  python3 scripts/run_backtest.py --verbose
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.backtest.engine import Backtester
from tradebot.brain.brain import Brain, _llm_available
from tradebot.brokers.paper import PaperBroker
from tradebot.core.risk import RiskConfig, RiskEngine
from tradebot.core.strategy import EmaRsiStrategy
from tradebot.data import feed


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest the trading loop")
    p.add_argument("--source", choices=["synthetic", "coinbase", "csv"],
                   default="synthetic")
    p.add_argument("--symbol", default="BTC-USD")
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--csv-path", default="")
    p.add_argument("--no-llm", action="store_true",
                   help="force heuristic brain even if API key is set")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.source == "synthetic":
        candles = list(feed.synthetic_candles(args.symbol, n=args.bars))
    elif args.source == "coinbase":
        candles = feed.coinbase_candles(args.symbol, granularity=3600,
                                        limit=min(args.bars, 300))
    else:
        candles = feed.csv_candles(args.csv_path, args.symbol)

    print(f"Loaded {len(candles)} {args.symbol} candles from {args.source}")

    strategy = EmaRsiStrategy()
    brain = Brain(use_llm=not args.no_llm)
    risk = RiskEngine(RiskConfig())
    broker = PaperBroker(starting_cash=args.cash)

    bt = Backtester(strategy, brain, risk, broker, verbose=args.verbose)
    result = bt.run(candles)

    mode = "LLM (Claude)" if (brain.use_llm and _llm_available()) else "heuristic (offline)"
    print(f"\nBrain mode: {mode}")
    print(result.summary())


if __name__ == "__main__":
    main()
