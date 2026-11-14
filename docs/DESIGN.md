# Design decisions

Short records of the choices that shape tradebot, why they were made, and what they cost. Each one is
something an engineer reading the code might reasonably ask about.

---

## 1. The risk gate runs in a separate process

**Decision.** The Go engine owns sizing, approval, the position book and bracket exits. Python research code
proposes decisions over HTTP and can't place an order any other way (`tradebot live --engine URL`).

**Why.** Pre-trade risk controls that the trading logic can't bypass are a regulatory expectation, not just a
nicety: SEC Rule 15c3-5 (the "market access rule") requires brokers to apply them before orders reach the
market. The cautionary tale is Knight Capital in 2012, which lost about $460 million in 45 minutes when dormant
code sent orders that nothing independent stopped. A process boundary turns "the research code must never have a
bug that skips the check" into "the research code can't reach the market except through the check".

**Cost.** A network hop (measured in the README's latency table) and two implementations of the sizing rules,
kept in step by a shared parity test (record 8).

## 2. Python refuses to drive a live engine

**Decision.** Before trading, Python reads `/health` and refuses unless the engine reports `venue: "sim"` and
`live: false`. An Alpaca paper engine needs an explicit `--allow-venue alpaca-paper`, and is never allowed with
synthetic prices. A live engine is refused whatever it's called. The engine listens on 127.0.0.1 by default.

**Why.** Synthetic or replayed prices sent to a real account would place real stop and target exits at fake
prices. This project never trades real money from Python.

## 3. A timeout means "unknown", never "no trade"

**Decision.** If a request fails before it's sent (connection refused), Python records no trade. If it fails
after the body was sent (read timeout, dropped connection, 504), the outcome is **unknown**: the symbol pauses
and Python re-sends the same `client_id`. The engine records each `client_id` on arrival and returns the stored
result for a repeat, so a retry can never open a second position. Every decision also carries the engine's
`boot_id`; if the engine restarted, it answers 409 before acting and Python stops.

**Why.** A timeout on a POST is ambiguous: the order may still fill. Treating it as a rejection is how books
drift from the broker's.

## 4. The kill switch is per day in Python, per process in Go

**Decision.** Python backtests and the local live loop start a new risk session on each new UTC day; a halt
lasts the rest of the day it fired. The Go engine's session is its process lifetime; it starts a new session
only when restarted, and Python stops if that happens mid-run (record 3).

**Why.** Backtests replay months of data and need daily sessions to be realistic. A long-running engine
resetting itself silently at midnight is a choice an operator should make explicitly, by restarting it.

## 5. Recovering equity doesn't clear a halt

**Decision.** Once the drawdown limit trips, trading stays halted until a new session, even if open positions
recover.

**Why.** A kill switch that re-arms itself is not a kill switch.

## 6. A bar that touches both stop and target counts as the stop

**Decision.** In backtests, when one bar's high and low span both the stop and the target, the position exits
at the stop.

**Why.** Open-high-low-close data can't say which came first. Assuming the favorable one would inflate every
backtest with a volatile bar.

**Where exits fill.** Backtests and the Go engine's simulated venue fill a touched stop or target at that level
plus slippage. The local Python live loop (no engine) exits at the bar's close plus slippage, which can be worse.
A real market can gap straight through a stop, so every simulated result here is an upper bound on what a live
venue would give.

## 7. The coin-flip test randomises position chains, not trades

**Decision.** The null model keeps the strategy's timing, sizing and costs and gives each position chain (flat
to flat) a random sign.

**Why.** Flipping each trade independently reverses positions far more often than the strategy does, pays more
fees, and makes any strategy look skilled. A first version that flipped per run of same-direction decisions still
traded about a third less than the strategy on real data; it was replaced before any result was published.

## 8. Python and Go size orders the same way

**Decision.** Shared JSON fixtures run through both risk engines' tests and must produce the same verdict and
quantity. Backtests and the coin-flip test use the Python engine, so this parity test is what lets their
results stand in for the Go gate. Known differences are listed here before the test runs:

- **Rounding.** Python rounds order quantity, stop and target to 8 decimals; Go keeps full precision. The
  parity test rounds Go's numbers to 8 decimals before comparing (1e-9).
- **Typed verdicts.** Python returns a reason string, Go a typed verdict plus a reason; the fixtures use Go's
  verdict names and the Python test maps the reason text to them. Reason wording differs (Go's carry the
  numbers) and so does the order's `tag` (`conf=0.90` in Python, the rationale in Go).
- **Stricter inputs in Go.** Go rejects a malformed decision (empty symbol, unknown side, NaN, confidence or
  `size_hint` outside 0..1) and a non-positive or non-finite price as `rejected_invalid_decision`. Python does
  no validation: it clamps `size_hint` to 0..1 and lets an out-of-range confidence through. A model that
  answers `size_hint: 1.4` is sized at full size by Python and refused by Go. The fixtures stay inside the
  valid range.
- **Check order beyond the shared cases.** Both check halt, then veto/FLAT, then confidence, then open
  positions, in that order. Go puts validation after the halt check and a non-positive equity check
  (`rejected_zero_size`) before the position limit; Python has neither. They only disagree for equity at or
  below zero or for invalid input.
- **Zero stop distance.** Python floors the stop distance at 1e-9 and sizes anyway; Go rejects a non-positive
  stop distance as invalid. This only happens at a price of zero.
- **Open-position limit.** The Python CLI raises `max_open_positions` to the number of symbols it trades
  (`_risk()` in `cli.py`); Go's profile fixes it at 3. The fixtures pass the limit explicitly.
- **Sessions.** The kill-switch arithmetic is identical (drawdown from the session's starting equity, `>=` the
  limit); the fixtures cover it. What differs is when a session starts (record 4).
- **Fills.** Sizing is the same; fills are not always. Python backtests and Go both fill a stop or target at
  its level plus slippage; the local Python live loop exits at the bar's close (record 6). Fee and slippage
  defaults are set separately in each engine. Parity covers sizing and approval only.

Everything else in the sizing path is the same formula: fixed-fractional risk to the stop, capped by max
notional, scaled by `size_hint`, minus same-side exposure already held, with the stop and target at fixed
percentages from the entry. A flip is sized as a fresh order against the pre-close book in both.

## 9. The Claude brain doesn't see the account

**Decision.** The prompt holds the signal, its features and the price, not equity or position size.

**Why.** Sizing and position awareness are the gate's job, which is the project's whole premise. It also makes
Claude's answers depend only on the market, so one paid run can be reused by all 199 coin-flip twins, and the
cost of the whole test is known exactly before any call is made.
