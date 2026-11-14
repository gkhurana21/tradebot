# tradebot execution engine (Go)

The low-latency half of a two-language trading system. Python researches and
decides; this service executes and enforces risk.

```
  Python research layer                 Go execution engine
  ─────────────────────                 ───────────────────
  market data ─┐                        ┌─────────────────────────┐
  indicators   ├─→ Claude brain ──HTTP──→│  risk gate (104 ns)     │
  backtests   ─┘   (proposes)            │  position book          │──→ fills
                                         │  bracket monitor        │
                                         └─────────────────────────┘
                                          decides; can always say no
```

**Why split the languages.** Research is IO-bound and exploratory: it calls an
LLM, replays history, and tolerates seconds of latency. Execution is the
opposite — it must be fast, deterministic, and impossible to accidentally
bypass. Putting the risk gate behind a process boundary means a bug in the
research layer *cannot* reach the market without passing it.

## Design

**A single owner goroutine serializes all state.** Ticks and decisions arrive
on channels; one goroutine owns the book and the risk engine. There is no lock
between them because there is no concurrent access — the design removes the
data-race class rather than defending against it. Verified with `-race`.

**Backpressure is explicit.** Market data is dropped when the buffer is full
(a stale price is worse than a missing one) unless the caller asked for
`?wait=1`, in which case a full buffer is a `503`; decisions block with a caller
context so the client controls its own timeout; telemetry is dropped rather
than allowed to stall trading.

**Rejections are typed, not strings.** `Verdict` values (`rejected_at_cap`,
`rejected_low_confidence`, …) can be counted and alerted on, so a shift in the
rejection mix is observable rather than buried in log text.

**Zero dependencies.** Standard library only.

## Run it

```bash
go test -race ./...                    # all tests, race detector on
go test -bench=. -benchmem ./...       # latency + allocation numbers
go run ./cmd/engine -addr :8080 -profile balanced -cash 10000
```

```bash
curl -X POST localhost:8080/tick     -d '{"symbol":"BTC-USD","price":64000}'
curl -X POST localhost:8080/decision -d '{"symbol":"BTC-USD","side":"BUY","confidence":0.9,"size_hint":1}'
curl localhost:8080/positions
```

The server binds `127.0.0.1:8080` by default (there is no authentication, so it
is loopback-only); pass `-addr :8080` to expose it deliberately.

| Endpoint | Purpose |
|---|---|
| `POST /tick` | market data; also drives stop/target monitoring. Async: `202`, and the tick is dropped if the buffer is full |
| `POST /tick?wait=1` | same, but applied by the owner goroutine before the reply: `{equity, halted, exits:[{ts, symbol, side, qty, price, fee, reason}]}`. `503` when the tick buffer is full (never a silent drop) |
| `POST /decision` | proposal in, risk verdict + sized order out. Optional `client_id` (idempotency) and `expect_boot_id` (restart guard); response adds `client_id` and `fills` |
| `GET /decisions?since=<seq>` | journal of the last 256 decisions, exits and halts (see below) |
| `GET /positions` | equity, cash, open positions, each with `mark` and `unrealized_pnl` |
| `GET /stats` | cumulative counters, halt state, `venue`/`live`, `session_start_equity`, `limits` |
| `GET /health` | `{status, uptime, api: 2, boot_id, venue, live}` |

### Bridge contract (`api: 2`)

- **`boot_id`** is random hex drawn at startup. A client that sends it back as
  `expect_boot_id` on `POST /decision` gets `409` (before anything is
  evaluated) if the engine restarted in between.
- **`client_id`** (max 128 bytes) is recorded when the decision arrives, in a
  bounded map of the last 4096 ids. A repeat returns the stored result (waiting
  for the original if it is still in flight, and with `"replayed": true`) and
  never places a second order, even if the first HTTP request timed out.
- **`fills`** lists every fill the decision caused, in order: the close leg of a
  flip (`reason: "flip"`) and then the entry. Rejections return `[]`. These are
  fills in the book's sense; the flip close still counts as an exit in `/stats`.
- **Decision timeout** is `5 s` in simulation. With a venue it is
  `2 x 10 s + 2 s`: a flip is a close and a place, each bounded to 10 s
  (venue retries run inside that bound). The HTTP `WriteTimeout` is derived
  from it.
- **`/stats.limits`** carries the configured fractions (`0.05` is 5%).
  `session_start_equity` is the kill-switch baseline; the session is the
  process lifetime (no day roll).
- **Reasons** carry the numbers: `confidence 0.30 < min 0.55`,
  `position at max notional 20%`, `max open positions 3 reached`,
  `halted: daily drawdown 5.2% ≥ 5%`, `sized 0.078 BTC-USD · risk 1.0% to stop`.
  The typed `verdict` values are unchanged.

### Decision journal

`GET /decisions?since=<seq>` returns
`{"next_seq", "oldest_seq", "gap", "items": [...]}` from a 256-entry ring that
only the owner goroutine writes. Items have `seq` (from 1), `at` (engine wall
clock, RFC3339Nano), `kind` (`decision`, `exit` or `halt`), `reason`, and, where
they apply, `client_id`, `symbol`, `side`, `confidence`, `verdict`, `approved`,
`qty`, `price`. `exit` covers bracket exits and flip close legs; `halt` is
written once, when the kill switch trips.

