"""Turn a backtest result into risk and PnL metrics, print side by side.

Risk control is the point of the ladder, so drawdown, adverse selection
and inventory get equal billing with PnL.
"""

import numpy as np


def summarize(result):
    pnl = result["history"]["pnl"]
    inv = result["history"]["inventory"]
    dpnl = np.diff(pnl, prepend=0.0)
    std = float(dpnl.std())
    sharpe = float(dpnl.mean() / std * np.sqrt(len(dpnl))) if std > 0 else 0.0
    peak = np.maximum.accumulate(pnl)
    max_dd = float((peak - pnl).max())
    quotes = result.get("quotes_offered", 0)
    q_fills = result.get("queue_fills", 0)
    return {
        "name": result["name"],
        "pnl": float(pnl[-1]),
        "sharpe": sharpe,
        "max_dd": max_dd,
        "fills": result["fills"],
        "fill_rate": result["fills"] / quotes if quotes else 0.0,
        "avg_queue": result.get("queue_sum", 0.0) / q_fills if q_fills else 0.0,
        "spread_capture": float(result["spread_capture"]),
        "adverse_selection": float(result["adverse_selection"]),
        "stale_cost": float(result.get("stale_cost", 0.0)),
        "avg_abs_inv": float(np.abs(inv).mean()),
        "max_abs_inv": int(np.abs(inv).max()),
        "end_inv": int(inv[-1]),
    }


COLUMNS = [
    ("Strategy", "name", "{:<13}"),
    ("PnL", "pnl", "{:>9.2f}"),
    ("Sharpe", "sharpe", "{:>7.2f}"),
    ("MaxDD", "max_dd", "{:>8.2f}"),
    ("Fills", "fills", "{:>6d}"),
    ("FillRt", "fill_rate", "{:>7.2f}"),
    ("AvgQ", "avg_queue", "{:>5.1f}"),
    ("SpreadCap", "spread_capture", "{:>10.2f}"),
    ("AdvSel", "adverse_selection", "{:>9.2f}"),
    ("Stale", "stale_cost", "{:>8.2f}"),
    ("AvgInv", "avg_abs_inv", "{:>7.1f}"),
    ("MaxInv", "max_abs_inv", "{:>7d}"),
    ("EndInv", "end_inv", "{:>7d}"),
]


def print_table(rows):
    header = "  ".join(
        fmt.replace("d", "").replace(".2f", "").replace(".1f", "").format(title)
        for title, _, fmt in COLUMNS
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print("  ".join(fmt.format(row[key]) for _, key, fmt in COLUMNS))
