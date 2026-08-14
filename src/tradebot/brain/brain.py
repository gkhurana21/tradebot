"""The brain: turns a Signal + market context into a Decision.

Two modes:
  * LLM mode  — calls the Claude API with a structured prompt and parses a
                JSON decision. Enabled when ANTHROPIC_API_KEY is set and the
                `anthropic` package is installed.
  * Heuristic — a deterministic fallback that mimics the decision shape so the
                full pipeline runs with zero dependencies and zero cost.

The brain NEVER executes. It only proposes. The RiskEngine gates everything.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..core.types import Account, Decision, Side, Signal

MODEL = "claude-opus-4-8"

# (input, output) USD per million tokens. Used only to estimate spend so the
# cost guard can warn before a run burns through a small API balance.
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

# Web-search server-tool version by model family (newer models need the newer tool).
_WEB_SEARCH_NEW = {"claude-opus-4-8", "claude-sonnet-5"}

SYSTEM_PROMPT = """You are the analysis engine of an automated trading system.
You receive a deterministic strategy signal plus market context and must decide
whether to act. You do NOT execute trades — a separate risk engine sizes and
gates every order, and can veto you. Be disciplined and skeptical: most signals
are noise. Respond with ONLY a JSON object, no prose:

{"side": "BUY"|"SELL"|"FLAT",
 "confidence": 0.0-1.0,
 "size_hint": 0.0-1.0,
 "veto": true|false,
 "rationale": "one sentence"}

Guidance: veto or lower confidence when the signal conflicts with the broader
context (e.g. RSI extremes, tiny EMA spread, or an existing opposing position).
"""


@dataclass
class Brain:
    use_llm: bool = True
    model: str = MODEL
    news_aware: bool = False       # let Claude web-search headlines before deciding
    max_web_searches: int = 2
    max_calls: int | None = None   # cost guard: after N API calls, fall back to heuristic
    timeout: float = 20.0          # per-call ceiling, seconds
    # Called after each API call as progress(calls_made, max_calls). Long runs
    # are otherwise indistinguishable from hung ones.
    progress: object = None

    # runtime cost tracking (not constructor args)
    calls_made: int = field(default=0, init=False)
    in_tokens: int = field(default=0, init=False)
    out_tokens: int = field(default=0, init=False)
    _capped_warned: bool = field(default=False, init=False)

    def decide(
        self,
        signal: Signal,
        account: Account,
        price: float,
        extra_context: dict | None = None,
    ) -> Decision:
        capped = self.max_calls is not None and self.calls_made >= self.max_calls
        if self.use_llm and _llm_available() and not capped:
            try:
                return self._decide_llm(signal, account, price, extra_context or {})
            except Exception as e:  # fall back rather than crash the loop
                print(f"[brain] LLM error, using heuristic: {e}")
        if capped and not self._capped_warned:
            self._capped_warned = True
            print(f"[brain] cost guard hit max_calls={self.max_calls}; "
                  f"remaining decisions use the free heuristic")
        return self._decide_heuristic(signal, account, price)

    def est_cost(self) -> float:
        """Estimated USD spent so far from tracked token usage."""
        pin, pout = PRICES.get(self.model, PRICES[MODEL])
        return self.in_tokens / 1e6 * pin + self.out_tokens / 1e6 * pout

    def cost_report(self) -> str:
        return (f"LLM calls: {self.calls_made}  "
                f"tokens in/out: {self.in_tokens:,}/{self.out_tokens:,}  "
                f"est. spend: ${self.est_cost():.4f} ({self.model})")

    # --- LLM path ----------------------------------------------------------
    def _decide_llm(
        self, signal: Signal, account: Account, price: float, extra: dict
    ) -> Decision:
        import anthropic

        # A backtest makes hundreds of these. The SDK's 10-minute default means
        # a single stalled response blocks the whole run, so bound it tightly:
        # a decision that takes 20s is already useless, and the heuristic
        # fallback is better than a hang.
        client = anthropic.Anthropic(timeout=self.timeout, max_retries=1)
        pos = account.position(signal.symbol)
        context = {
            "signal_side": signal.side.value,
            "signal_strength": signal.strength,
            "signal_reason": signal.reason,
            "features": signal.features,
            "price": price,
            "equity": round(account.equity, 2),
            "current_position_qty": round(pos.qty, 8),
        }
        if extra:
            context["market_context"] = extra   # on-chain, sentiment, etc.

        kwargs = dict(
            model=self.model,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(context)}],
        )
        if self.news_aware:
            # Server-side web search: Claude pulls fresh headlines itself.
            tool_type = ("web_search_20260209" if self.model in _WEB_SEARCH_NEW
                         else "web_search_20250305")
            kwargs["tools"] = [{
                "type": tool_type,
                "name": "web_search",
                "max_uses": self.max_web_searches,
            }]
            kwargs["messages"][0]["content"] += (
                f"\n\nBefore deciding, search for the latest news on "
                f"{signal.symbol} and overall crypto market sentiment."
            )
        msg = client.messages.create(**kwargs)
        # Track usage for the cost guard.
        self.calls_made += 1
        usage = getattr(msg, "usage", None)
        if usage is not None:
            self.in_tokens += getattr(usage, "input_tokens", 0) or 0
            self.out_tokens += getattr(usage, "output_tokens", 0) or 0
        if callable(self.progress):
            self.progress(self.calls_made, self.max_calls)

        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = _extract_json(text)
        return Decision(
            ts=signal.ts,
            symbol=signal.symbol,
            side=Side(data.get("side", "FLAT")),
            confidence=float(data.get("confidence", 0.0)),
            size_hint=float(data.get("size_hint", 0.0)),
            rationale=str(data.get("rationale", ""))[:280],
            veto=bool(data.get("veto", False)),
        )

    # --- Heuristic path (offline, deterministic) ---------------------------
    def _decide_heuristic(self, signal: Signal, account: Account, price: float) -> Decision:
        feats = signal.features
        conf = max(signal.strength, 0.5)   # start from strategy conviction
        veto = False
        rationale = signal.reason

        # Skepticism rules — only apply a check when its feature is present.
        if "spread_pct" in feats and abs(feats["spread_pct"]) < 0.05:
            conf *= 0.6  # EMA crossover too shallow to trust
            rationale = "shallow EMA spread; trimming conviction"
        if "rsi" in feats:
            rsi = feats["rsi"]
            if signal.side == Side.BUY and rsi > 68:
                veto, rationale = True, "buy signal but RSI overbought; stand down"
            if signal.side == Side.SELL and rsi < 32:
                veto, rationale = True, "sell signal but RSI oversold; stand down"
        # Ensemble agreement is a quality boost.
        if feats.get("agree", 0) >= 2:
            conf = min(0.99, conf + 0.1)

        # Don't add to an existing opposing position beyond a flip.
        pos = account.position(signal.symbol)
        size_hint = min(1.0, 0.4 + signal.strength * 0.6)
        if not pos.is_flat and ((pos.qty > 0) == (signal.side == Side.SELL)):
            size_hint = 1.0  # allow a clean flip

        return Decision(
            ts=signal.ts,
            symbol=signal.symbol,
            side=signal.side,
            confidence=round(min(conf, 0.99), 3),
            size_hint=round(size_hint, 3),
            rationale=rationale,
            veto=veto,
        )


def _llm_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in model output: {text!r}")
    return json.loads(text[start : end + 1])
