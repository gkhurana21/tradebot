"""Tests for the LangGraph research brain.

Every model call is faked, so the graph's routing, degradation, and cost
guarding are verified with no API key and no spend.

Run: PYTHONPATH=src python3 tests/test_graph.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.core.types import Account, Position, Side, Signal

TS = datetime(2024, 1, 1, tzinfo=timezone.utc)


def signal(side=Side.BUY, strength=0.8, **features):
    feats = {"rsi": 55.0, "strategy": "test", **features}
    return Signal(ts=TS, symbol="BTC-USD", side=side, strength=strength,
                  reason="test signal", features=feats)


def account(equity=10_000.0, qty=0.0):
    acct = Account(cash=equity, equity=equity)
    if qty:
        acct.positions["BTC-USD"] = Position(symbol="BTC-USD", qty=qty, avg_price=100.0)
    return acct


class ScriptedChat:
    """Replays canned model replies, keyed by which prompt is in play."""

    def __init__(self, thesis=None, critique=None, judge=None):
        self.thesis = thesis or {"side": "BUY", "confidence": 0.8, "argument": "trend is up"}
        self.critique = critique or {"severity": 0.2, "fatal": False, "objection": "minor"}
        self.judge = judge or {"side": "BUY", "confidence": 0.75, "size_hint": 0.6,
                               "veto": False, "rationale": "thesis holds"}
        self.seen: list[str] = []

    def __call__(self, system: str, user: str) -> str:
        if "case FOR" in system:
            self.seen.append("thesis")
            return json.dumps(self.thesis)
        if "risk officer" in system:
            self.seen.append("critique")
            return json.dumps(self.critique)
        self.seen.append("judge")
        return json.dumps(self.judge)


def brain(chat, **kwargs):
    from tradebot.brain.graph import GraphBrain
    return GraphBrain(chat=chat, **kwargs)


# --- tests -----------------------------------------------------------------

def test_full_path_runs_thesis_critique_judge():
    chat = ScriptedChat()
    decision = brain(chat).decide(signal(), account(), 100.0)

    assert chat.seen == ["thesis", "critique", "judge"], chat.seen
    assert decision.side == Side.BUY
    assert not decision.veto
    assert 0.7 < decision.confidence < 0.8


def test_fatal_critique_skips_the_judge_call():
    chat = ScriptedChat(critique={"severity": 0.95, "fatal": True,
                                  "objection": "RSI is exhausted"})
    b = brain(chat)
    decision = b.decide(signal(), account(), 100.0)

    # The whole point of the conditional edge: no judge call is made.
    assert chat.seen == ["thesis", "critique"], chat.seen
    assert decision.veto and decision.side == Side.FLAT
    assert "RSI is exhausted" in decision.rationale
    assert b.calls_made == 2, "a vetoed decision must cost 2 calls, not 3"


def test_high_severity_vetoes_even_without_the_fatal_flag():
    chat = ScriptedChat(critique={"severity": 0.85, "fatal": False,
                                  "objection": "thin evidence"})
    decision = brain(chat).decide(signal(), account(), 100.0)
    assert decision.veto, "severity above the threshold must veto on its own"


def test_judge_can_override_a_bullish_thesis():
    chat = ScriptedChat(
        thesis={"side": "BUY", "confidence": 0.9, "argument": "strong breakout"},
        critique={"severity": 0.6, "fatal": False, "objection": "move already made"},
        judge={"side": "FLAT", "confidence": 0.2, "size_hint": 0.0,
               "veto": True, "rationale": "objection is better supported"},
    )
    decision = brain(chat).decide(signal(), account(), 100.0)
    assert decision.veto and decision.side == Side.FLAT


def test_rationale_carries_both_sides_of_the_argument():
    chat = ScriptedChat(
        thesis={"side": "BUY", "confidence": 0.8, "argument": "EMA crossover confirmed"},
        critique={"severity": 0.3, "fatal": False, "objection": "volume is weak"},
    )
    decision = brain(chat).decide(signal(), account(), 100.0)
    # A losing trade is only reviewable if the counter-argument was recorded.
    assert "EMA crossover confirmed" in decision.rationale
    assert "volume is weak" in decision.rationale


def test_gather_node_adds_cost_context_without_a_model_call():
    from tradebot.brain.graph import GraphBrain
    facts = GraphBrain._node_gather({
        "side": "BUY", "strength": 0.8, "reason": "x", "price": 100.0,
        "features": {"rsi": 61.0, "trend": "up", "zscore": 1.4},
        "position_qty": 0.0, "extra": {},
    })["facts"]

    assert facts["rsi"] == 61.0 and facts["trend"] == "up"
    # Cost drag is the number models ignore unless it is handed to them.
    assert facts["round_trip_cost_pct"] == 0.30
    assert facts["already_holding"] is False


def test_existing_position_is_visible_to_the_graph():
    from tradebot.brain.graph import GraphBrain
    facts = GraphBrain._node_gather({
        "side": "BUY", "strength": 0.8, "reason": "x", "price": 100.0,
        "features": {}, "position_qty": 2.5, "extra": {},
    })["facts"]
    assert facts["already_holding"] is True and facts["position_qty"] == 2.5


def test_a_failed_critique_is_treated_as_maximum_doubt():
    """A missing adversary must not read as 'no objection'."""
    class BrokenCritique(ScriptedChat):
        def __call__(self, system, user):
            if "risk officer" in system:
                raise RuntimeError("model unavailable")
            return super().__call__(system, user)

    decision = brain(BrokenCritique()).decide(signal(), account(), 100.0)
    assert decision.veto, "losing the critique step must fail closed, not open"


def test_a_failed_thesis_does_not_crash_the_loop():
    class BrokenThesis(ScriptedChat):
        def __call__(self, system, user):
            if "case FOR" in system:
                raise RuntimeError("model unavailable")
            return super().__call__(system, user)

    decision = brain(BrokenThesis()).decide(signal(), account(), 100.0)
    assert decision.symbol == "BTC-USD"  # a Decision is still produced


def test_cost_guard_stops_further_research():
    chat = ScriptedChat()
    b = brain(chat, max_calls=2)

    b.decide(signal(), account(), 100.0)   # consumes the budget
    before = b.calls_made
    second = b.decide(signal(), account(), 100.0)

    assert b.calls_made == before, "no calls may be made past the cap"
    assert second.veto and "cost guard" in second.rationale


def test_malformed_model_output_degrades_safely():
    class Garbage(ScriptedChat):
        def __call__(self, system, user):
            return "I'm afraid I can't do that."

    decision = brain(Garbage()).decide(signal(), account(), 100.0)
    assert decision.veto, "unparseable output must not become a trade"


def test_cost_report_prices_the_configured_model():
    b = brain(ScriptedChat(), model="claude-haiku-4-5")
    b.in_tokens, b.out_tokens = 1_000_000, 1_000_000
    report = b.cost_report()
    assert "$6.0000" in report, report  # $1 in + $5 out per million


def test_output_is_a_drop_in_decision():
    """The graph must satisfy the same contract as the plain Brain."""
    from tradebot.brain.brain import Brain
    from tradebot.core.risk import RiskConfig, RiskEngine

    graph_decision = brain(ScriptedChat()).decide(signal(), account(), 100.0)
    plain_decision = Brain(use_llm=False).decide(signal(), account(), 100.0)
    assert type(graph_decision) is type(plain_decision)

    # And it must flow through the risk engine unchanged.
    engine = RiskEngine(RiskConfig())
    engine.start_day(10_000)
    order, reason = engine.size_order(graph_decision, account(), 100.0)
    assert order is not None, f"risk engine rejected the graph decision: {reason}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = []
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    print()
    if failures:
        print(f"{len(failures)} of {len(fns)} failed")
        sys.exit(1)
    print(f"{len(fns)} tests passed")
