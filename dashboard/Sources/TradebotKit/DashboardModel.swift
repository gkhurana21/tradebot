import Foundation
import Observation

/// Connection state, shown as a status pill in the UI.
public enum ConnectionState: Equatable, Sendable {
    case connecting
    case live
    case failing(String)

    public var isLive: Bool { self == .live }

    public var label: String {
        switch self {
        case .connecting: return "Connecting"
        case .live: return "Connected"
        case .failing: return "Disconnected"
        }
    }
}

/// Observable state backing the dashboard.
///
/// Isolated to the main actor because every property here is read by SwiftUI.
/// Network work happens inside `refresh()`, which suspends rather than blocks,
/// so the UI stays responsive without a second actor to reason about.
@MainActor
@Observable
public final class DashboardModel {
    public private(set) var stats = EngineStats()
    public private(set) var portfolio = PortfolioSnapshot(equity: 0, cash: 0, positions: [:])
    public private(set) var equityHistory: [EquityPoint] = []
    public private(set) var connection: ConnectionState = .connecting
    public private(set) var lastUpdate: Date?

    /// Retained so a transient blip does not blank the screen -- showing the
    /// last known book with a "Disconnected" pill is far more useful to an
    /// operator than showing zeros.
    public private(set) var consecutiveFailures = 0

    /// Newest first, capped at `decisionLimit`.
    public private(set) var decisions: [DecisionItem] = []
    /// False once the engine answers 404 for /decisions: an older engine, not a
    /// broken one. Unknown (nil) until the first answer.
    public private(set) var decisionsSupported: Bool?
    /// True if the engine's ring overwrote entries before we read them.
    public private(set) var decisionsGap = false
    /// Next `since` to ask for; nil means "everything retained".
    public private(set) var decisionCursor: UInt64?

    private let client: EngineClient
    private let pollInterval: Duration
    private let historyLimit: Int
    private let decisionLimit: Int
    private var pollTask: Task<Void, Never>?

    public init(client: EngineClient,
                pollInterval: Duration = .milliseconds(1000),
                historyLimit: Int = 300,
                decisionLimit: Int = 50) {
        self.client = client
        self.decisionLimit = decisionLimit
        self.pollInterval = pollInterval
        self.historyLimit = historyLimit
    }

    public var engineURL: URL { client.baseURL }

    /// True once the engine has ever answered, so the UI can distinguish
    /// "starting up" from "was live, now down".
    public var hasEverConnected: Bool { lastUpdate != nil }

    /// Equity at the start of the session. Prefers the engine's own baseline
    /// (the one its kill switch uses, and which survives a dashboard relaunch);
    /// falls back to the first sample this dashboard saw for an older engine.
    public var sessionStartEquity: Double? {
        if let start = stats.sessionStartEquity, start > 0 { return start }
        if let first = equityHistory.first, first.equity > 0 { return first.equity }
        return nil
    }

    /// Dollar change since the session start.
    public var sessionChange: Double {
        guard let start = sessionStartEquity else { return 0 }
        return stats.equity - start
    }

    /// Equity change since the session start, as a fraction.
    public var sessionReturn: Double {
        guard let start = sessionStartEquity else { return 0 }
        return (stats.equity - start) / start
    }

    /// Loss from the session start as a positive fraction, 0 when up.
    public var dailyLoss: Double { max(0, -sessionReturn) }

    /// Share of the kill-switch limit already used, 0...1, if the limit is known.
    public var dailyLossUsed: Double? {
        guard let limit = stats.limits?.maxDailyDrawdown, limit > 0 else { return nil }
        return min(dailyLoss / limit, 1)
    }

    /// Y-axis range for the equity chart: the observed low and high plus some
    /// headroom. A zero baseline would flatten a $25k account's moves into a
    /// straight line; a floor on the padding keeps a flat session from
    /// zooming in until rounding noise looks like volatility.
    public var equityDomain: ClosedRange<Double> {
        let values = equityHistory.map(\.equity)
        guard let low = values.min(), let high = values.max() else { return 0...1 }
        let pad = max((high - low) * 0.15, abs(high) * 0.001, 1)
        return (low - pad)...(high + pad)
    }

    /// Peak-to-trough decline across the observed window.
    public var observedDrawdown: Double {
        var peak = -Double.infinity
        var worst = 0.0
        for point in equityHistory {
            peak = max(peak, point.equity)
            if peak > 0 {
                worst = max(worst, (peak - point.equity) / peak)
            }
        }
        return worst
    }

    /// Begins polling. Safe to call repeatedly; only the first call starts a
    /// task, so a view that re-appears does not spawn a second loop.
    public func start() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            // Holding self weakly and bailing when it is gone means the loop
            // ends with the model; there is no deinit to race with.
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(for: self.pollInterval)
            }
        }
    }

    public func stop() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Performs one poll cycle. Exposed so tests can drive the model
    /// deterministically instead of waiting on wall-clock time.
    public func refresh() async {
        // The journal is fetched on its own path: its failure must never
        // decide whether the engine counts as connected, and vice versa.
        async let journal: Void = refreshDecisions()
        await refreshSnapshot()
        await journal
    }

    /// Polls the decision journal from the saved cursor. Errors are swallowed
    /// on purpose; a 404 marks the feed unsupported, anything else retries.
    public func refreshDecisions() async {
        do {
            applyDecisions(try await client.decisions(since: decisionCursor))
        } catch EngineError.badStatus(404) {
            decisionsSupported = false
        } catch {
            // Transient: the snapshot path reports connectivity.
        }
    }

    /// Merges a journal page. Separate from transport for deterministic tests.
    public func applyDecisions(_ page: DecisionPage) {
        decisionsSupported = true
        if let cursor = decisionCursor, page.nextSeq < cursor {
            // The engine restarted and its sequence began again. Forget the
            // old feed and read the new one from the start.
            decisions = []
            decisionsGap = false
            decisionCursor = nil
            return
        }
        if page.gap { decisionsGap = true }
        let known = Set(decisions.map(\.seq))
        let fresh = page.items.filter { !known.contains($0.seq) }
        decisions = (fresh + decisions).sorted { $0.seq > $1.seq }
        if decisions.count > decisionLimit {
            decisions.removeLast(decisions.count - decisionLimit)
        }
        decisionCursor = page.nextSeq
    }

    private func refreshSnapshot() async {
        do {
            let (stats, portfolio) = try await client.snapshot()
            apply(stats: stats, portfolio: portfolio, at: Date())
        } catch {
            consecutiveFailures += 1
            // Tolerate a single dropped poll: one missed request during a
            // redeploy should not read as an outage.
            if consecutiveFailures > 1 {
                connection = .failing(error.localizedDescription)
            }
        }
    }

    /// Applies a fresh snapshot. Separated from transport so tests can verify
    /// history trimming and derived values without any networking.
    public func apply(stats: EngineStats, portfolio: PortfolioSnapshot, at time: Date) {
        self.stats = stats
        self.portfolio = portfolio
        self.lastUpdate = time
        self.consecutiveFailures = 0
        self.connection = .live

        equityHistory.append(EquityPoint(time: time, equity: stats.equity))
        if equityHistory.count > historyLimit {
            equityHistory.removeFirst(equityHistory.count - historyLimit)
        }
    }
}
