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


def _run(streams, strategy_factory, brain, risk_cfg: RiskConfig, cash: float,
         zero_costs: bool = False) -> PortfolioResult:
    """One backtest. `zero_costs` sets fees and slippage to zero, which separates
    better trade choices from simply trading less (and paying less)."""
    cfg = RiskConfig(**vars(risk_cfg))     # fresh copy; the engine is stateful
    broker = (PaperBroker(cash, fee_pct=0.0, slippage_pct=0.0) if zero_costs
              else PaperBroker(cash))
    return PortfolioBacktester(strategy_factory, brain, RiskEngine(cfg), broker).run(streams)


def _run_null(job) -> "_NullRun":
    streams, strategy_factory, brain_factory, risk_cfg, cash, seed, zero_costs = job
    r = _run(streams, strategy_factory, NullDirectionBrain(brain_factory(), seed), risk_cfg, cash,
             zero_costs)
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
    zero_costs: bool = False,
) -> NullTestResult:
    """Run the strategy once and its coin-flip twin `seeds` times.

    Factories must be module-level callables so runs can go to worker
    processes; results are identical with any number of workers.
    """
    brain_factory = brain_factory or heuristic_brain
    risk_cfg = risk_cfg or RiskConfig(max_open_positions=max(3, len(streams)))
    real = _run(streams, strategy_factory, brain_factory(), risk_cfg, cash, zero_costs)
    jobs = [(streams, strategy_factory, brain_factory, risk_cfg, cash, first_seed + k, zero_costs)
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


def _run_twin(job) -> "_NullRun":
    streams, strategy_factory, twin_factory, risk_cfg, cash, seed, zero_costs = job
    r = _run(streams, strategy_factory, twin_factory(seed), risk_cfg, cash, zero_costs)
    return _NullRun(r.total_return_pct, r.metrics.n_trades)


def run_twins(
    streams: dict[str, list[Candle]],
    strategy_factory: Callable[[], Strategy],
    real_brain_factory: Callable[[], Brain],
    twin_factory: Callable[[int], object],
    risk_cfg: RiskConfig | None = None,
    cash: float = 25_000.0,
    seeds: int = 199,
    first_seed: int = 1,
    workers: int | None = None,
    zero_costs: bool = False,
) -> NullTestResult:
    """Like run_nulltest, but the twins are whatever `twin_factory(seed)` builds.

    run_nulltest's twin is fixed (the real brain with each position chain's
    direction sign-randomised). This one takes any seeded twin, so a different
    null, such as random vetoes and flips, shares the same p-value arithmetic
    and the same fees, slippage, candles and risk engine. Factories must be
    module-level callables so runs can go to worker processes.
    """
    risk_cfg = risk_cfg or RiskConfig(max_open_positions=max(3, len(streams)))
    real = _run(streams, strategy_factory, real_brain_factory(), risk_cfg, cash, zero_costs)
    jobs = [(streams, strategy_factory, twin_factory, risk_cfg, cash, first_seed + k, zero_costs)
            for k in range(seeds)]
    if workers is None:
        workers = min(len(jobs), os.cpu_count() or 1)
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            nulls = list(pool.map(_run_twin, jobs, chunksize=max(1, len(jobs) // (workers * 4))))
    else:
        nulls = [_run_twin(j) for j in jobs]
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


def histogram_svg(res: NullTestResult, title: str = "Strategy vs 199 coin flips",
                  bins: int = 24, w: int = 720, h: int = 300) -> str:
    """A self-contained SVG histogram of the coin-flip returns with the strategy marked.

    Neutral colours that read on both light and dark backgrounds, so it works
    as a README image on GitHub in either theme.
    """
    xs = res.null_returns_pct
    lo = min(xs + [res.real_return_pct])
    hi = max(xs + [res.real_return_pct])
    pad_v = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad_v, hi + pad_v
    width = (hi - lo) / bins
    counts = [0] * bins
    for x in xs:
        counts[min(bins - 1, int((x - lo) / width))] += 1
    left, right, top, bottom = 40, 20, 56, 44
    pw, ph = w - left - right, h - top - bottom
    peak = max(counts) or 1

    def px(v: float) -> float:
        return left + (v - lo) / (hi - lo) * pw

    bars = []
    for i, c in enumerate(counts):
        if not c:
            continue
        bh = c / peak * ph
        x0 = left + i * pw / bins
        bars.append(f'<rect x="{x0 + 1:.1f}" y="{top + ph - bh:.1f}" width="{pw / bins - 2:.1f}" '
                    f'height="{bh:.1f}" rx="2" fill="#8b949e" fill-opacity="0.55"/>')
    sx = px(res.real_return_pct)
    ticks = []
    step = 5 if hi - lo > 15 else 2
    t = (int(lo // step) + 1) * step
    while t < hi:
        ticks.append(f'<text x="{px(t):.1f}" y="{h - 18}" text-anchor="middle">{t:+d}%</text>')
        t += step
    font = "font-family=\"-apple-system,Segoe UI,Helvetica,Arial,sans-serif\" font-size=\"12\""
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img"
  aria-label="{title}: strategy {res.real_return_pct:+.2f}%, p = {res.p_value:.3f}">
  <g {font} fill="#8b949e">
    <text x="{left}" y="22" font-size="15" font-weight="600">{title}</text>
    <text x="{left}" y="41">Total return after costs · {", ".join(res.symbols)} · {res.start} → {res.end} · p = {res.p_value:.3f}</text>
    {"".join(ticks)}
  </g>
  <line x1="{left}" y1="{top + ph}" x2="{left + pw}" y2="{top + ph}" stroke="#8b949e" stroke-opacity="0.5"/>
  {"".join(bars)}
  <line x1="{sx:.1f}" y1="{top - 4}" x2="{sx:.1f}" y2="{top + ph}" stroke="#2f81f7" stroke-width="2.5"/>
  <text x="{sx + 6:.1f}" y="{top + 8}" {font} fill="#2f81f7" font-weight="600">strategy {res.real_return_pct:+.2f}%</text>
</svg>
'''
