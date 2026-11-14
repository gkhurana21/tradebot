# Tradebot Dashboard (SwiftUI)

A native macOS monitor for the Go execution engine. Polls the engine once a
second and renders equity, the risk gate's decision feed, and open positions.

```
  Go execution engine  ──HTTP──▶  SwiftUI dashboard
  :8080 /stats /positions          hero equity · equity curve · decision feed · positions
        /decisions?since=
```

## Run it

```bash
./build.sh          # compile (needs only Command Line Tools)
./test.sh           # 24 suites, 116 checks
./run.sh            # launch, watching http://127.0.0.1:8080
./run.sh --url http://host:9000    # watch a remote engine
```

With the engine running (`cd ../engine && go run ./cmd/engine`), the dashboard
comes up live. Without it, the status pill reads **Reconnecting…** and the last
known book stays on screen, dimmed.

The window is laid out around the engine's API v2: the **Decisions** feed shows
each proposal with the gate's verdict (blue Approved, orange Blocked) and its
reason; red is reserved for losses, HALTED and LIVE. `/decisions` is polled on
its own path, so an older engine (404) just shows "This engine doesn't publish
decisions" and stays connected. New engine fields (`session_start_equity`,
`limits`, position `mark`/`unrealized_pnl`) decode with defaults.

`build.sh` fails if swiftc reports any "only available in macOS" warning, so
APIs newer than macOS 14 cannot slip in as Swift 5-mode warnings.

Headless connectivity check, no window required — useful over SSH or in CI:

```bash
./.build/TradebotDashboard.app/Contents/MacOS/TradebotDashboard --probe
```
```
engine:    http://127.0.0.1:8080
equity:    24985.00
halted:    false
fills:     2   rejected: 0   exits: 0
positions:
  LONG  BTC-USD   qty 0.078125 @ 64032.00
  SHORT ETH-USD   qty -1.666167 @ 2998.50
```

## Design

**The UI layer is thin on purpose.** Everything worth testing — decoding,
polling, retry, derived metrics — lives in `TradebotKit`, so it is verified by
a real test suite rather than by looking at a window. `Sources/TradebotDashboard`
only arranges views.

**Read-only by construction.** The client exposes no way to place a trade. A
monitoring tool that can send orders is a monitoring tool that can fat-finger
the book, so the capability simply is not there.

**Transport is a protocol, not a concretion.** `HTTPTransport` lets the tests
replay canned responses, so error mapping and decoding are checked without a
server, a socket, or a timing dependency.

**A blip does not blank the screen.** One dropped poll is tolerated silently;
sustained failure flips the pill to Disconnected while the last known book
stays visible. An operator needs stale-but-labeled data far more than zeros.

**Polling outlives the window.** The model is owned by the app, not a view, so
equity history keeps accumulating and does not reset when a window closes.

Swift 6 language mode, strict concurrency clean: models are `Sendable` value
types, the client is a `Sendable` struct, and all observable state is
`@MainActor`-isolated.

## Layout

| Path | Role |
|---|---|
| `Sources/TradebotKit/Models.swift` | wire types mirroring the engine's JSON |
| `Sources/TradebotKit/EngineClient.swift` | async client + transport seam |
| `Sources/TradebotKit/DashboardModel.swift` | `@Observable` state, polling, derived metrics |
| `Sources/TradebotDashboard/App.swift` | app lifecycle, `--probe`, `--url` |
| `Sources/TradebotDashboard/DashboardView.swift` | views, Swift Charts equity curve |
| `Tests/TradebotKitTests/` | 10 suites, 31 checks |

## A note on the build

`build.sh` drives `swiftc` directly instead of `swift build`. SwiftPM requires
a full Xcode install — the Command Line Tools ship a `PackageDescription`
library that cannot compile its own manifests (`swift package init` output
fails too), so `swift build` is unusable on a CLT-only machine.

Building with `swiftc` means the app compiles on a stock macOS box with no
Xcode. `Package.swift` is kept for anyone who does have Xcode, but note it was
not verified here — SwiftPM could not run at all on this machine.

The script also packages a real `.app` bundle. macOS only grants a window to a
bundled app; a bare executable launches, runs, and silently shows nothing.
