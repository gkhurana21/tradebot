#!/usr/bin/env python3
"""Run a multi-symbol portfolio backtest with the ensemble strategy.

  python3 scripts/run_portfolio.py                       # synthetic, offline
  python3 scripts/run_portfolio.py --onchain             # add live on-chain context
  python3 scripts/run_portfolio.py --source coinbase --symbols BTC-USD,ETH-USD,SOL-USD
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.backtest.portfolio import PortfolioBacktester
from tradebot.brain.brain import Brain, _llm_available
from tradebot.brokers.paper import PaperBroker
from tradebot.core.risk import RiskConfig, RiskEngine
from tradebot.core.strategy import BollingerReversion, EmaRsiStrategy, EnsembleStrategy
from tradebot.data import feed, onchain


def build_ensemble() -> EnsembleStrategy:
    return EnsembleStrategy(EmaRsiStrategy(), BollingerReversion(), require=1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=["synthetic", "coinbase"], default="synthetic")
    p.add_argument("--symbols", default="BTC-USD,ETH-USD,SOL-USD")
    p.add_argument("--bars", type=int, default=1500)
    p.add_argument("--cash", type=float, default=25_000.0)
    p.add_argument("--onchain", action="store_true", help="attach live on-chain context")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    streams = {}
    for i, sym in enumerate(symbols):
        if args.source == "synthetic":
            streams[sym] = list(feed.synthetic_candles(
                sym, n=args.bars, seed=42 + i,
                start_price=[30_000, 2_000, 100][i % 3]))
        else:
            streams[sym] = feed.coinbase_candles(sym, granularity=3600,
                                                 limit=min(args.bars, 300))
        print(f"Loaded {len(streams[sym])} {sym} candles from {args.source}")

    ctx_provider = None
    if args.onchain:
        snap = onchain.fetch_snapshot()
        print(f"On-chain context: {snap.features()} (ok={snap.ok})")
        feats = snap.features()
        ctx_provider = (lambda sym: {"onchain": feats}) if snap.ok else None

    brain = Brain(use_llm=not args.no_llm)
    risk = RiskEngine(RiskConfig(max_open_positions=len(symbols)))
    broker = PaperBroker(starting_cash=args.cash)
    bt = PortfolioBacktester(build_ensemble, brain, risk, broker,
                             context_provider=ctx_provider, verbose=args.verbose)
    res = bt.run(streams)

    mode = "LLM (Claude)" if (brain.use_llm and _llm_available()) else "heuristic (offline)"
    print(f"\nBrain mode: {mode}")
    print(res.summary())
    print("\nFull metrics:")
    print(res.metrics.table())


if __name__ == "__main__":
    main()
