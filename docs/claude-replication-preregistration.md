# Pre-registration: do Claude's skips carry information? (fresh data)

Status: **FROZEN 2026-11-14**, before any bar in the window below exists. Committed and pushed before the data is
fetched. Everything after that point is run exactly as written here.

## Why

The first run ([pre-registration](claude-test-preregistration.md), results in the README) found that Claude
Haiku 5.5 does **not** pick trade directions better than chance (p = 0.805). Its choice of *which* signals to
skip beat skipping the same share at random after costs (p = 0.005), but the random-skip twins traded twice as
much (473 vs 238 round trips). With fees and slippage set to zero the edge disappeared (p = 0.900): the
after-cost result came from trading less, not from choosing better. This run asks, on data nobody has seen,
whether Claude's skips carry any information beyond lowering turnover.

## Frozen inputs

| Item | Value |
|---|---|
| Window | **2026-11-15 00:00 UTC to 2027-01-15 00:00 UTC** (end exclusive), BTC-USD, ETH-USD, SOL-USD, hourly |
| Data | fetched once with `tradebot snapshot --start 2026-11-15 --end 2027-01-15 --out docs/data-replication`, committed |
| Strategy, risk, fees, slippage, cash | unchanged from the first run (ensemble, `RiskConfig(max_open_positions=3)`, $25,000) |
| Model | `claude-haiku-5-5`, prompt `v2`, hash `677c6cec6a4a7acdbf61f8ed8b28e202b01fa6812d28036b7ac6794d0d21cdbe` |
| `max_tokens`, thinking, retries | 200; disabled; SDK `max_retries=0` (as before) |
| Cost cap | $1.00 hard cap, exact whole-batch bound checked before submitting |
| Cache | `docs/claude/replication/decisions.jsonl`, committed with its sha256 |

No code, prompt, strategy or risk setting changes between the two runs. If `claude-haiku-5-5` is no longer
served, the replication is reported as **not run**; no other model is substituted.

## Procedure (one run, no reruns)

1. After 2027-01-15, fetch the window with the snapshot command above and commit the CSVs.
2. `tradebot nulltest --brain claude --data docs/data-replication --prompts docs/claude/replication/prompts.jsonl
   --cache docs/claude/replication/decisions.jsonl --build-prompts`, then `--submit-batch`, then `--collect`
   (no pilot this time: the first run already measured the model's behaviour).
3. Failure rule as before: parse failure, refusal, `max_tokens` stop, or an errored/expired/cancelled batch item
   → FLAT `source=fallback`; more than 2% fallbacks makes the run **invalid** and no verdict is drawn.
4. Run the statistics below from the cache only: the same command with `--seeds 199 --zero-costs` (primary)
   and with `--seeds 199` (after-cost secondaries).

## Statistics

**Primary: test (b) before costs.** Claude's decisions vs K = 199 random veto/flip twins at Claude's observed
rates, statistic = total return **with fees and slippage set to zero** (`--zero-costs`), one-sided empirical
`p = (1 + #twins >= Claude) / (K + 1)`. **α = 0.05** (a single primary test). Zero costs is chosen because the
first run showed that after-cost comparisons reward any filter that simply trades less. Claude's skips count as
informative only if p < 0.05.

**Secondary, reported whatever they show (not tested):**

- Round trips for Claude and the twins' median, reported next to every p-value.
- Test (b) after costs (the first run's original statistic), next to the primary.
- Test (a), the sign-randomised direction test.
- Agreement, veto and flip rates; the heuristic brain with its own null on the same window.

## Known limits

- One stochastic draw of the model; p is conditional on that draw.
- Two months of hourly bars is a few hundred round trips; a null result may reflect low power, and the report will
  say so rather than claim "no effect".
- Random-skip twins trade more than a clustered filter, so after-cost results favour Claude mechanically; that
  is why the primary is measured before costs.
- Zero costs also removes a real benefit of trading less. A filter that only saves fees can still be useful;
  it just isn't evidence that the model understands the market.
- Every result is published, including a failure to replicate.
