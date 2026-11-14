"""Tests for the parts that touch money: paper broker P&L, risk sizing,
and the end-to-end backtest loop. Run: python3 -m pytest tests/ -q
(or: python3 tests/test_core.py)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.backtest.engine import Backtester
from tradebot.brain.brain import Brain
from tradebot.brokers.paper import PaperBroker
from tradebot.core.risk import RiskConfig, RiskEngine
from tradebot.core.strategy import EmaRsiStrategy, ema, rsi
from tradebot.core.types import Account, Candle, Decision, Order, Side, Signal
from tradebot.data import feed

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_paper_broker_realizes_pnl():
    b = PaperBroker(starting_cash=10_000, fee_pct=0.0, slippage_pct=0.0)
    b.mark("X", 100.0)
    b.submit(Order(TS, "X", Side.BUY, qty=10))
    assert abs(b.account().position("X").qty - 10) < 1e-9
    b.mark("X", 110.0)
    # Unrealized should lift equity by 10 * 10 = 100.
    assert abs(b.account().equity - 10_100) < 1e-6
    b.close("X")
    # Realized: cash up by 100, position flat.
    assert b.account().position("X").is_flat
    assert abs(b.account().cash - 10_100) < 1e-6


def test_paper_broker_short_pnl():
    b = PaperBroker(starting_cash=10_000, fee_pct=0.0, slippage_pct=0.0)
    b.mark("X", 100.0)
    b.submit(Order(TS, "X", Side.SELL, qty=5))
    assert b.account().position("X").qty == -5
    b.mark("X", 90.0)  # price falls, short profits
    assert abs(b.account().equity - 10_050) < 1e-6


def test_fees_reduce_cash():
    b = PaperBroker(starting_cash=10_000, fee_pct=0.001, slippage_pct=0.0)
    b.mark("X", 100.0)
    b.submit(Order(TS, "X", Side.BUY, qty=10))  # notional 1000, fee 1.0
    assert abs(b.account().cash - 9_999) < 1e-6


def test_risk_rejects_low_confidence():
    r = RiskEngine(RiskConfig(min_confidence=0.6))
    acct = Account(cash=10_000, equity=10_000)
    d = Decision(TS, "X", Side.BUY, confidence=0.5, size_hint=1.0, rationale="")
    order, reason = r.size_order(d, acct, price=100.0)
    assert order is None and "confidence" in reason


def test_risk_caps_position_size():
    cfg = RiskConfig(max_position_pct=0.2, risk_per_trade_pct=1.0,
                     stop_loss_pct=0.02, min_confidence=0.0)
    r = RiskEngine(cfg)
    acct = Account(cash=10_000, equity=10_000)
    d = Decision(TS, "X", Side.BUY, confidence=0.9, size_hint=1.0, rationale="")
    order, reason = r.size_order(d, acct, price=100.0)
    # Notional cap = 20% * 10k = 2000 -> qty <= 20 at price 100.
    assert order is not None and order.qty <= 20 + 1e-9


def test_kill_switch_halts():
    r = RiskEngine(RiskConfig(max_daily_drawdown_pct=0.05))
    acct = Account(cash=10_000, equity=10_000)
    r.start_day(10_000)
    acct.equity = 9_400  # -6% > 5% limit
    assert r.check_kill_switch(acct) is True
    d = Decision(TS, "X", Side.BUY, confidence=0.9, size_hint=1.0, rationale="")
    order, reason = r.size_order(d, acct, price=100.0)
    assert order is None and "halted" in reason


def test_kill_switch_holds_for_the_day_then_resets():
    from datetime import timedelta
    r = RiskEngine(RiskConfig(max_daily_drawdown_pct=0.05))
    acct = Account(cash=10_000, equity=10_000)
    r.start_day(10_000)
    assert r.roll_day(TS, 10_000) is False          # first bar only records the day
    acct.equity = 9_400
    assert r.check_kill_switch(acct) is True
    # Recovering equity later the same day does not clear the halt.
    assert r.roll_day(TS + timedelta(hours=5), 9_900) is False
    acct.equity = 9_900
    assert r.check_kill_switch(acct) is True
    # The next UTC day starts a fresh session from that day's opening equity.
    assert r.roll_day(TS + timedelta(days=1), 9_900) is True
    assert r.check_kill_switch(acct) is False
    acct.equity = 9_400                              # -5.05% from 9,900
    assert r.check_kill_switch(acct) is True


def test_backtests_roll_the_risk_day_each_bar():
    from tradebot.backtest.portfolio import PortfolioBacktester

    class CountingRisk(RiskEngine):
        def __init__(self):
            super().__init__()
            self.rolls = 0

        def roll_day(self, ts, equity):
            rolled = super().roll_day(ts, equity)
            self.rolls += rolled
            return rolled

    candles = list(feed.synthetic_candles(n=72, seed=1))   # hourly: three days
    days = len({c.ts.date() for c in candles})
    single, multi = CountingRisk(), CountingRisk()
    Backtester(EmaRsiStrategy(), Brain(use_llm=False), single,
               PaperBroker(10_000)).run(candles)
    PortfolioBacktester(EmaRsiStrategy, Brain(use_llm=False), multi,
                        PaperBroker(10_000)).run({"BTC-USD": candles})
    assert single.rolls == multi.rolls == days - 1


def test_summary_trades_are_round_trips():
    r = Backtester(EmaRsiStrategy(), Brain(use_llm=False), RiskEngine(),
                   PaperBroker(10_000)).run(list(feed.synthetic_candles(n=500, seed=7)))
    assert r.metrics.n_trades == len(r.trades)
    assert f"Trades: {len(r.trades)} " in r.summary()
    assert f"Orders: {r.n_orders} " in r.summary()


def test_backtest_records_when_the_kill_switch_fires():
    from datetime import timedelta

    class AlwaysBuy:
        def on_candle(self, c):
            return Signal(c.ts, c.symbol, Side.BUY, 0.9, "test", {})

    # Two days of hourly bars: day one falls 1% an hour, day two is flat.
    candles, px = [], 100.0
    for h in range(48):
        px = px * 0.99 if h < 24 else px
        candles.append(Candle(TS + timedelta(hours=h), px, px, px, px, 1.0, "X"))
    cfg = RiskConfig(stop_loss_pct=0.5, take_profit_pct=0.5, max_position_pct=1.0,
                     risk_per_trade_pct=0.5, min_confidence=0.0)
    r = Backtester(AlwaysBuy(), Brain(use_llm=False), RiskEngine(cfg),
                   PaperBroker(10_000)).run(candles)
    assert len(r.halts) == 1 and r.halts[0].date() == TS.date(), r.halts


def test_report_return_matches_the_summary():
    from tradebot.backtest.dashboard import render_page
    r = Backtester(EmaRsiStrategy(), Brain(use_llm=False), RiskEngine(),
                   PaperBroker(10_000)).run(list(feed.synthetic_candles(n=500, seed=7)))
    page = render_page(r, "t", banner="Synthetic data")
    assert f"{r.total_return_pct:+.2f}%" in r.summary()
    assert f"{r.total_return_pct:+.2f}%" in page
    assert "Synthetic data" in page


def test_risk_blocks_stacking_past_cap():
    cfg = RiskConfig(max_position_pct=0.2, risk_per_trade_pct=1.0,
                     stop_loss_pct=0.02, min_confidence=0.0)
    r = RiskEngine(cfg)
    acct = Account(cash=10_000, equity=10_000)
    # Already holding the max notional (20% of 10k = 2000 -> 20 units @ 100).
    acct.position("X").qty = 20
    acct.position("X").avg_price = 100.0
    d = Decision(TS, "X", Side.BUY, confidence=0.9, size_hint=1.0, rationale="")
    order, reason = r.size_order(d, acct, price=100.0)
    assert order is None and "max notional" in reason


def test_trade_ledger_records_round_trip():
    from tradebot.core.strategy import EmaRsiStrategy
    b = PaperBroker(10_000, fee_pct=0.0, slippage_pct=0.0)
    b.mark("X", 100.0, TS)
    b.submit(Order(TS, "X", Side.BUY, qty=5))
    b.mark("X", 120.0, TS)
    b.close("X", reason="target")
    assert len(b.trades) == 1
    t = b.trades[0]
    assert t.won and abs(t.pnl - 100.0) < 1e-6 and t.reason_exit == "target"


def test_ensemble_requires_agreement():
    from tradebot.core.strategy import BollingerReversion, EmaRsiStrategy, EnsembleStrategy
    from tradebot.data import feed
    ens = EnsembleStrategy(EmaRsiStrategy(), BollingerReversion(), require=2)
    fired = [s for c in feed.synthetic_candles(n=800, seed=3)
             if (s := ens.on_candle(c))]
    # With require=2 every emitted signal must have both strategies agreeing.
    assert all(s.features.get("agree", 0) >= 2 for s in fired)


def test_indicators_sane():
    up = [float(i) for i in range(1, 30)]
    assert rsi(up) == 100.0  # only gains
    assert ema(up, 5) is not None


def test_backtest_runs_and_is_deterministic():
    def make():
        return Backtester(EmaRsiStrategy(), Brain(use_llm=False),
                          RiskEngine(), PaperBroker(10_000))
    candles = list(feed.synthetic_candles(n=500, seed=7))
    r1 = make().run(candles)
    r2 = make().run(list(feed.synthetic_candles(n=500, seed=7)))
    assert r1.n_orders == r2.n_orders
    assert r1.metrics.n_trades == r2.metrics.n_trades
    assert abs(r1.ending_equity - r2.ending_equity) < 1e-6
    assert len(r1.equity_curve) == 500


def test_montecarlo_edge_is_everything():
    from tradebot.sim.montecarlo import MCParams, simulate
    # No edge (coin flip) -> essentially nobody compounds $1 to $1M.
    no_edge = simulate(MCParams(win_prob=0.50, risk_frac=0.05, min_stake=0.01),
                       n_traders=3000, seed=1)
    assert no_edge.p_target < 0.01
    # Overbetting a real edge -> ruin/bleed dominates vs careful sizing.
    aggressive = simulate(MCParams(win_prob=0.55, risk_frac=0.6, min_stake=0.01),
                          n_traders=3000, seed=1)
    careful = simulate(MCParams(win_prob=0.55, risk_frac=0.05, min_stake=0.01),
                       n_traders=3000, seed=1)
    assert careful.median_final > aggressive.median_final


def test_breakeven_costs_make_small_targets_impossible():
    from tradebot.sim.breakeven import CostModel, breakeven_win_rate, net_expectancy
    costs = CostModel()  # 30 bps round trip
    # A 0.1% target costs 3x the move to capture -> unwinnable at any win rate.
    assert breakeven_win_rate(0.001, 1.0, costs) > 1.0
    # A 10% target is easily viable with a modest edge.
    assert breakeven_win_rate(0.10, 1.0, costs) < 0.55
    # Required win rate must fall monotonically as the target grows.
    needs = [breakeven_win_rate(t, 1.0, costs) for t in (0.005, 0.01, 0.02, 0.05)]
    assert needs == sorted(needs, reverse=True)
    # Frictionless trading breaks even at exactly 50% on a 1:1 payoff.
    free = CostModel(fee_pct=0.0, slippage_pct=0.0)
    assert abs(breakeven_win_rate(0.02, 1.0, free) - 0.5) < 1e-9
    # Expectancy at the break-even win rate is zero by construction.
    p = breakeven_win_rate(0.05, 1.0, costs)
    assert abs(net_expectancy(p, 0.05, 1.0, costs)) < 1e-9


def test_brain_cost_estimate():
    b = Brain(use_llm=False, model="claude-haiku-4-5")
    b.in_tokens = 1_000_000   # 1M input @ $1
    b.out_tokens = 1_000_000  # 1M output @ $5
    assert abs(b.est_cost() - 6.0) < 1e-9
    b2 = Brain(use_llm=False, model="claude-opus-4-8")
    b2.in_tokens = 1_000_000
    b2.out_tokens = 1_000_000
    assert abs(b2.est_cost() - 30.0) < 1e-9   # $5 + $25


def test_pattern_analysis_detects_uptrend():
    from datetime import datetime, timedelta, timezone
    from tradebot.core.patterns import analyze
    # Construct a clean rising series -> should read as an uptrend.
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    candles = []
    for i in range(120):
        px = 100 + i * 0.8
        candles.append(Candle(t0 + timedelta(hours=i), px - 0.2, px + 0.5,
                              px - 0.5, px, 10, "UP"))
    ca = analyze(candles)
    assert ca.trend == "up" and ca.slope_pct > 0
    assert "trend_strength" in ca.features()


def test_candlestick_bullish_engulfing():
    from datetime import datetime, timezone
    from tradebot.core.patterns import candle_patterns
    t = datetime(2024, 1, 1, tzinfo=timezone.utc)
    prev = Candle(t, 100, 100.5, 98, 98.5, 10, "X")     # bearish
    cur = Candle(t, 98, 101.5, 97.8, 101, 10, "X")      # bullish, engulfs prev
    assert "bullish_engulfing" in candle_patterns([prev, cur])


def test_risk_profiles_scale_size():
    cons = RiskConfig.profile("conservative")
    aggr = RiskConfig.profile("aggressive")
    assert aggr.max_position_pct > cons.max_position_pct
    assert aggr.risk_per_trade_pct > cons.risk_per_trade_pct
    assert aggr.max_daily_drawdown_pct > cons.max_daily_drawdown_pct


def test_webhook_parses_tradingview_alert():
    from tradebot.live.webhook import parse_alert
    sig = parse_alert({"symbol": "BTC-USD", "side": "buy", "strength": 0.9,
                       "reason": "flip", "price": 30000})
    assert sig and sig.side == Side.BUY and sig.strength == 0.9
    assert parse_alert({"side": "buy"}) is None            # no symbol
    assert parse_alert({"symbol": "X", "side": "hold"}) is None  # flat


def test_live_trader_runs_and_fills():
    from tradebot.brokers.paper import PaperBroker
    from tradebot.core.strategy import BollingerReversion, EmaRsiStrategy, EnsembleStrategy
    from tradebot.data import feed
    from tradebot.live.engine import LiveConfig, LiveTrader
    events = []
    tick = feed.make_synthetic_ticker("BTC-USD", seed=7)
    trader = LiveTrader(
        lambda: EnsembleStrategy(EmaRsiStrategy(), BollingerReversion(), require=1),
        Brain(use_llm=False), RiskEngine(RiskConfig()), PaperBroker(10_000),
        fetch_latest=tick, config=LiveConfig(symbols=["BTC-USD"], poll_seconds=0),
        on_event=events.append)
    trader.run(max_iterations=400)
    kinds = {e["event"] for e in events}
    assert "start" in kinds and "fill" in kinds  # produced at least one trade


def test_live_external_signal_routes():
    from datetime import datetime, timezone
    from tradebot.brokers.paper import PaperBroker
    from tradebot.core.strategy import EmaRsiStrategy
    from tradebot.live.engine import LiveConfig, LiveTrader
    events = []
    # Feed a flat candle so a mark exists, then push an external BUY signal.
    price_candle = Candle(datetime(2024, 1, 1, tzinfo=timezone.utc),
                          100, 100, 100, 100, 1, "Z")
    trader = LiveTrader(
        EmaRsiStrategy, Brain(use_llm=False),
        RiskEngine(RiskConfig(min_confidence=0.0)), PaperBroker(10_000),
        fetch_latest=lambda s: price_candle,
        config=LiveConfig(symbols=["Z"], poll_seconds=0), on_event=events.append)
    trader.tick_once()  # establishes a mark for Z
    trader.submit_external_signal(Signal(
        price_candle.ts, "Z", Side.BUY, 0.9, "webhook", {"strategy": "tv"}))
    trader.tick_once()
    assert any(e["event"] == "fill" and e["symbol"] == "Z" for e in events)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
