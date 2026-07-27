"""Run the full strategy ladder on one identical market and compare.

Every strategy gets a Market built from the same seed, and flow generation
never depends on the book, so each strategy faces the exact same fair value
path and the exact same counterparty orders.

  python run.py                normal regime
  python run.py --hard         high vol, informed heavy stress regime
"""

import argparse

from engine.backtest import Backtest
from sim.market import Market
from strategies.market_makers import LADDER
from diagnostics.report import summarize, print_table

REGIMES = {
    "normal": dict(vol=0.04, flow_rate=6.0, informed_frac=0.2,
                   mean_reversion=0.01, patience=0.08),
    "hard": dict(vol=0.08, flow_rate=8.0, informed_frac=0.45,
                 mean_reversion=0.01, patience=0.12),
}

# competitive landscape around us: how many other quoters, how tight, how big
BG_REGIMES = {
    "tight": dict(bg_quoters=3, bg_prob=0.8, bg_half_range=(0.02, 0.05),
                  bg_min=1, bg_max=4),
    "medium": dict(bg_quoters=2, bg_prob=0.7, bg_half_range=(0.05, 0.10),
                   bg_min=2, bg_max=8),
    "wide": dict(bg_quoters=1, bg_prob=0.7, bg_half_range=(0.10, 0.18),
                 bg_min=4, bg_max=12),
}


def main():
    p = argparse.ArgumentParser(description="Market making practice harness")
    p.add_argument("--hard", action="store_true",
                   help="high vol, informed heavy stress regime")
    p.add_argument("--steps", type=int, default=2000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--rebate", type=float, default=0.0,
                   help="maker rebate per unit filled")
    p.add_argument("--bg", choices=sorted(BG_REGIMES), default="medium",
                   help="background competition regime")
    p.add_argument("--latency", type=int, default=0,
                   help="steps between deciding a quote and it resting")
    args = p.parse_args()

    regime = "hard" if args.hard else "normal"
    params = {**REGIMES[regime], **BG_REGIMES[args.bg]}
    print(f"regime={regime}  bg={args.bg}  latency={args.latency}  "
          f"steps={args.steps}  seed={args.seed}  rebate={args.rebate}  {params}")
    print()

    rows = []
    for strategy_cls in LADDER:
        market = Market(seed=args.seed, **params)
        bt = Backtest(market, strategy_cls(), steps=args.steps,
                      maker_rebate=args.rebate, latency=args.latency)
        rows.append(summarize(bt.run()))
    print_table(rows)


if __name__ == "__main__":
    main()
