# mm-harness

Practice harness for a quant market-making bot competition. Optimized for
fast editing under time pressure, not for framework completeness.

## Commands

```
.venv/bin/python run.py            # normal regime
.venv/bin/python run.py --hard     # stress regime: high vol, 45% informed
.venv/bin/python sweep.py          # all market x bg regimes, 8 seeds each
.venv/bin/python sweep.py --curve  # PnL vs latency 0/1/2/5 with attribution
.venv/bin/python tests.py          # queue + latency regression tests
```

Run tests.py after touching order_book.replace or the latency pipeline.
The queue priority rules are subtle and silently wrong is the failure mode.

run.py flags: `--steps`, `--seed`, `--rebate`, `--bg tight|medium|wide`
(background competition regime, defined in run.py BG_REGIMES), `--latency N`.
sweep.py flags: `--latency`, `--curve`, `--seeds`, `--steps`. Only
dependency is numpy (in .venv).

## Hard rules

- The interface seam is sacred: strategies and diagnostics may only use the
  state dict keys (fair_est, inventory, cash, vol_est, time_left, book,
  tick, queue_ahead_bid, queue_ahead_ask) and the Order/Fill shapes. The sim will be swapped for a
  real exchange adapter behind this seam. Never let a strategy import from
  sim/ or read counterparty kind off a fill.
- Risk control is the point. Any new strategy or metric should make
  inventory risk and adverse selection visible, not hide them.
- Readable over clever. No em dashes in comments or output.
- Keep flow generation independent of the book state so same-seed runs are
  identical across strategies (this is what makes comparisons fair).

## Architecture notes

- engine/order_book.py: FIFO price-time book. Flow orders are sent as
  marketable limit orders and cancelled right after (IOC) by the backtest.
  OrderBook.replace holds the queue semantics: same price keeps position,
  new price joins the back, size down trims from the back (front spot
  kept), size up adds the delta as a new order at the back. queue_ahead
  counts better priced size plus earlier size at our price.
- Background liquidity (agent "bg", from Market.generate_background) is
  what makes queue position real. Quoter count, presence, size and half
  spread range are a regime (BG_REGIMES in run.py); each quoter draws a
  fresh half spread per step. All bg orders cancel and re-add every step,
  so they always join the back; an MM that held its price is in front of
  them. bg refreshes BEFORE the MM quotes each step.
- sim/market.py: fair value draws its NEXT move one step early; informed
  traders trade on that pending move. That is the adverse selection.
- engine/backtest.py: owns PnL attribution (spread capture at fill vs fair,
  adverse selection realized on the next step move against informed size,
  stale cost from fair moving between quote decision and fill). MM agent
  name is "mm", flow is "flow". Latency is a deque of pending quote
  instructions: the decision from step t is applied at t+N, so flow in
  between hits the old resting quote. latency=0 reproduces the pre latency
  numbers exactly (verified), so it is a safe baseline.
- strategies/market_makers.py: LADDER exports the five classes. Instantiate
  fresh per run; AdverseSelectionMM carries toxicity state between steps.
- Rungs 2 and 3 inherit (Symmetric -> InvSkew -> VolScaled), rung 5
  inherits from rung 4. Tune via constructor args, not edits.

## Tuning history (why the defaults are what they are)

- AS gamma=0.3, k=10, horizon=30: gamma 1.0 made the reservation skew punt
  inventory through fair at a loss in the hard regime (negative spread
  capture, MaxDD ~390). Larger horizon (60) had the same pathology.
- tau_floor=0.3: pure AS lets spread and skew collapse as time_left -> 0,
  which got the MM run over late in high vol sessions (MaxDD 4.6 -> 1.9 in
  the hard regime after flooring).
- Toxicity guard reacts to NET one sided pressure only; widening on gross
  two sided flow just taxes healthy noise fills.

## Verified behavior

Run `sweep.py` for the current numbers; it covers both market regimes and
all three bg regimes at 8 seeds. Single seed numbers, especially MaxDD,
are noisy. Any conclusion worth acting on must hold across the sweep.

## How queue position changed the results

- Everyone's fill counts dropped versus the pre-queue harness because the
  background quoter now competes for the same flow (normal regime: InvSkew
  ~5200 -> ~4000 fills). Fills behind a queue are also worse fills: you
  only trade when flow sweeps deep enough to exhaust everyone ahead, and
  deep sweeps are disproportionately informed.
