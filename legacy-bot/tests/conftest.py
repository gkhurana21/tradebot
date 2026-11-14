from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

TF_MS = 14_400_000  # 4h


def make_closes(n_bars: int = 400, n_syms: int = 4, seed: int = 7,
                start_ts: int = 1_700_000_000_000) -> pd.DataFrame:
    """Synthetic geometric random-walk prices, ts-ms index like the cache."""
    rng = np.random.default_rng(seed)
    idx = [start_ts + i * TF_MS for i in range(n_bars)]
    cols = {}
    for j in range(n_syms):
        rets = rng.normal(0.0002, 0.01, n_bars)
        cols[f"SYM{j}"] = 100 * np.exp(np.cumsum(rets))
    return pd.DataFrame(cols, index=idx)


@pytest.fixture
def closes() -> pd.DataFrame:
    return make_closes()


@pytest.fixture
def opens(closes: pd.DataFrame) -> pd.DataFrame:
    # open of bar t = close of bar t-1 (continuous market approximation)
    return closes.shift(1).bfill()


@pytest.fixture
def cfg() -> dict:
    return {
        "universes": {
            "crypto": {"exchange": "kraken", "timeframe": "4h",
                       "symbols": ["SYM0", "SYM1", "SYM2", "SYM3"],
                       "allow_short": True},
        },
        "costs": {"crypto": {"fee_bps": 26, "slippage_bps": 10}},
        "strategies": {
            "tsmom": {"enabled": True, "lookback_bars": {"crypto": 50},
                      "vol_lookback_bars": 30, "target_vol_annual": 0.15},
            "xsmom": {"enabled": True, "lookback_bars": {"crypto": 50},
                      "top_n": 2, "rebalance_every_bars": {"crypto": 10}},
            "meanrev": {"enabled": True, "zscore_lookback_bars": 20,
                        "entry_z": 2.0, "exit_z": 0.5, "max_weight": 0.15},
            "null_random": {"enabled": True, "seed": 42,
                            "rebalance_every_bars": {"crypto": 10}},
        },
        "risk": {"start_equity_usd": 100_000, "max_position_weight": 0.25,
                 "max_gross_exposure": 1.5, "kill_switch_drawdown": 0.20},
        "walkforward": {"train_bars": 200, "test_bars": 50, "step_bars": 50,
                        "grids": {"tsmom": {"lookback_bars": [30, 50]}}},
        "gates": {"min_paper_days": 30, "min_closed_trades": 50,
                  "max_drawdown": 0.20, "null_test_p_value": 0.10},
        "bot": {"log_level": "INFO"},
        "paper": {"poll_interval_seconds": 300, "min_history_bars": 100},
    }
