# Does an LLM add trading skill? A pre-registered test

*tradebot research note · 2026-11-14 · every number below comes from a command in this repo*

## Summary

A rule-based crypto strategy and the same strategy filtered by Claude Haiku 5.5 were tested against coin-flip
versions of themselves on out-of-sample data. Neither showed skill. The strategy did not beat randomly flipped
directions (p = 0.150). Claude did not pick directions better than chance (p = 0.805). Claude's choice of which
trades to skip beat random skipping after costs (p = 0.005, pre-registered), but only because it traded half as
much: with fees and slippage removed, random skipping did better (p = 0.900, exploratory). The whole Claude test
cost $0.27.

## Question

Backtest returns can't tell skill from luck. The question here is narrower and answerable: **does the strategy, or
the LLM on top of it, do better than trading the same moments in random ways?**

## Setup

- **Data.** Coinbase hourly candles, BTC-USD, ETH-USD and SOL-USD, 2026-08-15 to 2026-10-31 (1,872 bars each),
  committed in `docs/data`. The strategy code last changed on 2026-08-14, so the window is out-of-sample. It also
  starts after Haiku 5.5's training cutoff (June 2026), so the model can't have memorised it.
- **Strategy.** An ensemble of EMA/RSI trend, Bollinger mean-reversion and chart-pattern signals.
- **Execution.** Position sizing from fixed-fractional risk, a 20% position cap, max 3 open positions, a 5% daily
  drawdown kill switch, 10 bp fees and 5 bp adverse slippage, $25,000 starting cash.
- **Statistic.** Total return after all costs, including the close-out at the end. With K = 199 twins,
  p = (1 + #twins ≥ real) / (K + 1), one-sided; the smallest possible p is 0.005.

## Null models

A null is only fair if it trades about as much as the thing it is compared with; otherwise fees decide the result.

1. **Sign-randomised twins (direction).** Keep every entry, exit, size and veto; give each *position chain* (flat
   to flat) a random direction. Turnover matches: 633 vs 624 round trips for the strategy. An earlier version
   flipped a coin per run of same-direction decisions; it traded about a third less than the strategy on real data,
   and was replaced before any result was published.
2. **Random-veto twins (filtering).** Follow the strategy's signals, but skip them at random at the LLM's observed
   veto rate. This tests whether the LLM's *choice* of what to skip beats a random choice at the same rate.

## Results

**The strategy, heuristic brain** (`tradebot nulltest --source snapshot --seeds 199`):

| | Total return | Round trips |
|---|---|---|
| Strategy | −24.99% | 624 |
| Sign-randomised twins, median (5th–95th) | −29.65% (−36.20% to −21.37%) | 633 |
| p | **0.150** | |

**Claude Haiku 5.5 as the brain**, pre-registered in `docs/claude-test-preregistration.md` before any paid call
(`tradebot nulltest --brain claude --seeds 199`, reads the committed answers):

| | Claude | Twins, median | p | Round trips (Claude / twins) |
|---|---|---|---|---|
| Valid run? | 23 of 4,590 answers unusable (0.50%, limit 2%) | | | |
| Agreement with signal | 59.8%, vetoed 40.2%, flipped 0% | | | |
| (a) direction, after costs | −11.39% | −8.03% | **0.805** | 238 / 235 |
| (b) filtering, after costs | −11.39% | −22.06% | **0.005** | 238 / 474 |
| (b) filtering, **before costs** *(exploratory)* | −1.51% | +3.28% | **0.900** | 238 / 473 |

## What the result means

- **No direction skill.** Given its own entry points, Claude's long/short calls did no better than coin flips.
- **The filtering "win" is a turnover effect.** Claude's vetoes cluster in quiet, sideways stretches (its stated
  reasons: "sideways trend with weak fit", "the signal looks like noise"), so it sits out whole periods. Random
  vetoes at the same rate are scattered, so the twins keep re-entering and trade twice as often. After costs that
  alone produces p = 0.005. Before costs, random skipping is ahead, so Claude's choices carried no detectable
  information about which trades would work.
- **Trading less is still worth something.** On this window the cheapest way to lose less was to trade less, and
  Claude does that for $0.27 per 4,590 decisions. That is a cost filter, not market understanding.

## Limits

- One window of about 2.5 months and one stochastic draw of the model; p is conditional on both.
- The zero-cost check was decided after seeing the turnover mismatch, so it is labelled exploratory. The
  pre-registered result is reported as it came out.
- All backtests use the Python risk engine. A parity test with 27 shared cases checks that it sizes and approves
  every case exactly as the Go execution gate does.
- Simulated fills: stops fill at the stop level plus slippage; a real market can gap through a stop.

## Next

A replication on data that doesn't exist yet (2026-11-15 to 2027-01-15) is pre-registered in
`docs/claude-replication-preregistration.md`, with the zero-cost filtering test as its single primary test
(α = 0.05). It will be run once, after the window closes, and published whatever it shows.

## Reproduce

```bash
pip install -e ".[llm]"
tradebot nulltest --source snapshot --seeds 199                  # strategy vs coin flips (free)
tradebot nulltest --brain claude --seeds 199                     # Claude, from the committed answers (free)
tradebot nulltest --brain claude --seeds 199 --zero-costs        # the before-costs check (free)
```