- The fill rate / edge tradeoff is clean in the numbers (normal, 8 seed
  means): AdvSelGuard captures 0.19 per fill at a 0.30 fill rate; QueueAware
  captures 0.07 per fill at a 1.29 fill rate and ends up with the best PnL
  (313 vs 215) and Sharpe (42 vs 23). Tighter price, less spread per fill,
  far more fills.
- QueueAware benefited most: jumping deep queues and holding the front spot
  (avg queue at fill 0.2 vs 0.4 for InvSkew, fills +29%) wins clearly in
  the normal regime. In the hard regime queue jumping is an adverse
  selection magnet (AdvSel -267 vs -155 for the guard): being first in
  queue means eating the informed flow first, so priority is only worth
  buying when flow is mostly noise.
- The wide quoting AS rungs lost the most fills (fill rate ~0.3 in normal)
  because the background quotes tighter than their optimal spread; their
  k parameter models flow decay and no longer matches a market with a
  competitor at the top of the book. Retune k upward (steeper decay) if
  they should stay competitive in the normal regime.
- Symmetric in the hard regime always quotes inside the wide background
  (0.05 vs 0.10 half spread), so it is always alone at the front: fill rate
  1.68, queue 0.0, and the worst adverse selection. Price priority without
  risk control just means getting picked off faster.

## Background regime robustness (sweep.py, 8 seeds, 2026-07)

The bg quoter's spread is a regime (tight 0.02-0.05, medium 0.05-0.10,
wide 0.10-0.18) precisely because the earlier medium-only results partly
measured who matched that one landscape. What survived the full sweep:

Robust across all six market x bg cells:
- Inventory control beats Symmetric on every risk metric, every cell. The
  gap is never close (MaxDD 47-180 vs low single digits).
- The toxicity guard cuts adverse selection versus plain AS in every cell,
  and its Sharpe is at or above AS in five of six (in hard+tight it is the
  single best strategy: Sharpe 12.1 vs AS 11.2).
- QueueAware is the best Sharpe in the ENTIRE normal market row, in all
  three bg regimes (23.8 / 45.3 / 48.6). When flow is mostly noise, queue
  tactics win regardless of how tight the competition quotes. Note the win
  when bg is wide comes mostly from holding the front spot, not jumping.
- VolScaled is the most rank stable strategy overall: second or first in
  five of six cells, never worse than third. A good default if the
  landscape is unknown.

Artifacts of a particular landscape (ranking flips to remember):
- QueueAware's win does NOT survive hard+tight: it drops to fourth (Sharpe
  5.6, AdvSel -252, MaxDD 5.9). Jumping tight queues in an informed heavy
  market means quoting 2-5 ticks from fair into toxic flow. If the day
  looks informed heavy AND other bots quote tight, switch it off or raise
  min_edge/deep_queue. In hard+wide it is a coin flip with VolScaled.
- The AS rungs' weak normal regime showing is a tight/medium competition
  artifact: fill starved at fill rate 0.15-0.19 there, but in normal+wide
  they jump to second and third (Sharpe 42.4/39.3) once their optimal
  spread matches the landscape. Their k should really track the observed
  landscape; a fixed k only fits one bg regime.
- InvSkew's fixed 0.05 half spread nearly dies in hard+tight (Sharpe 2.1,
  PnL 24.5): fills only toxic flow behind tighter competition. Fixed
  spreads do not survive landscape changes; vol scaling is the cheapest
  fix (VolScaled holds up fine in the same cell).
- Absolute PnL levels are landscape artifacts everywhere (everyone's PnL
  roughly halves from wide to tight competition). Compare strategies
  within a cell, never across cells, and never tune to absolute PnL.

## Latency sensitivity (sweep.py --curve and --latency, 8 seeds, 2026-07)

Latency N means the quote decided at step t only rests at t+N. This is the
harshest knob in the harness: it dominates every conclusion the bg regime
work produced. Everything below is 8 seed means.

### The adverse selection metric is a trap (read this first)

Expectation going in was that AdvSel gets worse with latency. It does the
opposite: hard+medium InvSkew goes -216 (N=0) -> -159 (N=1) -> -125 (N=2)
-> -76 (N=5). The economics are still exactly as expected, the metric just
does not measure it. AdvSel is FORWARD looking (the move after a fill).
A latency pick off is a fill on a move that ALREADY happened, which lands
in the new stale_cost column: same cell, -20 -> -275 -> -387 -> -494. Also
once quotes are stale, noise traders pick you off too, so the loss moves
out of the informed attributed bucket entirely.

