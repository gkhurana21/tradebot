"""Walk-forward validation.

Rolling windows: on each train window, pick the best param combo from the
config grid (by Sharpe); apply it to the following test window; stitch all
test-window returns into one out-of-sample series. If a strategy has no grid,
the config params are used as-is and the train window is pure warm-up.
"""

from __future__ import annotations

import itertools
from copy import deepcopy

import pandas as pd

from bot.backtest.engine import run_backtest
from bot.backtest.metrics import compute_metrics
from bot.strategies import REGISTRY


def _param_combos(grid: dict) -> list[dict]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, vals))
            for vals in itertools.product(*(grid[k] for k in keys))]


def _build(strategy_name: str, cfg: dict, universe: str, overrides: dict):
    params = deepcopy(cfg["strategies"][strategy_name])
    for k, v in overrides.items():
        # grid values are scalars even for per-universe params
        params[k] = v
    return REGISTRY[strategy_name](params, universe, cfg["universes"][universe])


def run_walkforward(strategy_name: str, cfg: dict, universe: str,
                    closes: pd.DataFrame, opens: pd.DataFrame) -> dict:
    wf = cfg["walkforward"]
    train, test, step = wf["train_bars"], wf["test_bars"], wf["step_bars"]
    grid = wf.get("grids", {}).get(strategy_name, {})
    combos = _param_combos(grid)

    oos_returns: list[pd.Series] = []
    windows: list[dict] = []
    start = 0
    while start + train + test <= len(closes):
        tr = slice(start, start + train)
        full = slice(start, start + train + test)

        best_combo, best_sharpe = combos[0], float("-inf")
        for combo in combos:
            strat = _build(strategy_name, cfg, universe, combo)
            res = run_backtest(strat, closes.iloc[tr], opens.iloc[tr], cfg)
            s = res["metrics"].get("sharpe", float("-inf"))
            if s > best_sharpe:
                best_sharpe, best_combo = s, combo

        # run chosen params over train+test so the test window has warm-up,
        # then keep only the test-window returns
        strat = _build(strategy_name, cfg, universe, best_combo)
        res = run_backtest(strat, closes.iloc[full], opens.iloc[full], cfg)
        test_rets = res["net_returns"].iloc[train:]
        oos_returns.append(test_rets)
        windows.append({"start_idx": start, "params": best_combo,
                        "train_sharpe": round(best_sharpe, 3),
                        "test_sharpe": compute_metrics(
                            test_rets, strat.bars_per_year).get("sharpe")})
        start += step

    if not oos_returns:
        return {"error": f"not enough bars ({len(closes)}) for "
                         f"train={train} + test={test}"}
    stitched = pd.concat(oos_returns)
    strat = _build(strategy_name, cfg, universe, {})
    return {"oos_metrics": compute_metrics(stitched, strat.bars_per_year),
            "oos_returns": stitched,
            "windows": windows}
