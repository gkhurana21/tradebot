// Test runner for TradebotKit.
//
// Written as a plain executable rather than an XCTest bundle so it runs with
// only the Command Line Tools installed -- same reasoning as build.sh, and it
// matches how the Python side runs its suite.
import Foundation
import TradebotKit

// MARK: - Tiny assertion harness

nonisolated(unsafe) var failures: [String] = []
nonisolated(unsafe) var checks = 0

func check(_ condition: Bool, _ message: String,
           file: StaticString = #file, line: UInt = #line) {
    checks += 1
    if !condition {
        failures.append("\(message)  (line \(line))")
    }
}

func checkEqual<T: Equatable>(_ got: T, _ want: T, _ message: String, line: UInt = #line) {
    checks += 1
    if got != want {
        failures.append("\(message): got \(got), want \(want)  (line \(line))")
    }
}

func checkClose(_ got: Double, _ want: Double, _ message: String,
                tolerance: Double = 1e-9, line: UInt = #line) {
    checks += 1
    if abs(got - want) > tolerance {
        failures.append("\(message): got \(got), want \(want)  (line \(line))")
    }
}

func suite(_ name: String, _ body: () async throws -> Void) async {
    do {
        try await body()
        print("  PASS  \(name)")
    } catch {
        failures.append("\(name): threw \(error)")
        print("  FAIL  \(name) — \(error)")
    }
}

// MARK: - Test doubles

/// Replays canned responses so decoding and error mapping are testable with no
/// server, no sockets, and no timing.
struct StubTransport: HTTPTransport {
    let responses: [String: (Data, Int)]

    func get(_ url: URL) async throws -> (Data, Int) {
        guard let hit = responses[url.lastPathComponent] else {
            throw EngineError.unreachable("no stub for \(url.lastPathComponent)")
        }
        return hit
    }

    func post(_ url: URL, body: Data) async throws -> (Data, Int) {
        try await get(url)
    }
}

let statsJSON = """
{"ticks":10,"decisions":8,"fills":5,"rejected":3,"exits":1,"halted":false,"equity":10250.5}
"""

let positionsJSON = """
{"equity":10250.5,"cash":9000.25,"positions":{
  "BTC-USD":{"symbol":"BTC-USD","qty":0.5,"avg_price":64000,"stop_loss":62720,"take_profit":66560},
  "ETH-USD":{"symbol":"ETH-USD","qty":-2.0,"avg_price":3000,"stop_loss":3060,"take_profit":2880}
}}
"""

func makeClient(stats: String = statsJSON,
                positions: String = positionsJSON,
                status: Int = 200) -> EngineClient {
    EngineClient(
        baseURL: URL(string: "http://127.0.0.1:8080")!,
        transport: StubTransport(responses: [
            "stats": (Data(stats.utf8), status),
            "positions": (Data(positions.utf8), status),
        ])
    )
}

// MARK: - Tests

print("TradebotKit tests")

await suite("decodes engine stats") {
    let stats = try await makeClient().stats()
    checkEqual(stats.fills, 5, "fills")
    checkEqual(stats.rejected, 3, "rejected")
    checkEqual(stats.halted, false, "halted")
    checkClose(stats.equity, 10250.5, "equity")
    // 3 of 8 decisions turned away.
    checkClose(stats.rejectionRate, 0.375, "rejection rate")
}

await suite("decodes positions with snake_case keys") {
    let portfolio = try await makeClient().portfolio()
    checkEqual(portfolio.positions.count, 2, "position count")

    guard let btc = portfolio.positions["BTC-USD"] else {
        throw EngineError.decoding("missing BTC-USD")
    }
    checkClose(btc.stopLoss, 62720, "stop_loss decoded")
    checkClose(btc.takeProfit, 66560, "take_profit decoded")
    checkEqual(btc.side, .buy, "positive qty is long")

    guard let eth = portfolio.positions["ETH-USD"] else {
        throw EngineError.decoding("missing ETH-USD")
    }
    checkEqual(eth.side, .sell, "negative qty is short")
}

await suite("ranks positions by exposure") {
    let portfolio = try await makeClient().portfolio()
    let ranked = portfolio.rankedPositions
    // BTC: 0.5 * 64000 = 32000. ETH: 2 * 3000 = 6000.
    checkEqual(ranked.first?.symbol, "BTC-USD", "largest exposure first")
    checkClose(ranked[0].notional, 32000, "btc notional")
    checkClose(ranked[1].notional, 6000, "eth notional")
}

await suite("computes unrealized P&L in both directions") {
    let long = Position(symbol: "X", qty: 2, avgPrice: 100)
    checkClose(long.unrealized(at: 110), 20, "long profits when price rises")
    checkClose(long.unrealized(at: 90), -20, "long loses when price falls")

    let short = Position(symbol: "X", qty: -2, avgPrice: 100)
    checkClose(short.unrealized(at: 90), 20, "short profits when price falls")
    checkClose(short.unrealized(at: 110), -20, "short loses when price rises")
}

