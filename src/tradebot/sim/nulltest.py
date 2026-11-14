"""Does the strategy beat a coin flip? A null-random significance test.

The question a backtest cannot answer on its own: is this return skill, or
what you would get by trading at the same moments in random directions?

Design, fixed before looking at any result:

  * **Null model.** The same strategy, brain, risk engine, fees, slippage and
    candles. The only change: after the brain decides, the null trades the
    brain's direction times a random sign. A new sign is drawn each time the
    null opens a position from flat, deterministic in (seed, symbol, chain),
    and is kept until the position is flat again. So the null reverses,
    stacks and exits exactly when the strategy's decisions would make it,
    and its turnover and fees match; only which way each position chain
    faces is random. This tests *direction skill given the entry timing*,
    not timing. A SELL when flat opens a short, so the sign is symmetric.
    (A first version flipped a coin per run of same-direction decisions; on
    real data it traded about a third less than the strategy, so it paid
    less in fees and the comparison was unfair. It was replaced before any
    result was published.)
  * **One primary metric:** total return after all costs, including the
    close-out at the end. Win rate, trade counts and the null spread are
    reported as description only and are never tested.
  * **p-value:** one-sided empirical, p = (1 + #null >= real) / (K + 1).
    It can never be 0, and with K = 199 the smallest possible value is 0.005.

Every number this produces is published as it comes out.
"""
from __future__ import annotations

import hashlib
import os
import statistics
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..backtest.portfolio import PortfolioBacktester, PortfolioResult
from ..brain.brain import Brain
from ..brokers.paper import PaperBroker
from ..core.risk import RiskConfig, RiskEngine
from ..core.strategy import Strategy
from ..core.types import Candle, Decision, Side


def coin(seed: int, symbol: str, chain: int) -> int:
    """A fair, reproducible sign: same inputs, same answer, on every machine."""
    digest = hashlib.sha256(f"{seed}|{symbol}|{chain}".encode()).digest()
    return 1 if digest[0] & 1 else -1


def _flip(side: Side) -> Side:
    return Side.SELL if side == Side.BUY else Side.BUY


class NullDirectionBrain:
    """Wraps a brain and gives each of its position chains a random sign.

    A chain runs from opening a position while flat until the position is
    flat again. Within a chain the brain's own decisions, reversals included,
    pass through with the chain's sign applied.
    """

    def __init__(self, inner: Brain, seed: int):
        self.inner = inner
        self.seed = seed
        self._chain: dict[str, int] = {}
        self._sign: dict[str, int] = {}

    def decide(self, signal, account, price, extra=None) -> Decision:
        d = self.inner.decide(signal, account, price, extra)
        if d.side not in (Side.BUY, Side.SELL) or d.veto:
            return d
        if d.symbol not in self._sign or account.position(d.symbol).is_flat:
            self._chain[d.symbol] = self._chain.get(d.symbol, -1) + 1
            self._sign[d.symbol] = coin(self.seed, d.symbol, self._chain[d.symbol])
        side = d.side if self._sign[d.symbol] > 0 else _flip(d.side)
        return Decision(d.ts, d.symbol, side, d.confidence, d.size_hint,
                        f"null: {d.rationale}", d.veto)


@dataclass
class NullTestResult:
    real_return_pct: float
    null_returns_pct: list[float]
    p_value: float
    real_trades: int
    null_median_trades: float
    real_win_rate_pct: float
    bars: int
    symbols: list[str] = field(default_factory=list)
    start: str = ""
    end: str = ""

    @property
    def seeds(self) -> int:
        return len(self.null_returns_pct)

    def null_quantile(self, q: float) -> float:
        xs = sorted(self.null_returns_pct)
        return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]

    def summary(self) -> str:
        verdict = ("beats the coin flip at p < 0.05" if self.p_value < 0.05
                   else "does not beat the coin flip (p >= 0.05)")
        return "\n".join([
            f"Data           : {', '.join(self.symbols)} · {self.bars} bars · {self.start} → {self.end}",
            f"Strategy       : {self.real_return_pct:+.2f}% total return · "
            f"{self.real_trades} round trips · {self.real_win_rate_pct:.1f}% win rate",
            f"Coin flips (K={self.seeds}): median {statistics.median(self.null_returns_pct):+.2f}% · "
            f"5th–95th pct {self.null_quantile(0.05):+.2f}% to {self.null_quantile(0.95):+.2f}% · "
            f"median {self.null_median_trades:.0f} round trips",
            f"p-value        : {self.p_value:.3f} (one-sided, total return)",
            f"Verdict        : the strategy {verdict}",
        ])


def heuristic_brain() -> Brain:
    """The free, deterministic brain. No API calls, so 200 runs cost nothing."""
    return Brain(use_llm=False)


def empirical_p(real: float, nulls: list[float]) -> float:
    """One-sided: the chance a coin-flip run does at least as well as `real`."""
    return (1 + sum(1 for x in nulls if x >= real)) / (len(nulls) + 1)


def _run(streams, strategy_factory, brain, risk_cfg: RiskConfig, cash: float) -> PortfolioResult:
    cfg = RiskConfig(**vars(risk_cfg))     # fresh copy; the engine is stateful
    return PortfolioBacktester(strategy_factory, brain, RiskEngine(cfg),
                               PaperBroker(cash)).run(streams)


def _run_null(job) -> "_NullRun":
    streams, strategy_factory, brain_factory, risk_cfg, cash, seed = job
    r = _run(streams, strategy_factory, NullDirectionBrain(brain_factory(), seed), risk_cfg, cash)
    return _NullRun(r.total_return_pct, r.metrics.n_trades)


@dataclass
class _NullRun:
    total_return_pct: float
    n_trades: int


def run_nulltest(
    streams: dict[str, list[Candle]],
    strategy_factory: Callable[[], Strategy],
    brain_factory: Callable[[], Brain] | None = None,
    risk_cfg: RiskConfig | None = None,
    cash: float = 25_000.0,
    seeds: int = 199,
    first_seed: int = 1,
    workers: int | None = None,
) -> NullTestResult:
    """Run the strategy once and its coin-flip twin `seeds` times.

    Factories must be module-level callables so runs can go to worker
    processes; results are identical with any number of workers.
    """
    brain_factory = brain_factory or heuristic_brain
    risk_cfg = risk_cfg or RiskConfig(max_open_positions=max(3, len(streams)))
    real = _run(streams, strategy_factory, brain_factory(), risk_cfg, cash)
    jobs = [(streams, strategy_factory, brain_factory, risk_cfg, cash, first_seed + k)
            for k in range(seeds)]
    if workers is None:
        workers = min(len(jobs), os.cpu_count() or 1)
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            nulls = list(pool.map(_run_null, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    else:
        nulls = [_run_null(j) for j in jobs]
    all_ts = [c.ts for cs in streams.values() for c in cs]
    return NullTestResult(
        real_return_pct=real.total_return_pct,
        null_returns_pct=[n.total_return_pct for n in nulls],
        p_value=empirical_p(real.total_return_pct, [n.total_return_pct for n in nulls]),
        real_trades=real.metrics.n_trades,
        null_median_trades=statistics.median(n.n_trades for n in nulls),
        real_win_rate_pct=real.metrics.win_rate_pct,
        bars=max(len(cs) for cs in streams.values()),
        symbols=list(streams),
        start=f"{min(all_ts):%Y-%m-%d}" if all_ts else "",
        end=f"{max(all_ts):%Y-%m-%d}" if all_ts else "",
    )
