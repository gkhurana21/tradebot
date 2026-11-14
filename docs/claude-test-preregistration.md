# Pre-registration: does the Claude brain beat a coin flip?

Status: **FROZEN 2026-11-14.** Filled by
`tradebot nulltest --brain claude --build-prompts --fill-prereg docs/claude-test-preregistration.md`.
This file is pushed **before any paid call**; everything after that point is run exactly as written here.
The prompts file is rebuilt deterministically by the same command and checked against the sha256 below.

## Question

On the fixed out-of-sample snapshot, do the Claude brain's decisions earn a higher total return after costs
than (a) the same decisions with each position chain's direction randomised and (b) a twin that vetoes and
flips the strategy's signals at random at Claude's own observed rates?

## Frozen inputs

| Item | Value |
|---|---|
| Window | snapshot start to snapshot end (end exclusive), BTC-USD, ETH-USD, SOL-USD |
| Data | `docs/data/*-1h.csv` (snapshot, committed) |
| Strategy, risk engine, fees, slippage, cash | as in the existing `nulltest` (ensemble, `RiskConfig(max_open_positions=3)`, $25,000) |
| Model id | `claude-haiku-5-5` |
| Prompt version | `v2` |
| Prompt hash | `677c6cec6a4a7acdbf61f8ed8b28e202b01fa6812d28036b7ac6794d0d21cdbe` (sha256 of system prompt + user template, see `brain.PROMPT_HASH`) |
| Prompts | 4590 account-free prompts, file sha256 `f6ec21f27c5564654a26bee0d084f9227cabe4286ebaa04ff5846503267bf5b5` |
| `max_tokens` | 200; thinking disabled; no `temperature`/`top_p`/`top_k`; SDK `max_retries=0` |
| Cost cap | $1.00 hard cap; the tool refuses to submit if its exact bound exceeds it |
| Cost bound | $0.505908 (batch price, 50% of list; input at 1 token per UTF-8 byte of system + prompt plus a fixed per-request allowance, output at n x `max_tokens`) |

The prompt contains the signal side, strength, reason, its features and the price. It contains no equity and no
position, so every prompt is known before anything is sent and a decision is a function of (model, prompt,
symbol, ts) only.

## Procedure (one run, no reruns)

1. Build prompts offline (`--build-prompts`); freeze the hash above; push this document.
2. Pilot: `--submit-batch --pilot 50`, then `--collect`. It reports agreement, veto and flip rates and the
   spend. **The pilot may only abort the run.** It never changes the prompt, the window, the tests or the
   thresholds. Pilot results are published whatever they show. The pilot's answers stay in the decision cache
   and are not re-asked.
3. Full batch: `--submit-batch`, then `--collect`. One Message Batch. The ledger records every request and all
   four usage fields; the tool's spend counter is published.
4. Failure rule: a parse failure, refusal, `max_tokens` stop, or batch item `errored`/`expired`/`canceled` is
   recorded as FLAT with `source=fallback`. If more than 2% of prompts fall back, the run is **invalid** and no
   verdict is drawn or published as a result.
5. The decision cache (JSONL, keyed by model, prompt version, symbol, ts, with a signal fingerprint) is
   committed with its sha256. All statistics below read only the cache; a missing key or a changed signal is an
   error. They cost $0.
6. No second paid draw. The model's sampling cannot be pinned, so the cache is the experiment's record.

## Statistics

Primary metric for every test: total return after all costs, including the close-out at the end. Win rate,
trade counts and null spreads are description only and are never tested.

Reported first, descriptive: the **agreement rate** between Claude's decision and the strategy's signal, with
the veto/FLAT rate and the flip rate (fallbacks count as vetoes, because that is how the run traded them).

Co-primary tests, each versus K = 199 twins, one-sided, empirical
`p = (1 + #twins >= Claude) / (K + 1)` (smallest possible p = 0.005):

- (a) **Sign-randomised twins.** Claude's decisions with each position chain's direction multiplied by a
  random sign (`NullDirectionBrain`). Holds Claude's vetoes, timing, sizes and turnover fixed; tests direction
  skill.
- (b) **Random veto/flip twins.** Follow the strategy's signal, then veto at Claude's observed veto rate and
  flip at Claude's observed flip rate, drawn independently per signal from a seeded hash. Confidence and size
  come from the heuristic brain with its own vetoes cleared. Tests whether Claude's filtering beats random
  filtering at the same rates.

**alpha = 0.025 for each test** (Bonferroni over two). Claude "beats" a twin family only if its p < 0.025.
Both p-values are published whatever they are.

Descriptive only: the heuristic brain with its own sign-randomised null (K = 199) on the same window. Claude's
raw return is never compared with the heuristic's raw return, because the heuristic never vetoes and the two
pay different fees.

## Known limits (stated now, not after the result)

- One draw of a stochastic model; p is conditional on that draw.
- The sign-randomised null holds Claude's vetoes fixed, so it cannot see value that comes from filtering;
  test (b) is there for that, and it controls for rates, not for which signals were filtered.
- Twin runs reuse Claude's cached decision at each (symbol, ts); that is the mirror-world null, approximate
  where stops or targets make the paths diverge.
- A null result is the likely outcome and will be published as such.
