"""Paper trading daemon.

Every poll interval:
  1. For each universe, refresh OHLCV cache from the feed (public endpoints).
  2. Find the latest COMPLETED bar. If a stream hasn't traded it yet, compute
     the strategy's weight matrix on trailing history, take the last row,
     apply risk caps, and rebalance the stream's paper portfolio at that
     bar's close (slippage lives in the cost model).
  3. Snapshot equity (kill switch checks happen inside the portfolio).

Streams are named "<strategy>_<universe>". State is in SQLite; the daemon
resumes cleanly after crash or SIGTERM.
"""

from __future__ import annotations

import logging
import signal
import time

import pandas as pd

from bot.backtest.costs import per_side_cost
from bot.data import cache
from bot.data.crypto import CryptoFeed
from bot.data.equities import EquityFeed
from bot.paper.portfolio import PaperPortfolio
from bot.risk.sizing import apply_risk_caps
from bot.store import Store
from bot.strategies import build_strategy, enabled_strategies

log = logging.getLogger("paper")


class PaperTrader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.store = Store(cfg["bot"]["db_path"], cfg["bot"]["audit_log_path"])
        self.cache_dir = cfg["bot"]["ohlcv_cache_dir"]
        self.stop = False

        self.feeds = {
            "crypto": CryptoFeed(cfg["universes"]["crypto"]["exchange"]),
            "equities": EquityFeed(),
        }
        self.streams: dict[str, dict] = {}
        for universe in cfg["universes"]:
            for strat_name in enabled_strategies(cfg):
                stream = f"{strat_name}_{universe}"
                self.streams[stream] = {
                    "strategy": build_strategy(strat_name, cfg, universe),
                    "universe": universe,
                    "portfolio": PaperPortfolio(
                        stream, universe, self.store,
                        cfg["risk"]["start_equity_usd"],
                        cfg["risk"]["kill_switch_drawdown"],
                        per_side_cost(cfg, universe)),
                }

    # ── data refresh ─────────────────────────────────────────────────────
    def refresh_universe(self, universe: str) -> pd.DataFrame:
        ucfg = self.cfg["universes"][universe]
        feed = self.feeds[universe]
        tf = ucfg["timeframe"]
        for sym in ucfg["symbols"]:
            cached = cache.load_bars(self.cache_dir, universe, sym, tf)
            since = cached[-1].ts if cached else None
            limit = None if cached else self.cfg["paper"]["min_history_bars"] * 2
            try:
                bars = feed.fetch_ohlcv(sym, tf, since_ms=since, limit=limit)
                if bars:
                    cache.save_bars(self.cache_dir, universe, bars)
            except Exception as e:  # network hiccups must not kill the daemon
                log.warning("fetch failed %s %s: %s", universe, sym, e)
        return cache.load_history_df(self.cache_dir, universe,
                                     ucfg["symbols"], tf)

    # ── one tick ─────────────────────────────────────────────────────────
    def tick(self) -> int:
        traded = 0
        min_hist = self.cfg["paper"]["min_history_bars"]
        for universe in self.cfg["universes"]:
            closes = self.refresh_universe(universe)
            if closes.empty or len(closes) < min_hist:
                log.info("%s: %d bars cached, need %d — warming up",
                         universe, len(closes), min_hist)
                continue
            latest_ts = int(closes.index[-1])
            prices = {s: float(closes[s].dropna().iloc[-1])
                      for s in closes.columns if closes[s].notna().any()}

            for stream, parts in self.streams.items():
                if parts["universe"] != universe:
                    continue
                pf: PaperPortfolio = parts["portfolio"]
                if pf.halted or pf.last_bar_ts >= latest_ts:
                    continue
                w = parts["strategy"].weights(closes)
                w = apply_risk_caps(w, self.cfg["risk"])
                targets = {s: float(x) for s, x in w.iloc[-1].items()
                           if s in prices}
                fills = pf.rebalance(targets, prices, latest_ts)
                snap = pf.snapshot(prices, latest_ts)
                traded += 1
                log.info("%s: bar %d — %d fills, equity $%.2f%s",
                         stream, latest_ts, len(fills), snap.equity_usd,
                         " [HALTED]" if pf.halted else "")
        return traded

    # ── daemon loop ──────────────────────────────────────────────────────
    def run(self) -> None:
        def _sig(_signo, _frame):
            log.info("SIGTERM/SIGINT — shutting down after current tick")
            self.stop = True
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)

        interval = self.cfg["paper"]["poll_interval_seconds"]
        log.info("paper daemon started: %d streams, poll every %ds",
                 len(self.streams), interval)
        while not self.stop:
            started = time.time()
            try:
                self.tick()
            except Exception:
                log.exception("tick failed — retrying next interval")
            sleep_for = max(1.0, interval - (time.time() - started))
            for _ in range(int(sleep_for)):
                if self.stop:
                    break
                time.sleep(1)
        log.info("paper daemon stopped cleanly")
