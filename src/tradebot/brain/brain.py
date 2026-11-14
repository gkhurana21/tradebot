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

import hashlib
import json
import os
from dataclasses import dataclass, field

from ..core.types import Account, Decision, Side, Signal

MODEL = "claude-opus-5-5"

# (input, output) USD per million tokens at list price, prompts <= 100k tokens.
# Used to estimate spend so the cost guard can warn before a run burns through a
# small API balance. Prices as of 2026-11-14 (platform.claude.com pricing page).
# claude-opus-5-5 comes from the research notes' pricing table
# (notes/claude-cost.md) and was not independently re-checked.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Cache-read price, USD per MTok (notes table). Cache writes are priced at 1.25x
# the input rate (the standard 5-minute write premium; an assumption, the notes
# list no write price for most models). Nothing here uses prompt caching, but
# spend accounting reads all four usage fields so the total stays complete.
CACHE_READ_PRICES = {
    "claude-haiku-5-5": 0.01, "claude-sonnet-5-5": 0.10, "claude-opus-5-5": 0.20,
    "claude-opus-4-8": 0.50, "claude-sonnet-5": 0.20, "claude-haiku-4-5": 0.10,
}
CACHE_WRITE_MULTIPLIER = 1.25

# Web-search server-tool version by model family (newer models need the newer tool).
_WEB_SEARCH_NEW = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-4-8", "claude-sonnet-5"}

# Thinking is on by default for the 5.5 family and its tokens count toward
# max_tokens, so a 200-token budget can be eaten before any JSON is written.
# Haiku 5.5 accepts {"type": "disabled"}; Sonnet 5.5 rejects "disabled" and takes
# "between_tools" (from the notes, unverified); Opus 5.5 cannot turn thinking off
# at all. Older models think only when asked.
_THINKING = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}
THINKING_CANNOT_BE_DISABLED = {"claude-opus-5-5"}

MAX_TOKENS = 200


class UnknownModelError(ValueError):
    """A model id with no known price. Raised instead of guessing a rate."""


class BrainError(RuntimeError):
    """A strict-mode brain refused to fall back to the heuristic."""


def price_for(model: str) -> tuple[float, float]:
    try:
        return PRICES[model]
    except KeyError:
        raise UnknownModelError(
            f"no price for model {model!r}; known: {', '.join(sorted(PRICES))}") from None


def thinking_param(model: str) -> dict | None:
    return _THINKING.get(model)


def usage_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
               cache_creation_input_tokens: int = 0, cache_read_input_tokens: int = 0,
               batch: bool = False) -> float:
    """USD for one response's four usage fields. Batch is 50% of list."""
    pin, pout = price_for(model)
    pread = CACHE_READ_PRICES[model]
    usd = (input_tokens * pin + output_tokens * pout
           + cache_creation_input_tokens * pin * CACHE_WRITE_MULTIPLIER
           + cache_read_input_tokens * pread) / 1e6
    return usd * 0.5 if batch else usd


# --- Prompt v2: account-free --------------------------------------------------
# The prompt carries only the strategy's signal, its features and the price. No
# equity, no position: a decision is then a pure function of
# (model, prompt, symbol, ts), which is what lets every prompt be built offline
# from candles alone, sent as one batch, and replayed from a cache.
PROMPT_VERSION = "v2"

SYSTEM_PROMPT = """You are the analysis engine of an automated trading system.
You receive a deterministic strategy signal plus market features and must
decide whether to act on it. You do NOT execute trades — a separate risk
engine sizes and gates every order, and can veto you. You are told nothing
about the account. Be disciplined and skeptical: most signals are noise.
Respond with ONLY a JSON object, no prose:

{"side": "BUY"|"SELL"|"FLAT",
 "confidence": 0.0-1.0,
 "size_hint": 0.0-1.0,
 "veto": true|false,
 "rationale": "one sentence"}

Guidance: veto or lower confidence when the signal conflicts with the broader
context (e.g. RSI extremes or a tiny EMA spread).
"""

USER_TEMPLATE = "Signal and market context (JSON):\n{context}"

PROMPT_HASH = hashlib.sha256(
    (SYSTEM_PROMPT + "\n\x00\n" + USER_TEMPLATE).encode("utf-8")).hexdigest()