await suite("maps a non-2xx response to a status error") {
    do {
        _ = try await makeClient(status: 500).stats()
        check(false, "expected a badStatus error")
    } catch let error as EngineError {
        checkEqual(error, .badStatus(500), "error case")
    }
}

await suite("maps malformed JSON to a decoding error") {
    do {
        _ = try await makeClient(stats: "{not json").stats()
        check(false, "expected a decoding error")
    } catch let error as EngineError {
        if case .decoding = error {} else {
            check(false, "expected .decoding, got \(error)")
        }
    }
}

await suite("surfaces an unreachable engine") {
    let client = EngineClient(
        baseURL: URL(string: "http://127.0.0.1:8080")!,
        transport: StubTransport(responses: [:])
    )
    do {
        _ = try await client.stats()
        check(false, "expected an unreachable error")
    } catch let error as EngineError {
        if case .unreachable = error {} else {
            check(false, "expected .unreachable, got \(error)")
        }
    }
}

await suite("model accumulates equity history and derives session stats") { @MainActor in
    let model = DashboardModel(client: makeClient(), historyLimit: 4)
    let t0 = Date()

    @MainActor func push(_ equity: Double, _ offset: TimeInterval) {
        model.apply(
            stats: EngineStats(decisions: 1, equity: equity),
            portfolio: PortfolioSnapshot(equity: equity, cash: equity, positions: [:]),
            at: t0.addingTimeInterval(offset)
        )
    }

    push(10_000, 0)
    push(10_500, 1) // peak
    push(9_975, 2)  // -5% from peak
    checkEqual(model.connection, .live, "connection goes live after a snapshot")
    checkClose(model.sessionReturn, -0.0025, "session return from first sample")
    checkClose(model.observedDrawdown, 0.05, "drawdown measured peak to trough")

    // History is capped, keeping the most recent samples.
    push(10_100, 3)
    push(10_200, 4)
    checkEqual(model.equityHistory.count, 4, "history trimmed to limit")
    checkClose(model.equityHistory.last?.equity ?? 0, 10_200, "newest sample retained")
    checkClose(model.equityHistory.first?.equity ?? 0, 10_500, "oldest sample dropped")
}

await suite("equity chart range hugs the data instead of starting at zero") { @MainActor in
    let model = DashboardModel(client: makeClient())
    let t0 = Date()
    @MainActor func push(_ equity: Double, _ offset: TimeInterval) {
        model.apply(
            stats: EngineStats(decisions: 1, equity: equity),
            portfolio: PortfolioSnapshot(equity: equity, cash: equity, positions: [:]),
            at: t0.addingTimeInterval(offset)
        )
    }

    checkEqual(model.equityDomain, 0...1, "no samples gives a placeholder range")

    push(25_000, 0)
    push(25_400, 1)
    push(24_800, 2)
    let domain = model.equityDomain
    check(domain.lowerBound > 20_000, "range does not reach down toward zero")
    check(domain.lowerBound < 24_800 && domain.upperBound > 25_400, "range contains every sample")
    checkClose(domain.upperBound - domain.lowerBound, 600 * 1.3, "15% headroom each side of the span")

    // A flat session keeps a sane minimum height instead of zooming into noise.
    let flat = DashboardModel(client: makeClient())
    flat.apply(stats: EngineStats(decisions: 0, equity: 25_000),
               portfolio: PortfolioSnapshot(equity: 25_000, cash: 25_000, positions: [:]),
               at: t0)
    checkClose(flat.equityDomain.upperBound - flat.equityDomain.lowerBound, 50,
               "flat range padded by 0.1% of equity each side")
}

await suite("model tolerates one dropped poll before reporting failure") { @MainActor in
    let offline = EngineClient(
        baseURL: URL(string: "http://127.0.0.1:8080")!,
        transport: StubTransport(responses: [:])
    )
    let model = DashboardModel(client: offline)

    await model.refresh()
    checkEqual(model.connection, .connecting, "a single miss is not an outage")
    checkEqual(model.consecutiveFailures, 1, "failure counted")

    await model.refresh()
    check(!model.connection.isLive, "sustained failure reports disconnected")
    checkEqual(model.consecutiveFailures, 2, "failures accumulate")
}

await suite("recovery clears the failure state") { @MainActor in
    let model = DashboardModel(client: makeClient())
    await model.refresh() // stub has data, so this succeeds
    checkEqual(model.connection, .live, "connected")
    checkEqual(model.consecutiveFailures, 0, "counter reset")
    check(model.hasEverConnected, "lastUpdate recorded")
}

// MARK: - Summary

print()
if failures.isEmpty {
    print("\(checks) checks passed")
    exit(0)
} else {
    print("\(failures.count) failure(s) of \(checks) checks:")
    for failure in failures { print("  - \(failure)") }
    exit(1)
}
