"""Tests for the Claude coin-flip test machinery. Run: PYTHONPATH=src python3 tests/test_claudetest.py

Everything here uses a fake batch client and a fake `anthropic` module: no
network, no key, no spend. They check the machinery (cost bound, cap, ledger,
fallback rule, cache integrity, replay, rates), not whether Claude is any good.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import types
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from tradebot.brain import brain as brain_mod
from tradebot.brain.brain import (
    MAX_TOKENS, PROMPT_HASH, SYSTEM_PROMPT, Brain, BrainError, UnknownModelError,
    build_user_prompt, price_for, usage_cost)
from tradebot.cli import _MODELS, _ensemble
from tradebot.core.types import Account, Decision, Side, Signal
from tradebot.data import feed
from tradebot.sim import claudetest as ct

MODEL = "claude-haiku-5-5"


def _streams(n=400, seed=3):
    return {"BTC-USD": list(feed.synthetic_candles("BTC-USD", n=n, seed=seed))}


def _prompt(symbol="BTC-USD", ts="2026-01-01T00:00:00+00:00", text="x" * 100):
    return ct.PromptRecord(ct.custom_id_for(MODEL, "v2", symbol, ts), MODEL, "v2", PROMPT_HASH,
                           symbol, ts, "fp", "BUY", text)


def _signal_side(request):
    body = request["params"]["messages"][0]["content"]
    return json.loads(body.split("\n", 1)[1])["signal_side"]


def _ok(side="BUY", conf=0.7, veto=False, **kw):
    text = json.dumps({"side": side, "confidence": conf, "size_hint": 0.5,
                       "veto": veto, "rationale": "r"})
    return {"type": "succeeded", "text": text, "stop_reason": "end_turn",
            "usage": {"input_tokens": 300, "output_tokens": 40,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "request_id": "req_x", "error": None, **kw}


class FakeBatchClient:
    """Answers every request with `script(request) -> item dict (or None to omit)`."""

    def __init__(self, script=None):
        self.script = script or (lambda req: _ok(_signal_side(req)))
        self.submitted: list[list[dict]] = []
        self.polls = 0
        self.ends_after = 1

    def submit(self, requests):
        self.submitted.append(requests)
        return f"batch_{len(self.submitted)}"

    def poll(self, batch_id):
        self.polls += 1
        return "ended" if self.polls >= self.ends_after else "in_progress"

    def results(self, batch_id):
        reqs = self.submitted[int(batch_id.split("_")[1]) - 1]
        for r in reqs:
            item = self.script(r)
            if item is not None:
                yield {"custom_id": r["custom_id"], **item}


def _mixed_script(agree=0.7, veto=0.15):
    """Deterministic per-request mix of agree / veto / flip, by custom_id."""
    def script(req):
        u = int(req["custom_id"][1:9], 16) / 16**8
        side = _signal_side(req)
        if u < agree:
            return _ok(side)
        if u < agree + veto:
            return _ok("FLAT", veto=True)
        return _ok("SELL" if side == "BUY" else "BUY")
    return script


def _collected_cache(tmp, streams=None, script=None, n_max=None):
    """Build prompts for synthetic data, push them through a fake batch, collect."""
    streams = streams or _streams()
    prompts = ct.build_prompts(streams, _ensemble, MODEL)
    if n_max:
        prompts = prompts[:n_max]
    cache = os.path.join(tmp, "decisions.jsonl")
    client = FakeBatchClient(script or _mixed_script())
    ct.submit_batch(prompts, cache, client, MODEL)
    res = ct.collect(prompts, cache, client, MODEL, wait=False)
    assert res.status == "collected", res.status
    return streams, prompts, cache, client, res


# --- prices, models ------------------------------------------------------------
def test_current_models_and_prices():
    assert _MODELS == {"opus": "claude-opus-5-5", "sonnet": "claude-sonnet-5-5",
                       "haiku": "claude-haiku-5-5"}
    assert price_for("claude-haiku-5-5") == (0.10, 0.50)
    assert price_for("claude-sonnet-5-5") == (2.0, 10.0)
    assert price_for("claude-sonnet-5") == (2.0, 10.0)      # was wrongly 3/15
    assert price_for("claude-opus-5-5") == (4.0, 20.0)


def test_unknown_model_raises_instead_of_pricing_at_opus():
    for fn in (lambda: price_for("claude-mystery-9"),
               lambda: usage_cost("claude-mystery-9", 1, 1),
               lambda: ct.cost_bound([_prompt()], "claude-mystery-9"),
               lambda: ct.require_testable("claude-mystery-9"),
               lambda: Brain(use_llm=False, model="claude-mystery-9").est_cost()):
        try:
            fn()
        except UnknownModelError:
            continue
        raise AssertionError("expected UnknownModelError")


def test_opus_55_is_refused_because_thinking_cannot_be_disabled():
    try:
        ct.require_testable("claude-opus-5-5")
    except ValueError as e:
        assert "thinking" in str(e)
    else:
        raise AssertionError("opus 5.5 must be refused for the test")


# --- prompt v2 ---------------------------------------------------------------------
def test_prompt_has_no_equity_or_position():
    sig = Signal(ts=_ts(), symbol="BTC-USD", side=Side.BUY, strength=0.8, reason="r",
                 features={"rsi": 55.0})
    text = (SYSTEM_PROMPT + build_user_prompt(sig, 100.0)).lower()
    assert "equity" not in text and "position" not in text
    for p in ct.build_prompts(_streams(), _ensemble, MODEL):
        assert "equity" not in p.prompt.lower() and "position" not in p.prompt.lower()


def _ts():
    from datetime import datetime, timezone
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_prompt_hash_covers_system_and_template():
    expect = hashlib.sha256((brain_mod.SYSTEM_PROMPT + "\n\x00\n"
                             + brain_mod.USER_TEMPLATE).encode()).hexdigest()
    assert PROMPT_HASH == expect and brain_mod.PROMPT_VERSION == "v2"


def test_prompts_are_independent_of_the_account_path():
    """Two different risk configs give different trades, identical prompts."""
    from tradebot.core.risk import RiskConfig
    a = ct.build_prompts(_streams(), _ensemble, MODEL)
    b = ct.build_prompts(_streams(), _ensemble, MODEL,
                         risk_cfg=RiskConfig(max_open_positions=1, risk_per_trade_pct=0.05))
    assert [(p.key, p.fp, p.prompt) for p in a] == [(p.key, p.fp, p.prompt) for p in b]


# --- the shipped brain on prompt v2 -----------------------------------------------------
class _FakeAnthropic(types.ModuleType):
    def __init__(self, reply):
        super().__init__("anthropic")
        self.calls = []
        outer = self

        class Messages:
            def create(self, **kw):
                outer.calls.append(kw)
                return reply

        class Anthropic:
            def __init__(self, **kw):
                outer.client_kwargs = kw
                self.messages = Messages()

        self.Anthropic = Anthropic


def _reply(text, stop="end_turn"):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop,
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=5,
                                    cache_creation_input_tokens=0, cache_read_input_tokens=0))


class _FakeEnv:
    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        self.old = (sys.modules.get("anthropic"), os.environ.get("ANTHROPIC_API_KEY"))
        sys.modules["anthropic"] = self.fake
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

    def __exit__(self, *a):
        mod, key = self.old
        if mod is None:
            sys.modules.pop("anthropic", None)
        else:
            sys.modules["anthropic"] = mod
        if key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = key


def _sig():
    return Signal(ts=_ts(), symbol="BTC-USD", side=Side.BUY, strength=0.8, reason="r",
                  features={"rsi": 55.0})


def test_single_call_brain_uses_v2_with_thinking_off_and_200_tokens():
    fake = _FakeAnthropic(_reply('{"side":"BUY","confidence":0.6,"size_hint":0.5,'
                                 '"veto":false,"rationale":"ok"}'))
    acct = Account(cash=1000, equity=1000)
    acct.position("BTC-USD").qty = 3.0
    with _FakeEnv(fake):
        d = Brain(model=MODEL).decide(_sig(), acct, 100.0)
    call = fake.calls[0]
    assert d.side == Side.BUY and call["max_tokens"] == MAX_TOKENS == 200
    assert call["thinking"] == {"type": "disabled"}
    assert "temperature" not in call and "top_p" not in call
    assert "equity" not in json.dumps(call).lower() and "position" not in json.dumps(call).lower()


def test_strict_brain_never_falls_back_to_the_heuristic():
    acct = Account(cash=1000, equity=1000)
    for reply in (_reply("no json here"), _reply("{}", stop="refusal"),
                  _reply('{"side":"BUY"', stop="max_tokens")):
        with _FakeEnv(_FakeAnthropic(reply)):
            try:
                Brain(model=MODEL, strict=True).decide(_sig(), acct, 100.0)
            except (BrainError, ValueError):
                pass
            else:
                raise AssertionError("strict brain fell back")
    # Non-strict still degrades to the heuristic, as before.
    with _FakeEnv(_FakeAnthropic(_reply("no json here"))):
        assert Brain(model=MODEL).decide(_sig(), acct, 100.0).side == Side.BUY
    # Strict without a key refuses rather than quietly using the heuristic.
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        Brain(model=MODEL, strict=True).decide(_sig(), acct, 100.0)
    except BrainError:
        pass
    else:
        raise AssertionError("strict brain ran without a key")
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


def test_heuristic_brain_unchanged():
    acct = Account(cash=1000, equity=1000)
    d = Brain(use_llm=False).decide(_sig(), acct, 100.0)
    assert d.side == Side.BUY and not d.veto and d.confidence == 0.8


# --- cost bound and cap ------------------------------------------------------------------
def test_cost_bound_arithmetic():
    prompts = [_prompt(ts=f"2026-01-01T0{i}:00:00+00:00", text="é" * 10 + "a" * 30) for i in range(3)]
    sysb = len(SYSTEM_PROMPT.encode())
    ub = len(prompts[0].prompt.encode())
    assert ub == 20 + 30                                    # é is two UTF-8 bytes
    b = ct.cost_bound(prompts, MODEL)
    in_tok = 3 * (sysb + ub + ct.REQUEST_OVERHEAD_TOKENS)
    assert b.input_tokens == in_tok and b.output_tokens == 3 * 200 and b.n_requests == 3
    list_usd = (Decimal(in_tok) * Decimal("0.1") + Decimal(600) * Decimal("0.5")) / Decimal(10**6)
    assert b.list_usd == list_usd and b.batch_usd == list_usd / 2
    # Sonnet is 20x the input price and 20x the output price.
    assert ct.cost_bound(prompts, "claude-sonnet-5-5").batch_usd == b.batch_usd * 20


def test_cost_bound_really_bounds_a_plausible_run():
    prompts = ct.build_prompts(_streams(), _ensemble, MODEL)
    b = ct.cost_bound(prompts, MODEL)
    # The same requests at 400 input tokens and 120 output tokens each cost far less.
    typical = usage_cost(MODEL, 400 * len(prompts), 120 * len(prompts), batch=True)
    assert 0 < typical < float(b.batch_usd)


def test_refuses_to_submit_over_the_cap_and_sends_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        prompts = ct.build_prompts(_streams(), _ensemble, MODEL)
        client = FakeBatchClient()
        cache = os.path.join(tmp, "d.jsonl")
        bound = ct.cost_bound(prompts, MODEL).batch_usd
        try:
            ct.submit_batch(prompts, cache, client, MODEL, cap=bound - Decimal("0.000001"))
        except ct.CapExceeded:
            pass
        else:
            raise AssertionError("submitted over the cap")
        assert client.submitted == [] and not os.path.exists(ct.ledger_path_for(cache))
        # Exactly at the cap is allowed.
        ct.submit_batch(prompts, cache, client, MODEL, cap=bound)
        assert len(client.submitted) == 1


def test_cap_counts_what_is_already_spent():
    with tempfile.TemporaryDirectory() as tmp:
        _, prompts, cache, client, res = _collected_cache(tmp, n_max=20)
        assert res.spend_usd > 0
        rest = ct.cost_bound(ct.pending_prompts(ct.build_prompts(_streams(), _ensemble, MODEL), cache), MODEL)
        try:
            ct.enforce_cap(rest, rest.batch_usd + res.spend_usd - Decimal("0.0000001"), res.spend_usd)
        except ct.CapExceeded:
            pass
        else:
            raise AssertionError("spent money was ignored")


# --- pilot, ledger, cache -------------------------------------------------------------------
def test_pilot_then_full_batch_then_no_reruns():
    with tempfile.TemporaryDirectory() as tmp:
        prompts = ct.build_prompts(_streams(), _ensemble, MODEL)
        cache = os.path.join(tmp, "d.jsonl")
        client = FakeBatchClient(_mixed_script())
        res = ct.submit_batch(prompts, cache, client, MODEL, pilot=ct.DEFAULT_PILOT)
        assert res.pilot and res.n_requests == 50
        assert [r["custom_id"] for r in client.submitted[0]] == [p.custom_id for p in prompts[:50]]
        try:                                     # an uncollected batch blocks a second submit
            ct.submit_batch(prompts, cache, client, MODEL)
        except RuntimeError as e:
            assert "collect" in str(e)
        else:
            raise AssertionError
        ct.collect(prompts, cache, client, MODEL, wait=False)
        full = ct.submit_batch(prompts, cache, client, MODEL)
        assert not full.pilot and full.n_requests == len(prompts) - 50   # pilot answers reused
        ct.collect(prompts, cache, client, MODEL, wait=False)
        assert len(ct.load_cache(cache)) == len(prompts)
        try:
            ct.submit_batch(prompts, cache, client, MODEL)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a second full run was allowed")


def test_ledger_records_every_request_and_all_four_usage_fields():
    with tempfile.TemporaryDirectory() as tmp:
        usage = {"input_tokens": 1000, "output_tokens": 100,
                 "cache_creation_input_tokens": 80, "cache_read_input_tokens": 400}
        _, prompts, cache, client, res = _collected_cache(
            tmp, n_max=10, script=lambda req: _ok(_signal_side(req), usage=usage))
        ledger = json.load(open(ct.ledger_path_for(cache)))
        assert len(ledger["requests"]) == 10
        r0 = ledger["requests"][0]
        assert set(r0["usage"]) == set(ct.USAGE_FIELDS) and r0["usage"] == usage
        one = usage_cost(MODEL, **usage, batch=True)
        assert abs(r0["spend_usd"] - one) < 1e-12 and one > 0
        assert abs(ledger["spend_usd"] - 10 * one) < 1e-9
        assert abs(float(res.spend_usd) - 10 * one) < 1e-9
        # The cache's sha256 is recorded and matches the file.
        sha = open(cache + ".sha256").read().split()[0]
        assert sha == ct.file_sha256(cache) == res.cache_sha256


def test_collect_waits_for_the_batch_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        prompts = ct.build_prompts(_streams(), _ensemble, MODEL)[:5]
        cache = os.path.join(tmp, "d.jsonl")
        client = FakeBatchClient()
        client.ends_after = 4
        ct.submit_batch(prompts, cache, client, MODEL)
        assert ct.collect(prompts, cache, client, MODEL, wait=False).status == "in_progress"
        slept = []
        res = ct.collect(prompts, cache, client, MODEL, sleep=slept.append)
        assert res.status == "collected" and slept       # polled, slept, then collected


def test_cache_file_is_deterministic():
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        a = _collected_cache(t1, n_max=30)[2]
        b = _collected_cache(t2, n_max=30)[2]
        assert ct.file_sha256(a) == ct.file_sha256(b)


# --- failure rule ---------------------------------------------------------------------------------
def _fallback_sources(items):
    with tempfile.TemporaryDirectory() as tmp:
        it = iter(items)
        _, prompts, cache, _, res = _collected_cache(tmp, n_max=len(items), script=lambda req: next(it))
        return [r["source"] for r in sorted(ct.load_cache(cache).values(), key=lambda r: r["ts"])], res


def test_every_failure_kind_becomes_flat_fallback():
    items = [
        _ok("BUY"),
        {"type": "succeeded", "text": "sorry, no", "stop_reason": "end_turn", "usage": _ok()["usage"]},
        {"type": "succeeded", "text": '{"side":"BUY","confidence":0.5', "stop_reason": "max_tokens",
         "usage": _ok()["usage"]},
        {"type": "succeeded", "text": "", "stop_reason": "refusal", "usage": _ok()["usage"]},
        {"type": "errored", "error": "overloaded", "text": None, "stop_reason": None, "usage": None},
        {"type": "expired", "text": None, "stop_reason": None, "usage": None},
        _ok("MAYBE"),                                   # not a side
        _ok("BUY", conf="high"),                        # not a number
        None,                                           # absent from the results
    ]
    sources, res = _fallback_sources(items)
    assert sources == ["claude", "fallback:parse", "fallback:max_tokens", "fallback:refusal",
                       "fallback:errored", "fallback:expired", "fallback:parse",
                       "fallback:parse", "fallback:missing"], sources
    assert res.rates["fallback"] == 8
    # Errored / expired / missing requests are not billed; refusals and truncations are.
    with tempfile.TemporaryDirectory() as tmp:
        it = iter(items)
        cache = _collected_cache(tmp, n_max=len(items), script=lambda req: next(it))[2]
        led = json.load(open(ct.ledger_path_for(cache)))["requests"]
        billed = [r["spend_usd"] > 0 for r in led]
        assert billed == [True, True, True, True, False, False, True, True, False]


def test_fallback_decision_is_flat_and_vetoed():
    rec = ct.decision_record(_prompt(), {"type": "expired"})
    assert (rec["side"], rec["veto"], rec["confidence"], rec["source"]) == \
           ("FLAT", True, 0.0, "fallback:expired")


def test_two_percent_rule():
    def rates(n_fallback, n=50):
        rows = [{"source": "claude", "veto": False, "side": "BUY", "signal_side": "BUY"}] * (n - n_fallback)
        rows += [{"source": "fallback:parse", "veto": True, "side": "FLAT", "signal_side": "BUY"}] * n_fallback
        return ct.decision_rates(rows)
    assert ct.run_is_valid(rates(1))            # 2.0% exactly: valid
    assert not ct.run_is_valid(rates(2))        # 4.0%: invalid
    assert not ct.run_is_valid(ct.decision_rates([]))


def test_invalid_run_gets_no_verdict():
    with tempfile.TemporaryDirectory() as tmp:
        streams, prompts, cache, _, res = _collected_cache(
            tmp, script=lambda req: {"type": "expired", "text": None, "stop_reason": None, "usage": None})
        assert res.rates["fallback_rate"] == 1.0
        try:
            ct.run_claude_test(streams, _ensemble, cache, MODEL, seeds=3, workers=1)
        except ct.InvalidRun as e:
            assert "INVALID" in str(e)
        else:
            raise AssertionError("an invalid run produced a verdict")


# --- rates -------------------------------------------------------------------------------------------
def test_agreement_rate_computation():
    def row(side, signal, veto=False, source="claude"):
        return {"side": side, "signal_side": signal, "veto": veto, "source": source}
    rows = [row("BUY", "BUY"), row("BUY", "BUY"), row("SELL", "SELL"),   # 3 agree
            row("SELL", "BUY"),                                           # 1 flip
            row("FLAT", "BUY"), row("BUY", "BUY", veto=True),             # 2 veto
            row("FLAT", "SELL", veto=True, source="fallback:parse")]      # fallback counts as veto
    r = ct.decision_rates(rows)
    assert (r["n"], r["agree"], r["flip"], r["veto"], r["fallback"]) == (7, 3, 1, 3, 1)
    assert abs(r["agreement_rate"] - 3 / 7) < 1e-12
    assert abs(r["agreement_rate"] + r["veto_rate"] + r["flip_rate"] - 1) < 1e-12
    assert ct.rates_report(r).startswith("Agreement with signal")      # reported first


# --- cache integrity and replay -------------------------------------------------------------------------
def test_replay_raises_on_a_miss_and_on_a_changed_signal():
    with tempfile.TemporaryDirectory() as tmp:
        streams, prompts, cache, _, _ = _collected_cache(tmp, n_max=5)
        replay = ct.ReplayBrain(cache, MODEL)
        acct = Account(cash=1, equity=1)
        # Rebuild the exact signal behind the first cached decision.
        rec = ct.RecordingBrain(MODEL)
        from tradebot.sim.nulltest import _run
        from tradebot.core.risk import RiskConfig
        seen = []

        class Tap:
            def decide(self, signal, account, price, extra=None):
                seen.append((signal, price))
                return Decision(signal.ts, signal.symbol, signal.side, 0.5, 0.5, "")
        _run(streams, _ensemble, Tap(), RiskConfig(max_open_positions=3), 25_000.0)
        sig, price = next((s, p) for s, p in seen if ct.ts_key(s.ts) == prompts[0].ts)
        assert replay.decide(sig, acct, price).symbol == "BTC-USD"
        # A different price: same key, different fingerprint -> stale cache.
        try:
            replay.decide(sig, acct, price + 1.0)
        except ct.CacheMismatch:
            pass
        else:
            raise AssertionError("fingerprint mismatch was accepted")
        # A signal that was never sent to the model -> miss.
        late = next(s for s, p in reversed(seen) if ct.ts_key(s.ts) not in {q.ts for q in prompts[:5]})
        try:
            replay.decide(late, acct, 1.0)
        except ct.CacheMiss:
            pass
        else:
            raise AssertionError("a miss was answered")
        assert replay.api_calls == 0 and replay.hits == 1


def test_stale_prompt_hash_in_cache_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        cache = _collected_cache(tmp, n_max=3)[2]
        rows = [json.loads(l) for l in open(cache)]
        rows[0]["prompt_hash"] = "0" * 64
        ct.write_cache(rows, cache)
        ct._load_cache_cached.cache_clear()
        try:
            ct.ReplayBrain(cache, MODEL)
        except ct.CacheMismatch:
            pass
        else:
            raise AssertionError("cache from another prompt text was accepted")


def test_replay_run_is_deterministic_and_never_calls_the_api():
    with tempfile.TemporaryDirectory() as tmp:
        streams, prompts, cache, _, res = _collected_cache(tmp)
        assert res.rates["fallback"] == 0 and res.rates["n"] == len(prompts)
        # Any attempt to reach the network would need this module; make that fatal.
        old = sys.modules.get("anthropic")
        sys.modules["anthropic"] = None
        try:
            r1 = ct.run_claude_test(streams, _ensemble, cache, MODEL, seeds=9, workers=1)
            r2 = ct.run_claude_test(streams, _ensemble, cache, MODEL, seeds=9, workers=1)
        finally:
            if old is None:
                sys.modules.pop("anthropic", None)
            else:
                sys.modules["anthropic"] = old
        for f in ("direction", "veto_flip", "heuristic"):
            a, b = getattr(r1, f), getattr(r2, f)
            assert a.null_returns_pct == b.null_returns_pct and a.real_return_pct == b.real_return_pct
            assert a.p_value == b.p_value and a.seeds == 9
        assert r1.direction.real_return_pct == r1.veto_flip.real_return_pct   # same Claude run
        assert r1.cache_sha256 == ct.file_sha256(cache)
        text = r1.summary()
        assert text.index("Agreement with signal") < text.index("Co-primary") < text.index("Descriptive")
        assert "alpha = 0.025" in text


def test_replay_matches_the_recorded_decisions_in_a_backtest():
    """The replayed run is a real backtest driven only by the cache."""
    from tradebot.sim.nulltest import _run
    from tradebot.core.risk import RiskConfig
    with tempfile.TemporaryDirectory() as tmp:
        streams, prompts, cache, _, _ = _collected_cache(tmp)
        brain = ct.ReplayBrain(cache, MODEL)
        r = _run(streams, _ensemble, brain, RiskConfig(max_open_positions=3), 25_000.0)
        assert brain.hits == len(prompts) and r.n_signals == len(prompts)


# --- the random veto/flip twin ------------------------------------------------------------------------------
def test_veto_flip_twin_hits_its_rates_deterministically():
    acct = Account(cash=1, equity=1)
    sigs = [Signal(ts=_ts() + timedelta(hours=h), symbol="BTC-USD", side=Side.BUY,
                   strength=0.8, reason="r", features={"rsi": 55.0}) for h in range(2000)]

    def counts(seed):
        twin = ct.TwinFactory(0.2, 0.1)(seed)
        ds = [twin.decide(s, acct, 1.0) for s in sigs]
        return (sum(d.veto for d in ds),
                sum((not d.veto) and d.side == Side.SELL for d in ds),
                [d.side for d in ds])
    v, f, sides = counts(1)
    assert 0.17 < v / 2000 < 0.23 and 0.07 < f / 2000 < 0.13, (v, f)
    assert counts(1)[2] == sides and counts(2)[2] != sides


def test_window_slicing_is_start_inclusive_end_exclusive():
    streams = _streams(n=100)
    cs = streams["BTC-USD"]
    cut = ct.slice_streams(streams, cs[10].ts, cs[20].ts)["BTC-USD"]
    assert cut[0] == cs[10] and cut[-1] == cs[19] and len(cut) == 10


def test_preregistration_placeholders_are_filled():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prereg.md")
        Path(path).write_text("hash {{PROMPT_HASH}} model {{MODEL_ID}} n {{N_PROMPTS}} "
                              "mt {{MAX_TOKENS}} bound {{COST_BOUND_USD}}")
        prompts = [_prompt()]
        assert ct.fill_preregistration(path, MODEL, prompts, "abc", "w", "1.00") == 5
        text = Path(path).read_text()
        assert PROMPT_HASH in text and MODEL in text and "{{" not in text


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
