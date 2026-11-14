"""Parity: the Python RiskEngine and the Go risk gate size orders the same way.

Both read tests/fixtures/risk_parity.json (engine/internal/risk/parity_test.go
is the Go half). Backtests and the coin-flip test use the Python engine, so this
is the evidence that their sizing matches the gate that trades. Intentional
differences are listed in docs/DESIGN.md record 8; none of them is exercised
here. Run: python3 tests/test_risk_parity.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tradebot.core.risk import RiskConfig, RiskEngine
from tradebot.core.types import Account, Decision, Position, Side

FIXTURE = ROOT / "tests" / "fixtures" / "risk_parity.json"
TS = datetime(2024, 1, 1, tzinfo=timezone.utc)

# Python returns a reason string; the Go gate returns a typed verdict. This maps
# one to the other so the fixtures can speak in Go's verdict names.
_VERDICT_BY_REASON = (
    ("halted", "rejected_halted"),
    ("no-trade", "rejected_veto"),
    ("confidence", "rejected_low_confidence"),
    ("max open positions", "rejected_max_open_positions"),
    ("max notional", "rejected_position_at_max_notional"),
    ("computed size is zero", "rejected_zero_size"),
)


def verdict_of(order, reason: str) -> str:
    if order is not None:
        return "approved"
    for needle, verdict in _VERDICT_BY_REASON:
        if needle in reason:
            return verdict
    raise AssertionError(f"unmapped Python reject reason: {reason!r}")


def run_case(case: dict):
    engine = RiskEngine(RiskConfig(**case["config"]))
    engine.start_day(case.get("session_start_equity", case["equity"]))
    acct = Account(
        cash=case["equity"], equity=case["equity"],
        positions={s: Position(s, qty=p["qty"]) for s, p in case["positions"].items()})
    engine.check_kill_switch(acct)
    d = case["decision"]
    decision = Decision(TS, d["symbol"], Side(d["side"]), d["confidence"],
                        d["size_hint"], "parity", veto=d["veto"])
    order, reason = engine.size_order(decision, acct, case["price"])
    return order, reason


def test_python_matches_the_shared_fixtures():
    fx = json.loads(FIXTURE.read_text())
    tol = fx["tolerance"]
    assert len(fx["cases"]) >= 20
    seen = set()
    for case in fx["cases"]:
        want = case["expected"]
        order, reason = run_case(case)
        got = verdict_of(order, reason)
        seen.add(got)
        assert got == want["verdict"], f"{case['name']}: {got} != {want['verdict']} ({reason})"
        assert (order is not None) == want["approved"], case["name"]
        if order is not None:
            for key in ("qty", "stop_loss", "take_profit"):
                assert abs(getattr(order, key) - want[key]) <= tol, \
                    f"{case['name']}: {key} {getattr(order, key)} != {want[key]}"
    # The fixtures must keep covering every verdict the gate can give.
    assert seen == {"approved", "rejected_halted", "rejected_veto", "rejected_low_confidence",
                    "rejected_max_open_positions", "rejected_position_at_max_notional",
                    "rejected_zero_size"}, seen


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
