"""A LangGraph research pipeline that argues with itself before trading.

The plain `Brain` makes one Claude call: everything goes in, a decision comes
out. That is fine, but a single pass has no mechanism for catching its own
motivated reasoning -- a model handed a bullish signal tends to find bullish
reasons.

This graph splits the work into stages and makes one of them adversarial:

    gather (free) ─→ thesis ─→ critique ─┬─→ judge ─→ Decision
                                         └─→ veto  ─→ Decision(veto=True)

  * gather   deterministic feature extraction -- no model call, no cost
  * thesis   argues FOR acting on the signal
  * critique argues AGAINST it, and only against it
  * judge    weighs both and commits

The conditional edge is why this is a graph rather than a chain: when the
critique is damning, routing straight to a veto skips the judge call entirely.
Cheap rejections are the common case, so the graph usually costs two calls, not
three.

Output is the same `Decision` the rest of the system already consumes, so the
backtester, live loop, and Go engine are unchanged.

Optional dependency: `pip install -e ".[graph]"`. Without it, everything else
still runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, TypedDict

from ..core.types import Account, Decision, Side, Signal

# One node's worth of tokens is small; the cost is in the number of hops.
MAX_TOKENS = 500

THESIS_PROMPT = """You are a trader building the case FOR acting on a signal.

Argue the bullish-or-bearish case that the signal implies. Be concrete about
what in the data supports it. Do not hedge into neutrality -- the critique step
exists to attack this, so your job here is the strongest honest case.

Respond with ONLY a JSON object:
{"side": "BUY"|"SELL", "confidence": 0.0-1.0, "argument": "two sentences max"}"""

CRITIQUE_PROMPT = """You are a risk officer whose only job is to find what is
wrong with a proposed trade. You are not balanced and you are not fair.

Attack the thesis. Look for: signals that contradict it, indicators near
exhaustion, thin evidence, a move already made, conditions where this setup
historically fails, and cost drag relative to the expected move.

If the trade is genuinely sound, say so -- but only after trying to break it.

Respond with ONLY a JSON object:
{"severity": 0.0-1.0, "fatal": true|false, "objection": "two sentences max"}

severity 0 means no real objection; 1 means the thesis is baseless.
fatal=true means this trade should not be taken at any size."""

JUDGE_PROMPT = """You decide whether to trade, having read both a thesis and a
critique of it.

You are not splitting the difference. Weigh which side is better supported by
the actual data. A strong thesis with a weak objection should still trade; a
weak thesis with a sharp objection should not. Most signals are noise -- when
the evidence is genuinely balanced, decline.

A separate risk engine sizes and can still veto whatever you approve, so
express conviction honestly rather than pre-hedging.

