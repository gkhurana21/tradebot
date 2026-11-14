"""Risk caps applied identically in backtest and paper trading."""

from __future__ import annotations

import pandas as pd


def apply_risk_caps(w: pd.DataFrame, risk_cfg: dict) -> pd.DataFrame:
    """Clip per-name weight, then scale rows whose gross exceeds the cap."""
    max_w = risk_cfg["max_position_weight"]
    max_gross = risk_cfg["max_gross_exposure"]
    capped = w.clip(lower=-max_w, upper=max_w)
    gross = capped.abs().sum(axis=1)
    scale = (max_gross / gross).clip(upper=1.0).fillna(1.0)
    return capped.mul(scale, axis=0)
