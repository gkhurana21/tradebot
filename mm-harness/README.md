# mm-harness

Practice harness for a quant market-making bot competition. Small on purpose:
a limit order book, a simulated market with adverse selection, a ladder of
five market makers with increasing risk control, and a comparison report.

## Quick start

```
python3 -m venv .venv
.venv/bin/pip install numpy
.venv/bin/python run.py            # normal regime
.venv/bin/python run.py --hard     # high vol, informed heavy stress regime
.venv/bin/python sweep.py          # robustness: all market x bg regimes, 8 seeds
.venv/bin/python sweep.py --curve  # PnL vs latency 0/1/2/5, per strategy
```

Flags for run.py: `--steps N`, `--seed N`, `--rebate X` (maker rebate per
unit), `--hard`, `--bg tight|medium|wide` (background competition regime),
`--latency N` (steps between deciding a quote and it resting).
sweep.py takes `--latency N`, `--curve`, `--seeds N`, `--steps N`.

## Layout

```
engine/order_book.py        limit order book, price-time FIFO matching
engine/backtest.py          step loop, PnL marking, PnL attribution, fees
sim/market.py               latent fair value + noise/informed flow
strategies/market_makers.py Strategy base class and the strategy ladder
diagnostics/report.py       summarize() metrics and print_table()
run.py                      run the whole ladder on one identical market
sweep.py                    multi seed sweep across market, bg and latency
tests.py                    regression tests for queue and latency logic
```

## The interface seam

Strategies and diagnostics only ever touch:

- the state dict passed to `quote(state)`: `fair_est`, `inventory`, `cash`,
  `vol_est`, `time_left`, `book`, `tick`, `queue_ahead_bid`,
  `queue_ahead_ask` (resting size that fills before our order does, None
  when we have no resting order on that side)
- the `Order` and `Fill` dataclasses in `engine/order_book.py`

To trade a real exchange later, write an adapter that produces the same
state dict and Fill shapes and drop it in place of `sim/` and the backtest
loop. Strategies and diagnostics should not need to change.

## The ladder

1. `SymmetricMM` - fixed half spread around fair. No risk control. The
   baseline everything else is measured against.
2. `InventorySkewMM` - shifts both quotes against inventory, stops quoting
   a side past `max_inventory`. Kills the inventory random walk.
3. `VolScaledMM` - rung 2 plus spread widens with realized vol.
4. `AvellanedaStoikovMM` - closed form reservation price and spread:
   `r = s - q * gamma * sigma^2 * (T - t)`,
   `spread = gamma * sigma^2 * (T - t) + (2 / gamma) * ln(1 + gamma / k)`.
   A `tau_floor` keeps spread and skew alive late in the session (pure AS
   lets both collapse as T-t goes to 0 and gets run over in high vol).
5. `AdverseSelectionMM` - rung 4 plus a toxicity guard: decayed fill
   pressure per side, and the side being picked off gets widened and sized
   down. Only reacts to one sided pressure; balanced flow is healthy noise.
6. `QueueAwareMM` - rung 2 risk control plus queue tactics: improve the
   best price by one tick when the queue ahead is deep, join the level when
   it is shallow, hold the price when already at the front. Trades edge per
   fill for fill frequency.

## What the sim models

- Fair value: random walk, optional mean reversion, tunable `vol`.
- Flow: Poisson arrivals of marketable limit orders. Each trader will pay
  at most `fair +/- Exp(patience)`, so wider quotes really do fill less.
- Adverse selection: `informed_frac` of flow trades in the direction of the
  next fair value move, which the MM has not seen yet.
- Queue competition: background quoters rest random size around fair each
  step, each drawing a half spread from the bg regime's range (tight,
  medium or wide, set with `--bg`). They cancel and re-add every step, so
  they always join the back of a level. Flow consumes resting orders
  strictly in price-time order, so an MM behind them only fills once they
  are exhausted. The competition is a regime on purpose: on the day you
  cannot assume the other bots' spread, so check conclusions with sweep.py
  across all bg regimes.
- Queue semantics on re-quote (`OrderBook.replace`): same price keeps queue
  position, new price joins the back of that level, moving away and coming
  back means the old spot is gone. Sizing down keeps the front spot, sizing
  up adds the extra as a new order at the back.
- Fair comparison: flow and background generation never look at the book,
  so every strategy run with the same seed faces the identical market.

## Reading the table

Risk control is the point. The columns to watch are MaxDD, AdvSel (PnL lost
to informed flow, less negative is better), AvgInv and MaxInv. FillRt is
fills per quote offered and AvgQ is the average queue size ahead of our
order when it filled; together they show what queue priority buys. Expected
shape, averaged over seeds:

- Symmetric: decent PnL, terrible everything else (MaxDD ~100+, AvgInv ~50).
- Each later rung tightens inventory and drawdown; the last two rungs also
  cut adverse selection by roughly 30 to 45 percent versus the baseline.
- VolScaled is a strong hand tuned baseline in the hard regime; the AS rungs
  win on adverse selection and inventory, and the guard has the lowest
  drawdown.

## Latency

`--latency N` puts N steps between the moment a strategy computes a quote
and the moment that quote rests in the book. The quote decided at step t
uses step t state but only goes live at t+N; flow in between trades against
the OLD resting quote. N=0 is the instant re-quote baseline.

This is the harness's most punishing knob. A quote left resting while fair
moves is exactly what gets picked off, and the cost shows up in the `Stale`
column, not `AdvSel` (see below). Rough rule from `sweep.py --curve`: a half
spread of at least 2 to 3 times `sigma * N` survives latency N; anything
tighter bleeds. Quoting 1.25x sigma dies at N=1, 3.75x holds through N=2.

## PnL attribution

`spread_capture` books signed edge versus fair at each fill.
`adverse_selection` books the next step fair move on size traded against
informed counterparties. `stale_cost` books the fair move between the quote
DECISION and the fill, which is where latency pick offs land. The backtest
does the attribution because it knows which flow order was informed and
when each quote was decided; strategies never see either.

Note the two toxicity columns measure different things. `adverse_selection`
is forward looking (the move AFTER the fill) and `stale_cost` is backward
looking (the move BEFORE it, since the decision). Latency pick offs are
mostly on moves that already happened, so raising latency makes `AdvSel`
look BETTER while `Stale` explodes. Read them together, never AdvSel alone.
