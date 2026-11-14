from __future__ import annotations

import pytest

from bot.gates.decision import (closed_trades_from_fills, compare_win_rates,
                                evaluate_stream)
from bot.models import PaperFill
from bot.paper.portfolio import PaperPortfolio
from bot.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"), str(tmp_path / "audit.jsonl"))
    yield s
    s.close()


def _pf(store, stream="tsmom_crypto", cost=0.0036):
    return PaperPortfolio(stream, "crypto", store, 100_000.0, 0.20, cost)


def test_portfolio_rebalance_and_equity(store):
    pf = _pf(store, cost=0.0)
    fills = pf.rebalance({"BTC/USD": 0.25}, {"BTC/USD": 50_000.0}, ts=1000)
    assert len(fills) == 1
    assert abs(fills[0].delta_units - 0.5) < 1e-9        # 25k / 50k
    assert pf.equity({"BTC/USD": 50_000.0}) == pytest.approx(100_000.0)
    # price doubles: equity = 75k cash + 0.5 * 100k = 125k
    assert pf.equity({"BTC/USD": 100_000.0}) == pytest.approx(125_000.0)


def test_portfolio_costs_reduce_cash(store):
    pf = _pf(store, cost=0.01)
    pf.rebalance({"BTC/USD": 0.25}, {"BTC/USD": 50_000.0}, ts=1000)
    # bought 25k notional, paid 1% = 250
    assert pf.equity({"BTC/USD": 50_000.0}) == pytest.approx(99_750.0)


def test_kill_switch_liquidates_and_halts(store):
    pf = _pf(store, cost=0.0)
    pf.rebalance({"BTC/USD": 1.0}, {"BTC/USD": 50_000.0}, ts=1000)
    pf.snapshot({"BTC/USD": 50_000.0}, ts=1000)
    pf.snapshot({"BTC/USD": 35_000.0}, ts=2000)   # -30% < -20% kill switch
    assert pf.halted
    assert pf.positions == {}
    # halted stream refuses to trade
    assert pf.rebalance({"BTC/USD": 1.0}, {"BTC/USD": 35_000.0}, 3000) == []


def test_crash_resume_round_trip(store):
    pf = _pf(store, cost=0.0)
    pf.rebalance({"BTC/USD": 0.25}, {"BTC/USD": 50_000.0}, ts=1000)
    cash_before = pf.cash
    resumed = _pf(store, cost=0.0)   # fresh object, same store
    assert resumed.cash == pytest.approx(cash_before)
    assert resumed.positions["BTC/USD"].units == pytest.approx(0.5)


def test_closed_trades_reconstruction():
    fills = [
        PaperFill("s", "crypto", "BTC/USD", 1000, 100.0, 10.0, 1000.0, 5.0,
                  "rebalance", id=1),
        PaperFill("s", "crypto", "BTC/USD", 2000, 110.0, -10.0, 1100.0, 5.0,
                  "rebalance", id=2),
    ]
    trades = closed_trades_from_fills(fills)
    assert len(trades) == 1
    # pnl = 10 * (110-100) - 5 - 5 = 90
    assert trades[0].pnl_usd == pytest.approx(90.0)
    assert trades[0].won


def test_compare_win_rates_sane():
    # identical rates → no significance
    r = compare_win_rates(50, 100, 50, 100)
    assert r["p_value"] > 0.4
    # strong edge → significant
    r = compare_win_rates(80, 100, 50, 100)
    assert r["p_value"] < 0.01
    # small cells use fisher
    r = compare_win_rates(3, 5, 2, 6)
    assert r["method"] == "fisher_exact"


def test_gate_inconclusive_without_data(store, cfg):
    _pf(store, cost=0.0)          # creates stream state only
    _pf(store, "null_random_crypto", cost=0.0)
    rep = evaluate_stream(store, "tsmom_crypto", "null_random_crypto",
                          cfg["gates"])
    assert rep.verdict == "INCONCLUSIVE"


def test_dry_run_guard(tmp_path):
    from bot.config import load_config
    p = tmp_path / "bad.yaml"
    p.write_text("dry_run: false\n")
    with pytest.raises(SystemExit):
        load_config(str(p))
