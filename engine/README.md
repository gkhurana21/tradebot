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
(a stale price is worse than a missing one); decisions block with a caller
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

| Endpoint | Purpose |
|---|---|
| `POST /tick` | market data; also drives stop/target monitoring |
| `POST /decision` | proposal in, risk verdict + sized order out |
| `GET /positions` | equity, cash, open positions |
| `GET /stats` | cumulative counters, halt state |
| `GET /health` | liveness |

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

## Not included

No live exchange connectivity. The `book` package is a simulator: it models
fees and adverse slippage but fills instantly at the mark, with no order book,
queue position, partial fills, or rejects. Wiring a real venue means
implementing an adapter behind the same interface — the risk gate and book are
already isolated from it.
