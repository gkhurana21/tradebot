"""Robustness sweep: the whole ladder across every market regime and every
background competition regime, averaged over seeds.

  python sweep.py                 # 8 seeds, all market x bg combinations
  python sweep.py --latency 2     # same sweep with quotes resting 2 steps late
  python sweep.py --curve         # PnL and AdvSel vs latency 0/1/2/5
  python sweep.py --seeds 16
  python sweep.py --steps 1000

Use this after any retune. Single seed numbers, especially MaxDD, are
noisy; a conclusion that does not survive this sweep is an artifact of one
seed or one competitive landscape.
"""

import argparse

import numpy as np

from engine.backtest import Backtest
from sim.market import Market
from strategies.market_makers import LADDER
from diagnostics.report import summarize
from run import REGIMES, BG_REGIMES


def sweep_cell(mparams, bparams, steps, seeds, latency=0, ladder=LADDER):
    """Average summarize() metrics per strategy for one regime combination."""
    rows = []
    for cls in ladder:
        per_seed = []
        for seed in range(seeds):
            market = Market(seed=seed, **mparams, **bparams)
            bt = Backtest(market, cls(), steps=steps, latency=latency)
            per_seed.append(summarize(bt.run()))
        agg = {k: float(np.mean([r[k] for r in per_seed]))
               for k in per_seed[0] if k != "name"}
        agg["name"] = cls.name
        rows.append(agg)
    return rows


# cells for the latency curve: the benign case plus the two toxic ones
CURVE_CELLS = [("normal", "medium"), ("hard", "medium"), ("hard", "tight")]
CURVE_LATENCIES = [0, 1, 2, 5]


def latency_curve(steps, seeds):
    """PnL against latency per strategy, with the attribution that explains
    it: spread capture, adverse selection and stale cost. Also reports edge
    lost per step of delay, measured from the latency 0 baseline."""
    for mname, bname in CURVE_CELLS:
        print(f"== latency curve  market={mname}  bg={bname}  "
              f"({seeds} seeds x {steps} steps) ==")
        per_lat = {lat: sweep_cell(REGIMES[mname], BG_REGIMES[bname],
                                   steps, seeds, latency=lat)
                   for lat in CURVE_LATENCIES}
        print(f"{'Strategy':<13} {'N':>2}  {'PnL':>8} {'SpreadCap':>10} "
              f"{'AdvSel':>9} {'Stale':>9} {'FillRt':>7}  {'PnL/step':>9}")
        for i, cls in enumerate(LADDER):
            base = per_lat[0][i]["pnl"]
            for lat in CURVE_LATENCIES:
                r = per_lat[lat][i]
                per_step = (r["pnl"] - base) / lat if lat else 0.0
                label = cls.name if lat == CURVE_LATENCIES[0] else ""
                print(f"{label:<13} {lat:>2}  {r['pnl']:>8.1f} "
                      f"{r['spread_capture']:>10.1f} "
                      f"{r['adverse_selection']:>9.1f} {r['stale_cost']:>9.1f} "
                      f"{r['fill_rate']:>7.2f}  {per_step:>9.1f}")
        print()


def main():
    p = argparse.ArgumentParser(description="Ladder robustness sweep")
    p.add_argument("--steps", type=int, default=2000)
    p.add_argument("--seeds", type=int, default=8)
    p.add_argument("--latency", type=int, default=0,
                   help="steps between deciding a quote and it resting")
    p.add_argument("--curve", action="store_true",
                   help="PnL and AdvSel vs latency instead of the full sweep")
    args = p.parse_args()

    if args.curve:
        latency_curve(args.steps, args.seeds)
        return

    for mname, mparams in REGIMES.items():
        for bname, bparams in BG_REGIMES.items():
            print(f"== market={mname}  bg={bname}  latency={args.latency}  "
                  f"({args.seeds} seeds x {args.steps} steps) ==")
            rows = sweep_cell(mparams, bparams, args.steps, args.seeds,
                              latency=args.latency)
            for r in rows:
                print(f"{r['name']:<13} pnl={r['pnl']:7.1f} "
                      f"sharpe={r['sharpe']:6.1f} fillrt={r['fill_rate']:5.2f} "
                      f"advsel={r['adverse_selection']:8.1f} "
                      f"stale={r['stale_cost']:8.1f} "
                      f"maxdd={r['max_dd']:7.2f} avginv={r['avg_abs_inv']:5.1f}")
            ranked = sorted(rows, key=lambda r: -r["sharpe"])
            print("rank by sharpe: " + " > ".join(r["name"] for r in ranked))
            print()


if __name__ == "__main__":
    main()
