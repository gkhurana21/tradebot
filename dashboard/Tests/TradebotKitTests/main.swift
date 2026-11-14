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

await suite("stats report where orders go, and older engines still decode") {
    let old = try JSONDecoder().decode(EngineStats.self, from: Data(statsJSON.utf8))
    checkEqual(old.mode, .sim, "an engine without venue fields reads as simulated")

    let paper = try JSONDecoder().decode(EngineStats.self, from: Data("""
    {"ticks":1,"decisions":1,"fills":1,"rejected":0,"exits":0,"halted":false,"equity":1,
     "venue":"alpaca-paper","live":false}
    """.utf8))
    checkEqual(paper.mode, .paper, "a non-sim venue without real money is paper")

    let live = try JSONDecoder().decode(EngineStats.self, from: Data("""
    {"ticks":1,"decisions":1,"fills":1,"rejected":0,"exits":0,"halted":false,"equity":1,
     "venue":"alpaca-live","live":true}
    """.utf8))
    checkEqual(live.mode, .live, "live is reported as live")
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

// MARK: - Engine API v2: decisions, marks, session baseline

/// Records every requested URL so cursor handling can be asserted.
final class RecordingTransport: HTTPTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [URL] = []
    let responses: [String: (Data, Int)]
    init(_ responses: [String: (Data, Int)]) { self.responses = responses }
    var urls: [URL] { lock.lock(); defer { lock.unlock() }; return recorded }

    private func record(_ url: URL) {
        lock.lock(); defer { lock.unlock() }
        recorded.append(url)
    }

    func get(_ url: URL) async throws -> (Data, Int) {
        record(url)
        guard let hit = responses[url.lastPathComponent] else {
            throw EngineError.unreachable("no stub for \(url.lastPathComponent)")
        }
        return hit
    }
    func post(_ url: URL, body: Data) async throws -> (Data, Int) { try await get(url) }
}

let statsV2JSON = """
{"ticks":10,"decisions":8,"fills":5,"rejected":3,"exits":1,"halted":false,"equity":25100,
 "venue":"sim","live":false,"session_start_equity":25000,
 "limits":{"max_daily_drawdown_pct":0.05,"max_open_positions":3,"min_confidence":0.55,"max_position_pct":0.2}}
"""

let decisionsJSON = """
{"next_seq":5,"oldest_seq":1,"gap":false,"items":[
 {"seq":1,"at":"2026-11-14T21:16:31.586526Z","kind":"decision","symbol":"BTC-USD","side":"BUY","confidence":0.85,
  "verdict":"approved","approved":true,"reason":"sized 0.0780 BTC-USD · risk 0.4% to stop","qty":0.078,"price":64098.5},
 {"seq":2,"at":"2026-11-14T21:16:34Z","kind":"decision","symbol":"ETH-USD","side":"BUY","confidence":0.3,
  "verdict":"rejected","approved":false,"reason":"confidence 0.30 < min 0.55"},
 {"seq":3,"at":"2026-11-14T21:16:35.123456789Z","kind":"exit","symbol":"BTC-USD","side":"SELL","reason":"stop","qty":0.078,"price":62700},
 {"seq":4,"at":"2026-11-14T21:16:36.5+00:00","kind":"halt","reason":"halted: daily drawdown 5.2% ≥ 5%"}
]}
"""

func decisionClient(_ transport: HTTPTransport) -> EngineClient {
    EngineClient(baseURL: URL(string: "http://127.0.0.1:8080")!, transport: transport)
}

func item(_ seq: UInt64, approved: Bool = true) -> DecisionItem {
    DecisionItem(seq: seq, at: Date(), symbol: "BTC-USD", side: "BUY", confidence: 0.8,
                 verdict: approved ? "approved" : "rejected", approved: approved, reason: "r\(seq)")
}

