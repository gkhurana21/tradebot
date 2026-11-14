"""Does Claude beat a coin flip? The offline half and the paid half, kept apart.

The pipeline, in the order it is run:

  1. build_prompts   offline, free. Runs the portfolio backtest on the snapshot
                     with a recording brain. Prompt v2 is account-free, so every
                     prompt is a function of (symbol, ts, signal, price) alone and
                     the whole set is known before anything is sent.
  2. cost_bound      offline, free. An exact upper bound on the batch's price.
                     The tool refuses to submit when the bound is above the cap.
  3. submit / collect  the only paid steps. One Message Batch (50% off), through a
                     small client interface so tests use a fake. Every request and
                     all four usage fields go into a ledger.
  4. ReplayBrain     reads the decision cache only. A miss or a signal mismatch
                     raises, so the null runs cannot cost anything.
  5. run_claude_test the pre-registered statistics, all offline.

Stdlib only. `anthropic` is imported lazily, and only by AnthropicBatchClient.
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Callable, Iterable, Protocol

from ..brain.brain import (
    MAX_TOKENS, PROMPT_HASH, PROMPT_VERSION, SYSTEM_PROMPT, THINKING_CANNOT_BE_DISABLED,
    Brain, build_user_prompt, parse_decision, price_for, thinking_param, usage_cost)
from ..core.risk import RiskConfig
from ..core.strategy import Strategy
from ..core.types import Candle, Decision, Side, Signal
from .nulltest import NullTestResult, _run, run_nulltest, run_twins

ALPHA = 0.025                 # per co-primary test (Bonferroni over two)
INVALID_FALLBACK_RATE = 0.02  # run is invalid above this share of fallbacks
DEFAULT_CAP_USD = Decimal("1.00")
DEFAULT_PILOT = 50
BATCH_DISCOUNT = Decimal("0.5")
# Messages carry a few framing tokens beyond the raw text. The bound adds a fixed
# allowance per request so "1 token per UTF-8 byte" stays a true upper bound.
REQUEST_OVERHEAD_TOKENS = 32
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")


class CapExceeded(RuntimeError):
    """The cost bound is above the cap. Nothing was sent."""


class CacheMiss(KeyError):
    """The replay brain was asked for a decision that is not in the cache."""


class CacheMismatch(RuntimeError):
    """A cached decision belongs to a different signal: the cache is stale."""


class InvalidRun(RuntimeError):
    """Too many fallbacks (or an incomplete cache): no verdict may be drawn."""


def require_testable(model: str) -> None:
    price_for(model)   # raises UnknownModelError
    if model in THINKING_CANNOT_BE_DISABLED:
        raise ValueError(
            f"{model} cannot turn thinking off, and thinking tokens would eat the "
            f"{MAX_TOKENS}-token budget; use claude-haiku-5-5 or claude-sonnet-5-5")


# --- keys and fingerprints ----------------------------------------------------
def ts_key(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def signal_fingerprint(signal: Signal, price: float) -> str:
    """Identifies exactly what the model was shown (side, strength, reason,
    features, price). A cached decision is only valid for the same fingerprint."""
    blob = json.dumps({"side": signal.side.value, "strength": signal.strength,
                       "reason": signal.reason, "features": signal.features,
                       "price": price}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def custom_id_for(model: str, version: str, symbol: str, ts: str) -> str:
    return "c" + hashlib.sha256(f"{model}|{version}|{symbol}|{ts}".encode()).hexdigest()[:40]


# --- 1. offline prompt build ----------------------------------------------------
@dataclass
class PromptRecord:
    custom_id: str
    model: str
    prompt_version: str
    prompt_hash: str
    symbol: str
    ts: str
    fp: str
    signal_side: str
    prompt: str

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.model, self.prompt_version, self.symbol, self.ts)


class RecordingBrain:
    """Captures every decide() call and returns the heuristic decision, so the
    backtest path continues. Prompts do not depend on that path: decide() runs
    before the risk engine's halt check."""

    def __init__(self, model: str):
        self.model = model
        self.records: list[PromptRecord] = []
        self._inner = Brain(use_llm=False)

    def decide(self, signal, account, price, extra=None) -> Decision:
        ts = ts_key(signal.ts)
        self.records.append(PromptRecord(
            custom_id=custom_id_for(self.model, PROMPT_VERSION, signal.symbol, ts),
            model=self.model, prompt_version=PROMPT_VERSION, prompt_hash=PROMPT_HASH,
            symbol=signal.symbol, ts=ts, fp=signal_fingerprint(signal, price),
            signal_side=signal.side.value, prompt=build_user_prompt(signal, price, extra)))
        return self._inner.decide(signal, account, price, extra)


