"""Regression tests for the parts that are easy to break while editing fast:
queue priority semantics and the latency pipeline.

  python tests.py

No test framework, just asserts and a pass/fail count, so it runs anywhere.
"""

import sys

from engine.backtest import Backtest
from engine.order_book import OrderBook
from sim.market import Market
from strategies.market_makers import InventorySkewMM

FAILED = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILED.append(name)


def test_fifo():
    b = OrderBook()
    b.add_limit("a", "buy", 99.0, 5)
    b.add_limit("mm", "buy", 99.0, 5)
    fills = b.add_market("flow", "sell", 7)
    check("oldest order fills first",
          fills[0].buy_agent == "a" and fills[0].size == 5)
    check("mm fills only after the order ahead is exhausted",
          fills[1].buy_agent == "mm" and fills[1].size == 2)


def test_requote_keeps_or_loses_position():
    b = OrderBook()
    b.add_limit("mm", "buy", 99.0, 4)
    b.add_limit("bg", "buy", 99.0, 6)
    b.replace("mm", "buy", 99.0, 4)
    check("same price re-quote keeps the front spot",
          b.queue_ahead("mm", "buy") == 0)
    b.replace("mm", "buy", 98.99, 4)
    b.replace("mm", "buy", 99.0, 4)
    check("moving away and coming back loses the spot",
          b.queue_ahead("mm", "buy") == 6)


def test_resize_priority():
    b = OrderBook()
    b.add_limit("mm", "buy", 99.0, 4)
    b.add_limit("bg", "buy", 99.0, 6)
    b.replace("mm", "buy", 99.0, 2)
    check("sizing down keeps the front spot",
          b.queue_ahead("mm", "buy") == 0 and b.bids[0].size == 2)
    b.replace("mm", "buy", 99.0, 5)
    check("sizing up keeps the original order in front",
          b.queue_ahead("mm", "buy") == 0)
    check("sizing up rests the extra at the back",
          [o.agent for o in b.bids] == ["mm", "bg", "mm"])


def test_queue_inspection():
    b = OrderBook()
    b.add_limit("bg", "buy", 99.02, 3)
    b.add_limit("bg", "buy", 99.0, 6)
    b.add_limit("mm", "buy", 99.0, 4)
    check("queue_ahead counts better prices plus earlier same price",
          b.queue_ahead("mm", "buy") == 9)
    check("queue_ahead is None with no resting order",
          b.queue_ahead("mm", "sell") is None)
    check("depth_at can exclude our own size",
          b.depth_at("buy", 99.0, exclude_agent="mm") == 6)


def test_partial_fill_then_top_up():
    b = OrderBook()
    b.add_limit("mm", "buy", 99.0, 4)
    b.add_limit("bg", "buy", 99.0, 6)
    b.add_market("flow", "sell", 3)      # mm filled 3, 1 left at the front
    b.replace("mm", "buy", 99.0, 4)      # top back up to 4
    check("remainder keeps priority after a partial fill",
          b.queue_ahead("mm", "buy") == 0)
    check("the topped up size goes behind the queue",
          [o.agent for o in b.bids] == ["mm", "bg", "mm"])


def _run(latency, steps=400, seed=3):
    market = Market(seed=seed, vol=0.04, flow_rate=6.0, informed_frac=0.2)
    return Backtest(market, InventorySkewMM(), steps=steps, latency=latency).run()


def test_latency_pipeline():
    a, b = _run(0), _run(0)
    check("runs are deterministic at the same seed",
          a["history"]["pnl"][-1] == b["history"]["pnl"][-1])
    zero, delayed = _run(0), _run(2)
    check("latency changes the outcome",
          zero["history"]["pnl"][-1] != delayed["history"]["pnl"][-1])
    check("latency delays the first quote reaching the book",
          delayed["quotes_offered"] < zero["quotes_offered"])
    check("stale cost grows with latency",
          abs(delayed["stale_cost"]) > abs(zero["stale_cost"]))


def test_seam_intact():
    """Strategies must only ever see the documented state keys."""
    expected = {"fair_est", "inventory", "cash", "vol_est", "time_left",
                "book", "tick", "queue_ahead_bid", "queue_ahead_ask"}
    seen = {}

    class Spy(InventorySkewMM):
        def quote(self, state):
            seen.update(state)
            return super().quote(state)

    market = Market(seed=1)
    Backtest(market, Spy(), steps=50).run()
    check("state dict exposes exactly the documented seam keys",
          set(seen) == expected)


if __name__ == "__main__":
    for fn in (test_fifo, test_requote_keeps_or_loses_position,
               test_resize_priority, test_queue_inspection,
               test_partial_fill_then_top_up, test_latency_pipeline,
               test_seam_intact):
        fn()
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
        sys.exit(1)
    print("all passed")