await suite("ISO-8601 parsing accepts nanoseconds, microseconds and no fraction") {
    let whole = ISO8601.parse("2026-11-14T21:16:31Z")
    check(whole != nil, "no-fraction form parses")
    let micro = ISO8601.parse("2026-11-14T21:16:31.586526Z")
    let nano = ISO8601.parse("2026-11-14T21:16:31.586526789Z")
    let trimmed = ISO8601.parse("2026-11-14T21:16:31.5Z")
    check(micro != nil && nano != nil && trimmed != nil, "fractional forms parse")
    if let whole, let micro, let nano, let trimmed {
        checkClose(micro.timeIntervalSince(whole), 0.586526, "microsecond fraction kept", tolerance: 1e-4)
        checkClose(nano.timeIntervalSince(whole), 0.586526789, "nanosecond fraction kept", tolerance: 1e-4)
        checkClose(trimmed.timeIntervalSince(whole), 0.5, "short fraction kept", tolerance: 1e-4)
    }
    checkEqual(ISO8601.parse("2026-11-14T21:16:31+00:00"), whole, "numeric offset parses")
    check(ISO8601.parse("yesterday") == nil, "garbage rejected")
}

await suite("decodes /decisions with every kind and date form") {
    let transport = RecordingTransport(["decisions": (Data(decisionsJSON.utf8), 200)])
    let page = try await decisionClient(transport).decisions()
    checkEqual(page.nextSeq, 5, "next_seq")
    checkEqual(page.items.count, 4, "items")
    checkEqual(page.gap, false, "gap")
    checkEqual(page.items[0].kind, .decision, "decision kind")
    check(page.items[0].isApproved, "approved decision")
    check(!page.items[1].isApproved, "rejected decision is not approved")
    checkEqual(page.items[2].kind, .exit, "exit kind")
    checkEqual(page.items[3].kind, .halt, "halt kind")
    checkEqual(page.items[3].symbol, nil, "halt has no symbol")
    checkClose(page.items[0].confidence ?? 0, 0.85, "confidence")
    check(page.items[2].at.timeIntervalSince(page.items[1].at) > 0.9, "later item has later date")
    checkEqual(transport.urls.last?.query, nil, "no cursor means no query string")
}

await suite("humanises the gate's reasons and keeps unknown ones") {
    checkEqual(item(1).displayReason, "R1", "unknown reason is capitalised")
    let low = DecisionItem(seq: 1, at: Date(), reason: "confidence 0.30 < min 0.55")
    checkEqual(low.displayReason, "Confidence 30% is below the 55% minimum", "confidence")
    let slots = DecisionItem(seq: 1, at: Date(), reason: "max open positions 3 reached")
    checkEqual(slots.displayReason, "3 of 3 position slots in use", "slots")
    let cap = DecisionItem(seq: 1, at: Date(), symbol: "BTC-USD", reason: "position at max notional 20%")
    checkEqual(cap.displayReason, "BTC already at the 20% max position size", "exposure cap")
    let halt = DecisionItem(seq: 1, at: Date(), reason: "halted: daily drawdown 5.2% ≥ 5%")
    checkEqual(halt.displayReason, "Kill switch active (5.2% daily loss, limit 5%)", "halt")
}

await suite("feed advances its since-cursor and de-duplicates") { @MainActor in
    let transport = RecordingTransport([
        "decisions": (Data(decisionsJSON.utf8), 200),
        "stats": (Data(statsV2JSON.utf8), 200),
        "positions": (Data(positionsJSON.utf8), 200),
    ])
    let model = DashboardModel(client: decisionClient(transport))
    await model.refresh()
    checkEqual(model.decisions.count, 4, "first poll loads the journal")
    checkEqual(model.decisions.first?.seq, 4, "newest first")
    checkEqual(model.decisionCursor, 5, "cursor is the page's next_seq")
    checkEqual(model.decisionsSupported, true, "supported")

    await model.refresh() // the stub replays the same page
    checkEqual(model.decisions.count, 4, "replayed items are not duplicated")
    let asked = transport.urls.filter { $0.lastPathComponent == "decisions" }
    checkEqual(asked.count, 2, "polled twice")
    checkEqual(asked[0].query, nil, "first poll has no cursor")
    checkEqual(asked[1].query, "since=5", "second poll asks since the cursor")
}