def build_prompts(streams: dict[str, list[Candle]], strategy_factory: Callable[[], Strategy],
                  model: str, risk_cfg: RiskConfig | None = None,
                  cash: float = 25_000.0) -> list[PromptRecord]:
    require_testable(model)
    risk_cfg = risk_cfg or RiskConfig(max_open_positions=max(3, len(streams)))
    rec = RecordingBrain(model)
    _run(streams, strategy_factory, rec, risk_cfg, cash)
    seen = set()
    for r in rec.records:
        if r.key in seen:
            raise ValueError(f"duplicate prompt key {r.key}")
        seen.add(r.key)
        low = r.prompt.lower()
        if "equity" in low or "position" in low:
            raise ValueError(f"prompt for {r.key} mentions account state")
    return rec.records


def write_prompts(records: list[PromptRecord], path: str) -> str:
    _ensure_dir(path)
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r.__dict__, sort_keys=True) + "\n")
    return file_sha256(path)


def read_prompts(path: str) -> list[PromptRecord]:
    with open(path) as f:
        return [PromptRecord(**json.loads(line)) for line in f if line.strip()]


# --- 2. exact cost bound ----------------------------------------------------------
@dataclass(frozen=True)
class CostBound:
    model: str
    n_requests: int
    input_tokens: int       # upper bound
    output_tokens: int      # upper bound: n x max_tokens
    list_usd: Decimal
    batch_usd: Decimal      # 50% of list: the number compared with the cap

    def summary(self) -> str:
        return (f"{self.n_requests} requests · input <= {self.input_tokens:,} tokens "
                f"(1 per UTF-8 byte + {REQUEST_OVERHEAD_TOKENS}/request) · output <= "
                f"{self.output_tokens:,} tokens ({MAX_TOKENS}/request) · {self.model} · "
                f"list ${self.list_usd:.6f} · batch (50%) bound ${self.batch_usd:.6f}")


def cost_bound(prompts: Iterable[PromptRecord], model: str,
               max_tokens: int = MAX_TOKENS) -> CostBound:
    pin, pout = price_for(model)
    system_bytes = len(SYSTEM_PROMPT.encode("utf-8"))
    n = in_tokens = 0
    for p in prompts:
        n += 1
        in_tokens += system_bytes + len(p.prompt.encode("utf-8")) + REQUEST_OVERHEAD_TOKENS
    out_tokens = n * max_tokens
    list_usd = (Decimal(in_tokens) * Decimal(str(pin))
                + Decimal(out_tokens) * Decimal(str(pout))) / Decimal(1_000_000)
    return CostBound(model, n, in_tokens, out_tokens, list_usd, list_usd * BATCH_DISCOUNT)


def enforce_cap(bound: CostBound, cap: Decimal | float | str = DEFAULT_CAP_USD,
                already_spent: Decimal | float = 0) -> None:
    cap = Decimal(str(cap))
    total = bound.batch_usd + Decimal(str(already_spent))
    if total > cap:
        raise CapExceeded(f"cost bound ${total:.6f} (batch ${bound.batch_usd:.6f} + already "
                          f"spent ${Decimal(str(already_spent)):.6f}) exceeds the cap ${cap}; "
                          f"nothing was sent")