`since` is inclusive: it returns items with `seq >= since`, so a poller passes
the previous `next_seq`. Omit it to read everything retained. `gap` is `true`
when entries the caller asked for were overwritten (`since < oldest_seq` after
the ring has wrapped); the items returned then start at `oldest_seq`.

## Benchmarks

Apple M3, `go test -bench=. -benchmem`:

| Path | Latency | Allocations |
|---|---|---|
| Risk gate (`Evaluate`) | **104 ns/op** | **0 B, 0 allocs** |
| Risk gate, parallel | 122 ns/op | 0 B, 0 allocs |
| Full pipeline (incl. channel hop) | 1.66 µs/op | 864 B, 4 allocs |

The gate is allocation-free by construction: it reads its arguments, does
arithmetic, and returns a value type. No garbage means no GC pressure on the
hot path, and latency that stays flat under load rather than degrading in
bursts.

## The risk gate

Every check, in precedence order. A halted engine rejects even a perfect
decision.

| Check | Rejects when |
|---|---|
| Kill switch | session drawdown breached the daily limit |
| Validation | malformed decision, non-finite or non-positive price |
| Veto | research layer declined, or proposed flat |
| Confidence | below the configured floor |
| Position limit | opening a new symbol beyond the concurrent cap |
| **Exposure cap** | **existing same-side exposure already at max notional** |
| Sizing | computed quantity rounds to zero |

The exposure check is the one worth calling out. Sizing each order in isolation
looks correct per-order while letting repeated same-side signals stack a
position far past its cap — every individual order compliant, the book quietly
not. The gate subtracts held exposure before sizing. `TestEvaluate` covers it
directly ("rejects stacking past the notional cap", "sizes only the remaining
headroom when partially filled").

Position size is fixed-fractional: risk a set slice of equity, let the stop
distance determine the quantity, then clamp to the notional cap.

## Tests

```
internal/risk    table-driven gate coverage, kill switch, concurrent access, benchmarks
internal/book    P&L correctness (long, short, partial close, averaging), fees, races
internal/engine  end-to-end flow, bracket exits, flips, halt propagation, shutdown
```

Two behaviors worth noting, both tested:

- **A bar that straddles stop and target is resolved as the stop.** Intrabar
  ordering is unknowable from OHLC, so the engine assumes the adverse fill.
  Choosing the favorable one would inflate every backtest that contains a
  volatile bar.
- **Recovering equity does not clear a halt.** Only an explicit `StartDay`
  does. A kill switch that silently resets is not a kill switch.

## Placing real orders

A `Venue` sits *behind* the risk gate: the engine evaluates, the gate sizes and
approves, and only then does an order reach a broker. Nothing else calls a
venue, so no code path can bypass risk.

```bash
go run ./cmd/engine -venue sim            # default: simulated fills, no account
go run ./cmd/engine -venue alpaca-paper   # real order lifecycle, no money
go run ./cmd/engine -venue alpaca-live -i-understand-this-trades-real-money
```

Credentials come from the environment, never flags — a flag lands in shell
history and in `ps` output:

```bash
export APCA_API_KEY_ID=...
export APCA_API_SECRET_KEY=...
```

### Safety properties

| Property | Why |
|---|---|
| Live needs **two** independent flags | one mistyped flag must never be the difference between paper and real money |
| Credentials are env-only | keeps keys out of shell history and process listings |
| Startup **reconciles** with the venue | the broker knows about fills from other sessions; trading against a stale local book would misprice every order |
| Reconcile failure **aborts** | refusing to start beats trading blind |
| Live mode logs a `WARN` banner | an operator should never have to infer that money is at stake |
| Orders carry an **idempotency key** | a timeout on a POST is ambiguous; the key means a retry cannot become a second position |
| 4xx is **never** retried | the request is wrong; repeating it cannot help |
| A failed close does **not** update the book | risk must not believe a position is flat while the broker still holds it |

### Venue constraints are enforced, not discovered

Alpaca spot crypto has three limits that would otherwise surface as rejected
orders on your account's record:

- **Symbols are `BTC/USD`**, not the engine's `BTC-USD` — translated on the wire
- **Time-in-force must be `gtc` or `ioc`** — `day` is rejected
- **No brackets and no shorting** — so `Preflight` blocks a sell with no
  inventory before it is sent, and the engine keeps monitoring stops itself.
  With no venue-side bracket, the engine is the *only* thing watching your
  stop; if the process dies, the position is unprotected.

`Capabilities()` advertises these, and the engine reads them rather than
assuming. A venue that supports shorting or brackets simply reports so.

### What is still simulated

`-venue sim` fills instantly at the mark with no order book, queue position,
partial fills, or rejects. That is what `alpaca-paper` exists to replace: same
code path, real order lifecycle, no money.

The wire format was written against Alpaca's published API and is covered by
tests using a stubbed transport, but it has **not** been smoke-tested against a
live paper account — do that before trusting it with anything.