Respond with ONLY a JSON object:
{"side": "BUY"|"SELL"|"FLAT", "confidence": 0.0-1.0, "size_hint": 0.0-1.0,
 "veto": true|false, "rationale": "one sentence"}"""


class ResearchState(TypedDict, total=False):
    """State threaded through the graph. Each node adds its own slice."""
    # inputs
    symbol: str
    side: str
    strength: float
    reason: str
    features: dict
    price: float
    equity: float
    position_qty: float
    extra: dict
    # derived
    facts: dict
    thesis: dict
    critique: dict
    verdict: dict
    calls: int
    errors: list


def _extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:120]!r}")
    return json.loads(text[start : end + 1])


def _clamp(value: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


@dataclass
class GraphBrain:
    """Multi-step research brain backed by LangGraph.

    Mirrors `Brain`'s interface -- `decide(signal, account, price, extra)` ->
    `Decision` -- so it is a drop-in replacement anywhere the plain brain is
    used.
    """

    model: str = "claude-opus-5-5"
    max_calls: int | None = None
    # Below this severity the critique is treated as noise and the judge is
    # skipped in the thesis's favour; above `fatal_severity` the trade is
    # vetoed without a judge call.
    fatal_severity: float = 0.8

    # Injected for testing: takes (system, user) and returns the raw text.
    chat: Callable[[str, str], str] | None = None

    calls_made: int = field(default=0, init=False)
    in_tokens: int = field(default=0, init=False)
    out_tokens: int = field(default=0, init=False)
    _graph: Any = field(default=None, init=False, repr=False)
    _capped_warned: bool = field(default=False, init=False)

    # --- public API --------------------------------------------------------
    def decide(
        self,
        signal: Signal,
        account: Account,
        price: float,
        extra_context: dict | None = None,
    ) -> Decision:
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            if not self._capped_warned:
                self._capped_warned = True
                print(f"[graph] cost guard hit max_calls={self.max_calls}; "
                      f"declining further research")
            return self._flat(signal, "cost guard reached; standing down")

        pos = account.position(signal.symbol)
        state: ResearchState = {
            "symbol": signal.symbol,
            "side": signal.side.value,
            "strength": signal.strength,
            "reason": signal.reason,
            "features": signal.features,
            "price": price,
            "equity": round(account.equity, 2),
            "position_qty": round(pos.qty, 8),
            "extra": extra_context or {},
            "calls": 0,
            "errors": [],
        }

        try:
            final = self._compiled().invoke(state)
        except Exception as e:  # never let research break the trading loop
            print(f"[graph] research failed, standing down: {e}")
            return self._flat(signal, f"research error: {e}"[:200])

        return self._to_decision(signal, final)

    def cost_report(self) -> str:
        from .brain import price_for
        pin, pout = price_for(self.model)
        spend = self.in_tokens / 1e6 * pin + self.out_tokens / 1e6 * pout
        return (f"graph calls: {self.calls_made}  "
                f"tokens in/out: {self.in_tokens:,}/{self.out_tokens:,}  "
                f"est. spend: ${spend:.4f} ({self.model})")

    # --- graph construction ------------------------------------------------
    def _compiled(self):
        if self._graph is None:
            self._graph = self._build()
        return self._graph

    def _build(self):
        from langgraph.graph import END, START, StateGraph

        builder = StateGraph(ResearchState)
        builder.add_node("gather", self._node_gather)
        builder.add_node("thesis", self._node_thesis)
        builder.add_node("critique", self._node_critique)
        builder.add_node("judge", self._node_judge)
        builder.add_node("veto", self._node_veto)

        builder.add_edge(START, "gather")
        builder.add_edge("gather", "thesis")
        builder.add_edge("thesis", "critique")
        # The branch that earns the graph: a fatal objection skips the judge.
        builder.add_conditional_edges(
            "critique",
            self._route_after_critique,
            {"judge": "judge", "veto": "veto"},
        )
        builder.add_edge("judge", END)
        builder.add_edge("veto", END)
        return builder.compile()

    # --- nodes -------------------------------------------------------------
    @staticmethod
    def _node_gather(state: ResearchState) -> dict:
        """Deterministic feature extraction. No model call, so no cost.

        Doing this in code rather than prose keeps the expensive nodes focused
        on judgement instead of arithmetic they are bad at.
        """
        feats = state.get("features", {}) or {}
        price = state.get("price", 0.0) or 0.0

        facts: dict = {
            "signal_side": state.get("side"),
            "signal_strength": state.get("strength"),
            "signal_reason": state.get("reason"),
            "price": price,
            "already_holding": state.get("position_qty", 0.0) != 0.0,
            "position_qty": state.get("position_qty", 0.0),
        }
        for key in ("rsi", "trend", "trend_strength", "atr_pct", "zscore",
                    "spread_pct", "candlestick_patterns", "chart_patterns",
                    "nearest_support", "nearest_resistance"):
            if key in feats:
                facts[key] = feats[key]

        # Round-trip cost as a share of the move a 2% target would capture --
        # the single most decision-relevant number, and one models reliably
        # ignore unless it is put in front of them.
        facts["round_trip_cost_pct"] = 0.30
        facts["cost_share_of_2pct_target"] = "15%"

        if state.get("extra"):
            facts["market_context"] = state["extra"]
        return {"facts": facts}

    def _node_thesis(self, state: ResearchState) -> dict:
        payload = {"facts": state.get("facts", {})}
        data, err = self._ask(THESIS_PROMPT, payload)
        if err:
            return {"thesis": {"side": state.get("side"), "confidence": 0.0,
                               "argument": f"thesis unavailable: {err}"},
                    "errors": state.get("errors", []) + [err]}
        return {"thesis": {
            "side": str(data.get("side", state.get("side", "FLAT"))).upper(),
            "confidence": _clamp(data.get("confidence"), 0.0, 1.0),
            "argument": str(data.get("argument", ""))[:400],
        }}

    def _node_critique(self, state: ResearchState) -> dict:
        payload = {"facts": state.get("facts", {}), "thesis": state.get("thesis", {})}
        data, err = self._ask(CRITIQUE_PROMPT, payload)
        if err:
            # A missing critique must not read as "no objection"; without the
            # adversarial step the safe reading is maximum doubt.
            return {"critique": {"severity": 1.0, "fatal": True,
                                 "objection": f"critique unavailable: {err}"},
                    "errors": state.get("errors", []) + [err]}
        return {"critique": {
            "severity": _clamp(data.get("severity"), 0.0, 1.0, default=1.0),
            "fatal": bool(data.get("fatal", False)),
            "objection": str(data.get("objection", ""))[:400],
        }}

    def _route_after_critique(self, state: ResearchState) -> str:
        critique = state.get("critique", {}) or {}
        if critique.get("fatal") or critique.get("severity", 1.0) >= self.fatal_severity:
            return "veto"
        return "judge"

    def _node_judge(self, state: ResearchState) -> dict:
        payload = {
            "facts": state.get("facts", {}),
            "thesis": state.get("thesis", {}),
            "critique": state.get("critique", {}),
        }
        data, err = self._ask(JUDGE_PROMPT, payload)
        if err:
            return {"verdict": {"side": "FLAT", "confidence": 0.0, "size_hint": 0.0,
                                "veto": True, "rationale": f"judge unavailable: {err}"},
                    "errors": state.get("errors", []) + [err]}
        return {"verdict": {
            "side": str(data.get("side", "FLAT")).upper(),
            "confidence": _clamp(data.get("confidence"), 0.0, 1.0),
            "size_hint": _clamp(data.get("size_hint"), 0.0, 1.0),
            "veto": bool(data.get("veto", False)),
            "rationale": str(data.get("rationale", ""))[:280],
        }}

    @staticmethod
    def _node_veto(state: ResearchState) -> dict:
        """Terminal node for a fatal critique -- costs nothing."""
        critique = state.get("critique", {}) or {}
        return {"verdict": {
            "side": "FLAT", "confidence": 0.0, "size_hint": 0.0, "veto": True,
            "rationale": "vetoed by critique: " + str(critique.get("objection", ""))[:200],
        }}

    # --- model plumbing ----------------------------------------------------
    def _ask(self, system: str, payload: dict) -> tuple[dict, str | None]:
        """Runs one model call. Returns (parsed, error) -- never raises, so a
        single bad node degrades the decision instead of killing the run."""
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            return {}, "cost guard reached"
        try:
            text = self._invoke(system, json.dumps(payload, default=str))
            self.calls_made += 1
            return _extract_json(text), None
        except Exception as e:
            return {}, str(e)[:200]

    def _invoke(self, system: str, user: str) -> str:
        if self.chat is not None:  # test seam
            return self.chat(system, user)

        from langchain_anthropic import ChatAnthropic
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = ChatAnthropic(model=self.model, max_tokens=MAX_TOKENS)
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content=user)])

        usage = getattr(response, "usage_metadata", None) or {}
        self.in_tokens += usage.get("input_tokens", 0) or 0
        self.out_tokens += usage.get("output_tokens", 0) or 0

        content = response.content
        if isinstance(content, list):  # content blocks
            return "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        return str(content)

    # --- output ------------------------------------------------------------
    def _to_decision(self, signal: Signal, state: dict) -> Decision:
        verdict = state.get("verdict") or {}
        try:
            side = Side(verdict.get("side", "FLAT"))
        except ValueError:
            side = Side.FLAT

        rationale = verdict.get("rationale", "")
        thesis, critique = state.get("thesis") or {}, state.get("critique") or {}
        if thesis.get("argument") and critique.get("objection"):
            # Carry both sides into the audit trail: knowing what the system
            # argued against is what makes a losing trade reviewable later.
            rationale = (f"{rationale} | for: {thesis['argument'][:120]}"
                         f" | against: {critique['objection'][:120]}")

        return Decision(
            ts=signal.ts,
            symbol=signal.symbol,
            side=side,
            confidence=float(verdict.get("confidence", 0.0)),
            size_hint=float(verdict.get("size_hint", 0.0)),
            rationale=rationale[:400],
            veto=bool(verdict.get("veto", False)),
        )

    @staticmethod
    def _flat(signal: Signal, why: str) -> Decision:
        return Decision(ts=signal.ts, symbol=signal.symbol, side=Side.FLAT,
                        confidence=0.0, size_hint=0.0, rationale=why, veto=True)


def graph_available() -> bool:
    """Reports whether the optional dependencies and a key are present."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import langchain_anthropic  # noqa: F401
        import langgraph  # noqa: F401
        return True
    except ImportError:
        return False