Never read AdvSel alone once latency is on. Improving AdvSel is a symptom
of losing money to staleness instead. Read stale_cost with it.

### Edge decay per step

PnL cost of the FIRST step of latency, normal+medium: InvSkew -238,
VolScaled -225, QueueAware -199, Symmetric -106. AS rungs actually GAIN
(+38 AvellStoikov, +25 AdvSelGuard) because they were fill starved at N=0
and staleness hands them fills their spread is wide enough to absorb.
By N=5 everything except Symmetric is deeply negative in every cell.

The mechanism is half spread versus one step of vol, confirmed directly by
sweeping InvSkew's half_spread in normal+medium (vol 0.04):

  half 0.05 (1.25x vol)  Sharpe  25.2 -> -5.0 -> -16.0  at N=0/1/2
  half 0.10 (2.5x)       Sharpe  23.1 -> 13.3 ->  -3.6
  half 0.15 (3.75x)      Sharpe  12.8 -> 14.5 ->   8.7
  half 0.25 (6.25x)      Sharpe   6.9 ->  6.9 ->   7.4

Rule of thumb: you need half spread >= 2 to 3 times sigma*N to survive
latency N. Going wider buys latency immunity and costs zero latency edge,
and at 6x vol the strategy is flat across all latencies. On the day,
measure the real round trip first and set the floor spread from it.

### What still holds once quotes go stale

- The toxicity guard cuts adverse selection versus plain AS in all six
  cells at N=0, all six at N=1, and all six at N=2. It cuts stale_cost in
  all six too. This is the single most robust finding in the harness and
  the only one that survived every regime and every latency. Note it is a
  RISK win, not always a Sharpe win: at N=2 hard+tight the guard is last
  by Sharpe (-3.1) because everything there loses money.
- Inventory control still beats Symmetric on AvgInv and MaxInv everywhere.
  The PnL and Sharpe ranking versus Symmetric does NOT survive (below).

### What breaks

- VolScaled is NOT the most rank stable strategy once latency is on. That
  was a zero latency artifact. At N=1 it drops to 5th with negative Sharpe
  in normal+tight (-1.6) and normal+medium (-1.9), while staying 1st or 2nd
  in the hard cells. Its vol scaling widens it in high vol (which is also
  where staleness hurts most, so it accidentally hedges) but in the normal
  market it quotes ~0.06 half, under the 2x sigma floor, and bleeds.
- QueueAware's clean sweep of the normal market is a zero latency artifact.
  It was 1st in all three normal cells at N=0; at N=1 it is 3rd/3rd/3rd and
  at N=2 4th/3rd/3rd, and it is 5th in hard+tight at N=1. Front of queue
  plus a stale price is the worst place on the book: you are first in line
  to be picked off. Its stale_cost is the highest of the risk controlled
  strategies (normal+medium N=1: -134 vs -78 for the guard). Queue tactics
  need low latency to pay; do not run them if the round trip is slow.
- InvSkew is the biggest casualty: negative Sharpe in 5 of 6 cells at N=1
  and 6 of 6 at N=2, with MaxDD blowing from 0.7 to 100-350. A fixed tight
  spread is the single worst thing to bring to a latency environment.
- The AS rungs invert: fill starvation looked like a weakness at N=0 in the
  normal market (fill rate 0.15-0.19, mediocre rank) and becomes the top
  ranking strategy pair at N=1 and N=2 there. Wide quoting is latency
  insurance. Their earlier "retune k upward to get more fills" note is now
  actively bad advice if the real setup has latency; check --curve first.
- MaxDD stops measuring inventory spikes and starts measuring steady bleed.
  At N=2 normal+medium InvSkew has MaxDD 224 with AvgInv only 5.0: the
  position is controlled, the strategy is just losing monotonically.

### Ranking artifacts to not be fooled by

- Symmetric ranks 1st by Sharpe in several hard cells at N=1 and N=2. This
  is only because every risk controlled strategy has gone negative there.
  Its Sharpe is 1.1-1.7 in absolute terms with MaxDD 137-203 and AvgInv
  45-62. It is not a strategy, it is the least bad loser in a losing cell.
- Symmetric's PnL RISES with latency in hard+tight (108 -> 410 at N=5)
  while its stale_cost goes to -972. It carries such a large uncontrolled
  position that its PnL is a lottery on the fair value path, not edge.
  Ignore its PnL entirely; look at its Sharpe and AvgInv.
- Any cell where every strategy is negative should be read as "do not
  trade this configuration", not as a ranking.
