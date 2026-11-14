"""Tests for the null-random significance test. Run: python3 tests/test_nulltest.py

They check the test itself, not any strategy: the p-value arithmetic, that the
coin is fair and reproducible, that the null keeps the strategy's turnover,
that a planted edge is detected, and that p-values are calibrated when there
is no edge to find.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.brain.brain import Brain
from tradebot.core.strategy import EmaRsiStrategy
from tradebot.core.types import Decision, Side
from tradebot.data import feed
from tradebot.sim.nulltest import NullDirectionBrain, coin, empirical_p, run_nulltest


def _streams(n=300, seed=3):
    return {"BTC-USD": list(feed.synthetic_candles("BTC-USD", n=n, seed=seed))}


def test_p_value_bounds():
    assert empirical_p(10.0, [1.0, 2.0, 3.0]) == 1 / 4      # beats every null
    assert empirical_p(0.0, [1.0, 2.0, 3.0]) == 1.0         # beaten by every null
    assert empirical_p(2.0, [1.0, 2.0, 3.0]) == 3 / 4       # ties count against


def test_coin_is_fair_and_reproducible():
    flips = [coin(7, "BTC-USD", e) for e in range(10_000)]
    share = flips.count(1) / len(flips)
    assert 0.48 < share < 0.52, share
    assert flips == [coin(7, "BTC-USD", e) for e in range(10_000)]
    assert flips != [coin(8, "BTC-USD", e) for e in range(10_000)]


def test_null_keeps_reversals_within_a_position_chain():
    from tradebot.core.types import Account

    class Fixed:
        def __init__(self, sides):
            self.sides = iter(sides)

        def decide(self, signal, account, price, extra=None):
            return Decision(signal, "X", next(self.sides), 0.9, 1.0, "")

    real = [Side.BUY, Side.BUY, Side.SELL, Side.BUY]
    acct = Account(cash=1_000, equity=1_000)
    null = NullDirectionBrain(Fixed(real), seed=1)
    out = []
    for i, _ in enumerate(real):
        out.append(null.decide(i, acct, 0.0).side)
        acct.position("X").qty = 1.0          # a position is now open
    # One chain, one sign: the null mirrors every reversal the brain makes.
    same = out[0] == real[0]
    assert all((o == r) == same for o, r in zip(out, real))


def test_null_keeps_the_strategys_turnover():
    res = run_nulltest(_streams(n=600), EmaRsiStrategy, seeds=9, workers=1)
    assert res.real_trades > 0
    assert abs(res.null_median_trades - res.real_trades) <= 0.35 * res.real_trades


def test_planted_edge_is_detected():
    # A strong, test-only edge: the brain knows the close 12 bars ahead. A
    # one-bar peek is too weak to show up in a few dozen trades, which is
    # itself the point: this test checks the machinery, not the power.
    streams = _streams(n=2000)
    candles = streams["BTC-USD"]
    ahead = 12
    nxt = {c.ts: candles[i + ahead].close for i, c in enumerate(candles[:-ahead])}

    class Peeking:
        """Test-only cheat: knows the close 12 bars ahead."""
        def __init__(self):
            self.inner = Brain(use_llm=False)

        def decide(self, signal, account, price, extra=None):
            d = self.inner.decide(signal, account, price, extra)
            if d.side not in (Side.BUY, Side.SELL) or d.ts not in nxt:
                return d
            side = Side.BUY if nxt[d.ts] > price else Side.SELL
            return Decision(d.ts, d.symbol, side, d.confidence, d.size_hint, "peek")

    res = run_nulltest(streams, EmaRsiStrategy, brain_factory=Peeking, seeds=19, workers=1)
    assert res.p_value <= 0.05, res.summary()


def test_p_values_are_calibrated_without_an_edge():
    # Treat one coin-flip run as "the strategy": it has no edge by construction,
    # so p <= 0.1 should happen about 10% of the time. Binomial(30, 0.1) has
    # mean 3; more than 9 hits has probability below 0.1%.
    streams = _streams(n=250, seed=5)
    hits = 0
    for m in range(30):
        def fake_real(m=m):
            return NullDirectionBrain(Brain(use_llm=False), seed=10_000 + m)
        res = run_nulltest(streams, EmaRsiStrategy, brain_factory=fake_real,
                           seeds=9, workers=1, first_seed=1 + 100 * m)
        hits += res.p_value <= 0.1
    assert hits <= 9, hits


def test_zero_costs_removes_fees_from_every_run():
    with_costs = run_nulltest(_streams(n=300), EmaRsiStrategy, seeds=3, workers=1)
    free = run_nulltest(_streams(n=300), EmaRsiStrategy, seeds=3, workers=1, zero_costs=True)
    assert free.real_trades == with_costs.real_trades           # same trades...
    assert free.real_return_pct > with_costs.real_return_pct     # ...without paying for them
    assert all(f > c for f, c in zip(free.null_returns_pct, with_costs.null_returns_pct))


def test_results_do_not_depend_on_worker_count():
    from tradebot.cli import _ensemble
    one = run_nulltest(_streams(n=300), _ensemble, seeds=4, workers=1)
    many = run_nulltest(_streams(n=300), _ensemble, seeds=4, workers=2)
    assert one.null_returns_pct == many.null_returns_pct
    assert one.p_value == many.p_value


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
