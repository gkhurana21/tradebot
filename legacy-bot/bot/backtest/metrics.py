"""Performance metrics over a per-bar net-return series."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_metrics(net_returns: pd.Series, bars_per_year: int) -> dict:
    r = net_returns.dropna()
    if len(r) == 0:
        return {"n_bars": 0}
    equity = (1 + r).cumprod()
    total_return = equity.iloc[-1] - 1
    years = len(r) / bars_per_year
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 else 0.0
    vol = r.std() * np.sqrt(bars_per_year)
    sharpe = (r.mean() * bars_per_year) / vol if vol > 0 else 0.0
    downside = r[r < 0].std() * np.sqrt(bars_per_year)
    sortino = (r.mean() * bars_per_year) / downside if downside > 0 else 0.0
    peak = equity.cummax()
    max_dd = ((equity - peak) / peak).min()
    return {
        "n_bars": int(len(r)),
        "years": round(years, 2),
        "total_return": round(float(total_return), 4),
        "cagr": round(float(cagr), 4),
        "vol_annual": round(float(vol), 4),
        "sharpe": round(float(sharpe), 3),
        "sortino": round(float(sortino), 3),
        "max_drawdown": round(float(max_dd), 4),
    }


def format_metrics_table(rows: dict[str, dict]) -> str:
    """rows: {label: metrics dict} → aligned text table."""
    cols = ["n_bars", "years", "total_return", "cagr", "vol_annual",
            "sharpe", "sortino", "max_drawdown", "turnover_annual",
            "avg_gross_exposure"]
    header = f"{'strategy':<14}" + "".join(f"{c:>16}" for c in cols)
    lines = [header, "-" * len(header)]
    for label, m in rows.items():
        lines.append(f"{label:<14}" +
                     "".join(f"{m.get(c, ''):>16}" for c in cols))
    return "\n".join(lines)