# --- batch client interface -----------------------------------------------------------
class BatchClient(Protocol):
    def submit(self, requests: list[dict]) -> str:
        """Create one batch; returns its id."""
    def poll(self, batch_id: str) -> str:
        """'in_progress' | 'ended' (| 'canceling')."""
    def results(self, batch_id: str) -> Iterable[dict]:
        """Items: {custom_id, type: succeeded|errored|expired|canceled, text,
        stop_reason, usage: {four fields} | None, request_id, error}."""


def make_request(p: PromptRecord) -> dict:
    params = {"model": p.model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT,
              "messages": [{"role": "user", "content": p.prompt}]}
    thinking = thinking_param(p.model)
    if thinking is not None:
        params["thinking"] = thinking
    return {"custom_id": p.custom_id, "params": params}


class AnthropicBatchClient:
    """The real Message Batches client. Paid path; never constructed by tests.
    The SDK reads the API key from the environment itself."""

    def __init__(self):
        import anthropic
        self._c = anthropic.Anthropic(max_retries=0)

    def submit(self, requests: list[dict]) -> str:
        return self._c.messages.batches.create(requests=requests).id

    def poll(self, batch_id: str) -> str:
        return self._c.messages.batches.retrieve(batch_id).processing_status

    def results(self, batch_id: str) -> Iterable[dict]:
        for item in self._c.messages.batches.results(batch_id):
            res = item.result
            out = {"custom_id": item.custom_id, "type": res.type, "text": None,
                   "stop_reason": None, "usage": None, "request_id": None, "error": None}
            if res.type == "succeeded":
                msg = res.message
                out["text"] = "".join(b.text for b in msg.content
                                      if getattr(b, "type", "") == "text")
                out["stop_reason"] = msg.stop_reason
                out["request_id"] = getattr(msg, "_request_id", None)
                u = msg.usage
                out["usage"] = {k: getattr(u, k, 0) or 0 for k in USAGE_FIELDS}
            elif res.type == "errored":
                out["error"] = str(getattr(res, "error", ""))
            yield out


# --- ledger and cache files --------------------------------------------------------------
def ledger_path_for(cache_path: str) -> str:
    return cache_path + ".ledger.json"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ensure_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)


class Ledger:
    """Every paid request, all four usage fields, and the computed spend."""

    def __init__(self, path: str, model: str):
        self.path = path
        self.data = {"model": model, "prompt_version": PROMPT_VERSION,
                     "prompt_hash": PROMPT_HASH, "batches": [], "requests": [],
                     "spend_usd": 0.0}
        if os.path.exists(path):
            with open(path) as f:
                self.data = json.load(f)
            if self.data["model"] != model:
                raise ValueError(f"ledger {path} is for {self.data['model']}, not {model}")

    @property
    def spend(self) -> Decimal:
        return Decimal(str(self.data["spend_usd"]))

    @property
    def batches(self) -> list[dict]:
        return self.data["batches"]

    def add_request(self, entry: dict) -> None:
        self.data["requests"].append(entry)
        total = sum(Decimal(str(r["spend_usd"])) for r in self.data["requests"])
        self.data["spend_usd"] = float(total)

    def save(self) -> None:
        _ensure_dir(self.path)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)


@lru_cache(maxsize=4)
def _load_cache_cached(path: str, mtime_ns: int, size: int) -> dict:
    out = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[(r["model"], r["prompt_version"], r["symbol"], r["ts"])] = r
    return out


def load_cache(path: str) -> dict:
    st = os.stat(path)
    return _load_cache_cached(os.path.abspath(path), st.st_mtime_ns, st.st_size)


def write_cache(records: Iterable[dict], path: str) -> str:
    """Deterministic file (sorted), so its sha256 identifies the experiment record."""
    _ensure_dir(path)
    rows = sorted(records, key=lambda r: (r["ts"], r["symbol"], r["model"], r["prompt_version"]))
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    digest = file_sha256(path)
    with open(path + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(path)}\n")
    return digest