await suite("feed keeps the newest 50 and flags gaps") { @MainActor in
    let model = DashboardModel(client: makeClient())
    model.applyDecisions(DecisionPage(nextSeq: 61, items: (1...60).map { item(UInt64($0)) }))
    checkEqual(model.decisions.count, 50, "capped at 50")
    checkEqual(model.decisions.first?.seq, 60, "newest kept")
    checkEqual(model.decisions.last?.seq, 11, "oldest dropped")
    check(!model.decisionsGap, "no gap yet")

    model.applyDecisions(DecisionPage(nextSeq: 400, oldestSeq: 300, gap: true,
                                      items: [item(300), item(301)]))
    check(model.decisionsGap, "gap remembered")
    checkEqual(model.decisions.first?.seq, 301, "items after the gap are still shown")
    checkEqual(model.decisionCursor, 400, "cursor jumps to next_seq")
}

await suite("an engine restart resets the feed") { @MainActor in
    let model = DashboardModel(client: makeClient())
    model.applyDecisions(DecisionPage(nextSeq: 10, items: [item(8), item(9)]))
    model.applyDecisions(DecisionPage(nextSeq: 1, items: []))
    checkEqual(model.decisions.count, 0, "stale feed dropped")
    checkEqual(model.decisionCursor, nil, "cursor cleared so the next poll reads from the start")
}

await suite("a 404 on /decisions does not disconnect an older engine") { @MainActor in
    let transport = RecordingTransport([
        "decisions": (Data("404 page not found".utf8), 404),
        "stats": (Data(statsJSON.utf8), 200),
        "positions": (Data(positionsJSON.utf8), 200),
    ])
    let model = DashboardModel(client: decisionClient(transport))
    await model.refresh()
    checkEqual(model.connection, .live, "still connected")
    checkEqual(model.decisionsSupported, false, "feed marked unsupported")
    checkEqual(model.consecutiveFailures, 0, "no failure counted")
    checkEqual(model.stats.sessionStartEquity, nil, "old stats decode without v2 fields")
    checkEqual(model.stats.limits, nil, "old stats have no limits")
}

await suite("a failing /decisions (500) does not disconnect either") { @MainActor in
    let transport = RecordingTransport([
        "decisions": (Data("{}".utf8), 500),
        "stats": (Data(statsV2JSON.utf8), 200),
        "positions": (Data(positionsJSON.utf8), 200),
    ])
    let model = DashboardModel(client: decisionClient(transport))
    await model.refresh()
    checkEqual(model.connection, .live, "still connected")
    checkEqual(model.decisionsSupported, nil, "support unknown, will retry")
}

await suite("positions decode mark and unrealized P&L, with or without them") {
    let v2 = try JSONDecoder().decode(PortfolioSnapshot.self, from: Data("""
    {"equity":1,"cash":1,"positions":{"BTC-USD":{"symbol":"BTC-USD","qty":0.5,"avg_price":64000,
     "stop_loss":62720,"take_profit":66560,"mark":64500,"unrealized_pnl":250}}}
    """.utf8))
    let btc = v2.positions["BTC-USD"]
    checkClose(btc?.mark ?? 0, 64500, "mark")
    checkClose(btc?.unrealizedPnL ?? 0, 250, "unrealized pnl")
    checkClose(btc?.unrealizedFraction ?? 0, 250.0 / 32000.0, "pnl as share of cost")

    let old = try JSONDecoder().decode(PortfolioSnapshot.self, from: Data(positionsJSON.utf8))
    checkEqual(old.positions["BTC-USD"]?.mark, nil, "older engine: no mark")
    checkEqual(old.positions["BTC-USD"]?.unrealizedPnL, nil, "older engine: no pnl")

    // The engine reports mark 0 until a symbol's first tick.
    let unticked = try JSONDecoder().decode(PortfolioSnapshot.self, from: Data("""
    {"equity":1,"cash":1,"positions":{"X":{"symbol":"X","qty":1,"avg_price":10,
     "stop_loss":9,"take_profit":12,"mark":0,"unrealized_pnl":0}}}
    """.utf8))
    checkEqual(unticked.positions["X"]?.mark, nil, "zero mark reads as unknown")
    checkEqual(unticked.positions["X"]?.unrealizedPnL, nil, "and so does its pnl")
}

