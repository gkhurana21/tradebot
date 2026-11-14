"""Config loading + the dry_run hard guard."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml


class LiveTradingNotBuilt(SystemExit):
    pass


def load_config(path: str = "config.yaml") -> dict:
    cfg = yaml.safe_load(Path(path).read_text())
    if cfg.get("dry_run") is not True:
        raise LiveTradingNotBuilt(
            "\n" + "=" * 72 +
            "\n  REFUSING TO START: dry_run is not true."
            "\n  Live trading is NOT BUILT. This platform is backtest + paper only."
            "\n  A live phase may only be considered after the gates in config"
            "\n  (gates:) pass for a stream. Set dry_run: true in config.yaml."
            "\n" + "=" * 72
        )
    return cfg