def decision_record(p: PromptRecord, item: dict | None) -> dict:
    """One cache row from one batch result. Anything but a clean, parseable
    reply becomes FLAT with source=fallback:<why>."""
    why = None
    dec = None
    if item is None:
        why = "missing"
    elif item["type"] != "succeeded":
        why = item["type"]                       # errored | expired | canceled
    elif item.get("stop_reason") in ("refusal", "max_tokens"):
        why = item["stop_reason"]
    else:
        try:
            ts = datetime.fromisoformat(p.ts)
            dec = parse_decision(item.get("text") or "",
                                 Signal(ts, p.symbol, Side(p.signal_side), 0.0))
        except Exception:
            why = "parse"
    if dec is None:
        side, conf, size, veto, rat = "FLAT", 0.0, 0.0, True, f"fallback: {why}"
    else:
        side, conf, size, veto, rat = dec.side.value, dec.confidence, dec.size_hint, dec.veto, dec.rationale
    usage = (item or {}).get("usage") or {}
    return {
        "model": p.model, "prompt_version": p.prompt_version, "prompt_hash": p.prompt_hash,
        "symbol": p.symbol, "ts": p.ts, "fp": p.fp, "signal_side": p.signal_side,
        "side": side, "confidence": conf, "size_hint": size, "veto": veto, "rationale": rat,
        "source": "claude" if why is None else f"fallback:{why}",
        "text": (item or {}).get("text"), "stop_reason": (item or {}).get("stop_reason"),
        "usage": {k: int(usage.get(k, 0) or 0) for k in USAGE_FIELDS},
        "request_id": (item or {}).get("request_id"), "custom_id": p.custom_id,
    }


# --- 3. submit and collect ------------------------------------------------------------------
@dataclass
class SubmitResult:
    batch_id: str
    n_requests: int
    bound: CostBound
    pilot: bool


def pending_prompts(prompts: list[PromptRecord], cache_path: str) -> list[PromptRecord]:
    have = load_cache(cache_path) if os.path.exists(cache_path) else {}
    return [p for p in prompts if p.key not in have]


