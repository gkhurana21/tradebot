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
        case .live: return "Live"
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

    private let client: EngineClient
    private let pollInterval: Duration
    private let historyLimit: Int
    private var pollTask: Task<Void, Never>?

    public init(client: EngineClient,
                pollInterval: Duration = .milliseconds(1000),
                historyLimit: Int = 300) {
        self.client = client
        self.pollInterval = pollInterval
        self.historyLimit = historyLimit
    }

    public var engineURL: URL { client.baseURL }

    /// True once the engine has ever answered, so the UI can distinguish
    /// "starting up" from "was live, now down".
    public var hasEverConnected: Bool { lastUpdate != nil }

    /// Equity change since the first observed sample, as a fraction.
    public var sessionReturn: Double {
        guard let first = equityHistory.first, first.equity > 0 else { return 0 }
        return (stats.equity - first.equity) / first.equity
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