def build_context(signal: Signal, price: float, extra: dict | None = None) -> dict:
    ctx = {
        "signal_side": signal.side.value,
        "signal_strength": signal.strength,
        "signal_reason": signal.reason,
        "features": signal.features,
        "price": price,
    }
    if extra:
        ctx["market_context"] = extra   # on-chain, sentiment, etc.
    return ctx


def build_user_prompt(signal: Signal, price: float, extra: dict | None = None) -> str:
    return USER_TEMPLATE.format(
        context=json.dumps(build_context(signal, price, extra), sort_keys=True))


def parse_decision(text: str, signal: Signal) -> Decision:
    """Strict parse of a model reply. Raises ValueError on anything malformed."""
    data = _extract_json(text)
    if not isinstance(data, dict):
        raise ValueError(f"model output is not a JSON object: {text!r}")
    try:
        side = Side(data["side"])
        conf, size = data["confidence"], data["size_hint"]
    except KeyError as e:
        raise ValueError(f"model output lacks {e}: {text!r}") from None
    for name, v in (("confidence", conf), ("size_hint", size)):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{name} is not a number: {v!r}")
    veto = data.get("veto", False)
    if not isinstance(veto, bool):
        raise ValueError(f"veto is not a boolean: {veto!r}")
    return Decision(
        ts=signal.ts,
        symbol=signal.symbol,
        side=side,
        confidence=min(1.0, max(0.0, float(conf))),
        size_hint=min(1.0, max(0.0, float(size))),
        rationale=str(data.get("rationale", ""))[:280],
        veto=veto,
    )


@dataclass
class Brain:
    use_llm: bool = True
    model: str = MODEL
    news_aware: bool = False       # let Claude web-search headlines before deciding
    max_web_searches: int = 2
    max_calls: int | None = None   # cost guard: after N API calls, fall back to heuristic
    timeout: float = 20.0          # per-call ceiling, seconds
    # strict: never fall back to the heuristic. Any LLM failure (no key, API
    # error, refusal, truncation, unparseable reply, call cap) raises BrainError,
    # so a run that is "Claude" is never silently part heuristic.
    strict: bool = False
    # Called after each API call as progress(calls_made, max_calls). Long runs
    # are otherwise indistinguishable from hung ones.
    progress: object = None

    # runtime cost tracking (not constructor args)
    calls_made: int = field(default=0, init=False)
    in_tokens: int = field(default=0, init=False)
    out_tokens: int = field(default=0, init=False)
    cache_write_tokens: int = field(default=0, init=False)
    cache_read_tokens: int = field(default=0, init=False)
    _capped_warned: bool = field(default=False, init=False)

    def decide(
        self,
        signal: Signal,
        account: Account,
        price: float,
        extra_context: dict | None = None,
    ) -> Decision:
        capped = self.max_calls is not None and self.calls_made >= self.max_calls
        if self.strict and self.use_llm:
            if capped:
                raise BrainError(f"cost guard hit max_calls={self.max_calls} (strict)")
            if not _llm_available():
                raise BrainError("strict brain needs ANTHROPIC_API_KEY and the "
                                 "`anthropic` package; refusing the heuristic fallback")
            return self._decide_llm(signal, account, price, extra_context or {})
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
        """Estimated USD spent so far from tracked token usage. Raises on an
        unknown model rather than pricing it at some other model's rate."""
        return usage_cost(self.model, self.in_tokens, self.out_tokens,
                          self.cache_write_tokens, self.cache_read_tokens)

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
        # a decision that takes 20s is already useless. Strict mode makes our
        # own retry decisions, so the SDK does none: every attempt is visible.
        client = anthropic.Anthropic(timeout=self.timeout,
                                     max_retries=0 if self.strict else 1)
        # Prompt v2 is account-free: `account` is deliberately unused.
        kwargs = dict(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": build_user_prompt(signal, price, extra)}],
        )
        thinking = thinking_param(self.model)
        if thinking is not None:
            kwargs["thinking"] = thinking
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
        # Track usage for the cost guard (all four fields).
        self.calls_made += 1
        usage = getattr(msg, "usage", None)
        if usage is not None:
            self.in_tokens += getattr(usage, "input_tokens", 0) or 0
            self.out_tokens += getattr(usage, "output_tokens", 0) or 0
            self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
            self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        if callable(self.progress):
            self.progress(self.calls_made, self.max_calls)

        stop = getattr(msg, "stop_reason", None)
        if stop in ("refusal", "max_tokens"):
            raise BrainError(f"model stopped with stop_reason={stop}")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return parse_decision(text, signal)

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