def submit_batch(prompts: list[PromptRecord], cache_path: str, client: BatchClient,
                 model: str, cap: Decimal | float | str = DEFAULT_CAP_USD,
                 pilot: int | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> SubmitResult:
    """Submit the pilot (first N pending prompts) or the whole pending set.

    Refuses when spent + bound would exceed the cap, when any prompt was built
    from a different prompt text, when an earlier batch is still uncollected,
    or when the full batch has already been submitted ("one run, no reruns").
    """
    require_testable(model)
    for p in prompts:
        if p.model != model or p.prompt_hash != PROMPT_HASH or p.prompt_version != PROMPT_VERSION:
            raise ValueError(f"prompt {p.key} was built for a different model or prompt text; "
                             f"rebuild with --build-prompts")
    ledger = Ledger(ledger_path_for(cache_path), model)
    if any(not b.get("collected") for b in ledger.batches):
        raise RuntimeError("an earlier batch is not collected yet; run --collect first")
    if any(not b["pilot"] for b in ledger.batches):
        raise RuntimeError("the full batch was already submitted: one run, no reruns")
    todo = pending_prompts(prompts, cache_path)
    if pilot is not None:
        todo = todo[:pilot]
    if not todo:
        raise RuntimeError("nothing to submit: every prompt is already in the cache")
    bound = cost_bound(todo, model)
    enforce_cap(bound, cap, ledger.spend)
    batch_id = client.submit([make_request(p) for p in todo])
    ledger.batches.append({"batch_id": batch_id, "submitted_at": now().isoformat(),
                           "n_requests": len(todo), "pilot": pilot is not None,
                           "bound_usd": float(bound.batch_usd), "collected": False,
                           "custom_ids": [p.custom_id for p in todo]})
    ledger.save()
    return SubmitResult(batch_id, len(todo), bound, pilot is not None)


@dataclass
class CollectResult:
    status: str                  # 'collected' | 'in_progress' | 'nothing'
    n_results: int = 0
    spend_usd: Decimal = Decimal(0)
    cache_sha256: str = ""
    rates: dict = field(default_factory=dict)


def collect(prompts: list[PromptRecord], cache_path: str, client: BatchClient, model: str,
            wait: bool = True, poll_seconds: float = 30.0,
            sleep: Callable[[float], None] = time.sleep) -> CollectResult:
    ledger = Ledger(ledger_path_for(cache_path), model)
    open_batches = [b for b in ledger.batches if not b.get("collected")]
    if not open_batches:
        return CollectResult("nothing", spend_usd=ledger.spend)
    by_id = {p.custom_id: p for p in prompts}
    cache = dict(load_cache(cache_path)) if os.path.exists(cache_path) else {}
    n = 0
    for b in open_batches:
        while client.poll(b["batch_id"]) != "ended":
            if not wait:
                ledger.save()
                return CollectResult("in_progress", spend_usd=ledger.spend)
            sleep(poll_seconds)
        items = {it["custom_id"]: it for it in client.results(b["batch_id"])}
        for cid in b["custom_ids"]:
            p = by_id[cid]
            item = items.get(cid)
            rec = decision_record(p, item)
            cache[p.key] = rec
            billed = item is not None and item["type"] == "succeeded"
            usage = rec["usage"] if billed else {k: 0 for k in USAGE_FIELDS}
            ledger.add_request({
                "custom_id": cid, "batch_id": b["batch_id"], "symbol": p.symbol, "ts": p.ts,
                "result_type": item["type"] if item else "missing", "source": rec["source"],
                "stop_reason": rec["stop_reason"], "request_id": rec["request_id"],
                "usage": usage,
                # Errored, expired and canceled requests are not billed.
                "spend_usd": usage_cost(model, **usage, batch=True) if billed else 0.0})
            n += 1
        b["collected"] = True
    ledger.save()
    sha = write_cache(cache.values(), cache_path)
    _load_cache_cached.cache_clear()
    return CollectResult("collected", n, ledger.spend, sha, decision_rates(cache.values()))


# --- 4. replay brain and twins ------------------------------------------------------------------
class ReplayBrain:
    """Reads the decision cache and nothing else. A missing key or a signal that
    differs from the one the model saw raises: it cannot call the API."""

    api_calls = 0

    def __init__(self, cache_path: str, model: str, prompt_version: str = PROMPT_VERSION):
        self.model, self.prompt_version = model, prompt_version
        self.cache = load_cache(cache_path)
        self.hits = 0
        stale = {r["prompt_hash"] for r in self.cache.values()} - {PROMPT_HASH}
        if stale:
            raise CacheMismatch(f"cache holds decisions for another prompt text: {sorted(stale)}")

    def decide(self, signal, account, price, extra=None) -> Decision:
        key = (self.model, self.prompt_version, signal.symbol, ts_key(signal.ts))
        rec = self.cache.get(key)
        if rec is None:
            raise CacheMiss(f"no cached decision for {key}")
        if rec["fp"] != signal_fingerprint(signal, price):
            raise CacheMismatch(f"signal at {key} differs from the one that was sent to the model; "
                                f"the strategy or data changed, the cache is stale")
        self.hits += 1
        return Decision(signal.ts, signal.symbol, Side(rec["side"]), rec["confidence"],
                        rec["size_hint"], rec["rationale"], rec["veto"])


@dataclass(frozen=True)
class ReplayFactory:
    cache_path: str
    model: str

    def __call__(self) -> ReplayBrain:
        return ReplayBrain(self.cache_path, self.model)


def _uniform(seed: int, symbol: str, ts: str) -> float:
    d = hashlib.sha256(f"{seed}|{symbol}|{ts}|veto-flip".encode()).digest()
    return int.from_bytes(d[:8], "big") / 2**64


class RandomVetoFlipBrain:
    """A twin that follows the strategy's signal, then vetoes or flips it at
    random at fixed rates. The draw depends only on (seed, symbol, ts), so it is
    reproducible and path-independent. Confidence and size come from the
    heuristic brain with its own vetoes cleared, so the twin's veto rate versus
    the signal is exactly the rate it was given."""

    def __init__(self, veto_rate: float, flip_rate: float, seed: int):
        self.veto_rate, self.flip_rate, self.seed = veto_rate, flip_rate, seed
        self.inner = Brain(use_llm=False)

    def decide(self, signal, account, price, extra=None) -> Decision:
        d = self.inner.decide(signal, account, price, extra)
        if d.side not in (Side.BUY, Side.SELL):
            return d
        u = _uniform(self.seed, d.symbol, ts_key(d.ts))
        if u < self.veto_rate:
            return Decision(d.ts, d.symbol, d.side, d.confidence, d.size_hint,
                            "null: random veto", True)
        if u < self.veto_rate + self.flip_rate:
            flipped = Side.SELL if d.side == Side.BUY else Side.BUY
            return Decision(d.ts, d.symbol, flipped, d.confidence, d.size_hint,
                            "null: random flip", False)
        return Decision(d.ts, d.symbol, d.side, d.confidence, d.size_hint,
                        "null: follow signal", False)


@dataclass(frozen=True)
class TwinFactory:
    veto_rate: float
    flip_rate: float

    def __call__(self, seed: int) -> RandomVetoFlipBrain:
        return RandomVetoFlipBrain(self.veto_rate, self.flip_rate, seed)


# --- 5. rates, validity and the pre-registered tests ------------------------------------------------
def decision_rates(records: Iterable[dict]) -> dict:
    """How Claude's decisions relate to the signal it was shown.

    agree: same side, not vetoed. veto: vetoed or FLAT (fallbacks count here:
    that is how the run traded them). flip: opposite side, not vetoed."""
    n = agree = veto = flip = fallback = 0
    for r in records:
        n += 1
        fallback += r["source"] != "claude"
        if r["veto"] or r["side"] == "FLAT":
            veto += 1
        elif r["side"] == r["signal_side"]:
            agree += 1
        else:
            flip += 1
    div = n or 1
    return {"n": n, "agree": agree, "veto": veto, "flip": flip, "fallback": fallback,
            "agreement_rate": agree / div, "veto_rate": veto / div, "flip_rate": flip / div,
            "fallback_rate": fallback / div}


def run_is_valid(rates: dict) -> bool:
    return rates["n"] > 0 and rates["fallback_rate"] <= INVALID_FALLBACK_RATE


def rates_report(rates: dict) -> str:
    return (f"Agreement with signal : {rates['agreement_rate']:.1%}  "
            f"({rates['agree']}/{rates['n']})\n"
            f"Veto / FLAT rate      : {rates['veto_rate']:.1%}  ({rates['veto']})\n"
            f"Flip rate             : {rates['flip_rate']:.1%}  ({rates['flip']})\n"
            f"Fallbacks             : {rates['fallback']}/{rates['n']} = "
            f"{rates['fallback_rate']:.2%}  (invalid above {INVALID_FALLBACK_RATE:.0%})")


@dataclass
class ClaudeTestResult:
    rates: dict
    direction: NullTestResult       # (a) sign-randomised twins
    veto_flip: NullTestResult       # (b) random veto/flip twins
    heuristic: NullTestResult       # descriptive, own null, same window
    model: str
    cache_sha256: str
    zero_costs: bool = False

    def summary(self) -> str:
        d, v, h = self.direction, self.veto_flip, self.heuristic

        def verdict(res):
            return ("beats" if res.p_value < ALPHA else "does not beat")

        return "\n".join([
            f"Data           : {', '.join(d.symbols)} · {d.bars} bars · {d.start} → {d.end}",
            f"Model / cache  : {self.model} · sha256 {self.cache_sha256}",
            "",
            "1. Agreement with the strategy's signal (reported first)",
            rates_report(self.rates),
            "",
            f"2. Co-primary tests, one-sided, alpha = {ALPHA} each, total return "
            + ("BEFORE costs (fees and slippage set to 0; exploratory)" if self.zero_costs
               else "after costs"),
            f"   Claude        : {d.real_return_pct:+.2f}% · {d.real_trades} round trips · "
            f"{d.real_win_rate_pct:.1f}% win rate",
            f"   (a) sign-randomised twins (K={d.seeds}): median "
            f"{statistics.median(d.null_returns_pct):+.2f}% · 5th-95th "
            f"{d.null_quantile(0.05):+.2f}% to {d.null_quantile(0.95):+.2f}% · p = {d.p_value:.3f}"
            f" -> Claude {verdict(d)} the coin flip",
            f"   (b) random veto/flip twins (K={v.seeds}) at veto {self.rates['veto_rate']:.1%}"
            f" / flip {self.rates['flip_rate']:.1%}: median "
            f"{statistics.median(v.null_returns_pct):+.2f}% · 5th-95th "
            f"{v.null_quantile(0.05):+.2f}% to {v.null_quantile(0.95):+.2f}% · p = {v.p_value:.3f}"
            f" -> Claude {verdict(v)} the random veto/flip twin",
            "",
            "3. Descriptive: the heuristic brain with its own coin-flip null, same window",
            h.summary(),
            "",
            "Claude's raw return is never compared with the heuristic's raw return: they trade "
            "different amounts and pay different fees.",
        ])


def slice_streams(streams: dict[str, list[Candle]], start: datetime | None,
                  end: datetime | None) -> dict[str, list[Candle]]:
    """Candles with start <= ts < end (end exclusive, as in `tradebot snapshot`)."""
    def ok(c):
        return (start is None or c.ts >= start) and (end is None or c.ts < end)
    out = {s: [c for c in cs if ok(c)] for s, cs in streams.items()}
    empty = [s for s, cs in out.items() if not cs]
    if empty:
        raise ValueError(f"no candles in the window for {', '.join(empty)}")
    return out


def run_claude_test(streams: dict[str, list[Candle]], strategy_factory: Callable[[], Strategy],
                    cache_path: str, model: str, seeds: int = 199, cash: float = 25_000.0,
                    workers: int | None = None, zero_costs: bool = False) -> ClaudeTestResult:
    require_testable(model)
    rows = [r for r in load_cache(cache_path).values()
            if r["model"] == model and r["prompt_version"] == PROMPT_VERSION]
    rates = decision_rates(rows)
    if not run_is_valid(rates):
        raise InvalidRun(f"INVALID RUN: fallback rate {rates['fallback_rate']:.2%} exceeds "
                         f"{INVALID_FALLBACK_RATE:.0%} (or the cache is empty); no verdict.\n"
                         + rates_report(rates))
    factory = ReplayFactory(cache_path, model)
    direction = run_nulltest(streams, strategy_factory, brain_factory=factory,
                             cash=cash, seeds=seeds, workers=workers, zero_costs=zero_costs)
    veto_flip = run_twins(streams, strategy_factory, factory,
                          TwinFactory(rates["veto_rate"], rates["flip_rate"]),
                          cash=cash, seeds=seeds, workers=workers, zero_costs=zero_costs)
    heuristic = run_nulltest(streams, strategy_factory, cash=cash, seeds=seeds, workers=workers,
                             zero_costs=zero_costs)
    return ClaudeTestResult(rates, direction, veto_flip, heuristic, model, file_sha256(cache_path),
                            zero_costs)


# --- pre-registration template ------------------------------------------------------------------------
def fill_preregistration(path: str, model: str, prompts: list[PromptRecord],
                         prompts_sha256: str, window: str, cap: Decimal | float | str) -> int:
    """Replaces {{PLACEHOLDER}} tokens in the pre-registration doc in place.
    Returns how many placeholders were replaced."""
    bound = cost_bound(prompts, model)
    values = {
        "PROMPT_HASH": PROMPT_HASH, "PROMPT_VERSION": PROMPT_VERSION, "MODEL_ID": model,
        "MAX_TOKENS": str(MAX_TOKENS), "N_PROMPTS": str(len(prompts)),
        "PROMPTS_SHA256": prompts_sha256, "COST_BOUND_USD": f"{bound.batch_usd:.6f}",
        "CAP_USD": str(Decimal(str(cap))), "WINDOW": window,
    }
    with open(path) as f:
        text = f.read()
    n = 0
    for k, v in values.items():
        token = "{{" + k + "}}"
        n += text.count(token)
        text = text.replace(token, v)
    with open(path, "w") as f:
        f.write(text)
    return n
