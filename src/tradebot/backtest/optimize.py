"""Parameter optimization with walk-forward validation.

Grid-searches strategy parameters, but scores them the honest way: fit on an
in-sample window, then measure on the *next* out-of-sample window the params
never saw. A param set that only looks good in-sample is overfit and gets
penalized. Reported score is the mean out-of-sample objective across folds.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

from ..brain.brain import Brain
from ..brokers.paper import PaperBroker
from ..core.risk import RiskConfig, RiskEngine
from ..core.strategy import Strategy
from ..core.types import Candle
from .engine import Backtester


@dataclass
class OptResult:
    params: dict
    oos_score: float
    is_score: float
    folds: int
    detail: list = field(default_factory=list)


def _objective(result) -> float:
    """Risk-adjusted objective: Sharpe, penalized by drawdown. Higher is better."""
    m = result.metrics
    return m.sharpe - 0.1 * m.max_drawdown_pct


def _run(strategy: Strategy, candles: list[Candle], cash: float) -> float:
    bt = Backtester(strategy, Brain(use_llm=False), RiskEngine(RiskConfig()),
                    PaperBroker(cash))
    return _objective(bt.run(candles))


def walk_forward(
    strategy_factory: Callable[..., Strategy],
    param_grid: dict[str, list],
    candles: list[Candle],
    folds: int = 4,
    train_frac: float = 0.6,
    cash: float = 10_000.0,
) -> list[OptResult]:
    """Return every param combo scored by mean out-of-sample objective, best first."""
    keys = list(param_grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*param_grid.values())]

    n = len(candles)
    fold_size = n // folds
    results: list[OptResult] = []

    for params in combos:
        oos_scores, is_scores, detail = [], [], []
        for f in range(folds):
            start = f * fold_size
            end = min(start + fold_size, n)
            window = candles[start:end]
            if len(window) < 50:
                continue
            split = int(len(window) * train_frac)
            train, test = window[:split], window[split:]
            if len(train) < 30 or len(test) < 20:
                continue
            is_scores.append(_run(strategy_factory(**params), train, cash))
            oos = _run(strategy_factory(**params), test, cash)
            oos_scores.append(oos)
            detail.append({"fold": f, "oos": round(oos, 3)})
        if oos_scores:
            results.append(OptResult(
                params=params,
                oos_score=round(sum(oos_scores) / len(oos_scores), 4),
                is_score=round(sum(is_scores) / len(is_scores), 4) if is_scores else 0.0,
                folds=len(oos_scores),
                detail=detail,
            ))
    results.sort(key=lambda r: r.oos_score, reverse=True)
    return results