await suite("stop-to-target bar puts the stop on the left for longs and shorts") {
    let long = Position(symbol: "L", qty: 1, avgPrice: 100, stopLoss: 90, takeProfit: 120)
    checkClose(long.bracketFraction(at: 90) ?? -1, 0, "long at stop")
    checkClose(long.bracketFraction(at: 120) ?? -1, 1, "long at target")
    checkClose(long.bracketFraction(at: 100) ?? -1, 1.0 / 3.0, "long entry")
    let short = Position(symbol: "S", qty: -1, avgPrice: 100, stopLoss: 110, takeProfit: 80)
    checkClose(short.bracketFraction(at: 110) ?? -1, 0, "short at stop is still the left end")
    checkClose(short.bracketFraction(at: 80) ?? -1, 1, "short at target is the right end")
    checkClose(short.bracketFraction(at: 200) ?? -1, 0, "clamped beyond the stop")
    check(Position(symbol: "N", qty: 1, avgPrice: 1).bracketFraction(at: 1) == nil, "no bracket, no bar")
    checkClose(long.distanceToStop(from: 100) ?? 0, 0.10, "distance to stop")
    checkClose(long.distanceToTarget(from: 100) ?? 0, 0.20, "distance to target")
}

await suite("stats decode the session baseline and limits (fractions)") {
    let s = try JSONDecoder().decode(EngineStats.self, from: Data(statsV2JSON.utf8))
    checkClose(s.sessionStartEquity ?? 0, 25000, "session_start_equity")
    checkClose(s.limits?.maxDailyDrawdown ?? 0, 0.05, "drawdown limit is a fraction")
    checkEqual(s.limits?.maxOpenPositions, 3, "position slots")
    checkClose(s.limits?.minConfidence ?? 0, 0.55, "min confidence")
    checkClose(s.limits?.maxPositionFraction ?? 0, 0.2, "max position")
}

await suite("session change uses the engine's baseline, not the first sample") { @MainActor in
    let model = DashboardModel(client: makeClient())
    let t0 = Date()
    let limits = RiskLimits(maxDailyDrawdown: 0.05, maxOpenPositions: 3,
                            minConfidence: 0.55, maxPositionFraction: 0.2)
    // The dashboard was opened late: its first sample is already 24,900, but
    // the engine's session began at 25,000.
    model.apply(stats: EngineStats(equity: 24_900, sessionStartEquity: 25_000, limits: limits),
                portfolio: PortfolioSnapshot(equity: 24_900, cash: 24_900, positions: [:]), at: t0)
    checkClose(model.sessionChange, -100, "dollar change from engine baseline")
    checkClose(model.sessionReturn, -0.004, "fractional change from engine baseline")
    checkClose(model.dailyLoss, 0.004, "daily loss is positive")
    checkClose(model.dailyLossUsed ?? -1, 0.08, "8% of the kill-switch limit used")

    model.apply(stats: EngineStats(equity: 25_250, sessionStartEquity: 25_000, limits: limits),
                portfolio: PortfolioSnapshot(equity: 25_250, cash: 25_250, positions: [:]),
                at: t0.addingTimeInterval(1))
    checkClose(model.sessionChange, 250, "gain")
    checkClose(model.dailyLoss, 0, "no daily loss when up")

    // Older engine: fall back to the first sample this dashboard saw.
    let old = DashboardModel(client: makeClient())
    old.apply(stats: EngineStats(equity: 10_000),
              portfolio: PortfolioSnapshot(equity: 10_000, cash: 0, positions: [:]), at: t0)
    old.apply(stats: EngineStats(equity: 10_100),
              portfolio: PortfolioSnapshot(equity: 10_100, cash: 0, positions: [:]),
              at: t0.addingTimeInterval(1))
    checkClose(old.sessionChange, 100, "fallback change")
    checkEqual(old.dailyLossUsed, nil, "no limits, no meter")
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
