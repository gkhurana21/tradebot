"""The tests that make the backtest trustworthy.

1. Truncation invariance: a strategy's weight at bar t must not change when
   future bars are appended/removed.
2. Engine invariance: perturbing future prices must not change past returns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import run_backtest
from bot.strategies import REGISTRY

STRATS = list(REGISTRY)


def _build(name, cfg):
    return REGISTRY[name](cfg["strategies"][name], "crypto",
                          cfg["universes"]["crypto"])


@pytest.mark.parametrize("name", STRATS)
def test_truncation_invariance(name, closes, cfg):
    strat = _build(name, cfg)
    k = 300
    full = strat.weights(closes).iloc[:k]
    trunc = strat.weights(closes.iloc[:k])
    pd.testing.assert_frame_equal(full, trunc, check_exact=False, atol=1e-12)


@pytest.mark.parametrize("name", STRATS)
def test_engine_future_perturbation(name, closes, opens, cfg):
    strat = _build(name, cfg)
    base = run_backtest(strat, closes, opens, cfg)["net_returns"]

    tampered_c, tampered_o = closes.copy(), opens.copy()
    tampered_c.iloc[350:] *= 1.5   # blow up the future
    tampered_o.iloc[350:] *= 1.5
    perturbed = run_backtest(_build(name, cfg), tampered_c, tampered_o,
                             cfg)["net_returns"]

    # weights shift by 1 bar and fwd returns reach 1 bar ahead, so anything
    # before bar 348 must be identical
    pd.testing.assert_series_equal(base.iloc[:348], perturbed.iloc[:348],
                                   check_exact=False, atol=1e-12)


def test_null_random_is_deterministic(closes, cfg):
    a = _build("null_random", cfg).weights(closes)
    b = _build("null_random", cfg).weights(closes)
    pd.testing.assert_frame_equal(a, b)


def test_weights_respect_long_only(closes, cfg):
    cfg["universes"]["crypto"]["allow_short"] = False
    for name in STRATS:
        w = _build(name, cfg).weights(closes)
        assert (w.min().min() >= 0), f"{name} produced shorts in long-only"
