import Foundation

/// Wire types mirroring the Go execution engine's JSON.
///
/// These are decode-only value types: the dashboard observes the engine, it
/// never mutates it. Keeping them `Sendable` lets the polling actor hand
/// results to the main actor without copying concerns.

public enum Side: String, Codable, Sendable {
    case buy = "BUY"
    case sell = "SELL"
    case flat = "FLAT"

    public var isLong: Bool { self == .buy }
}

public struct Position: Codable, Sendable, Identifiable, Equatable {
    public let symbol: String
    public let qty: Double
    public let avgPrice: Double
    public let stopLoss: Double
    public let takeProfit: Double

    public var id: String { symbol }

    /// A short position is held when quantity is negative.
    public var side: Side { qty >= 0 ? .buy : .sell }

    /// Gross exposure at the position's own entry price.
    public var notional: Double { abs(qty) * avgPrice }

    /// Unrealized profit or loss at `price`.
    public func unrealized(at price: Double) -> Double {
        qty * (price - avgPrice)
    }

    enum CodingKeys: String, CodingKey {
        case symbol, qty
        case avgPrice = "avg_price"
        case stopLoss = "stop_loss"
        case takeProfit = "take_profit"
    }

    public init(symbol: String, qty: Double, avgPrice: Double,
                stopLoss: Double = 0, takeProfit: Double = 0) {
        self.symbol = symbol
        self.qty = qty
        self.avgPrice = avgPrice
        self.stopLoss = stopLoss
        self.takeProfit = takeProfit
    }
}

public struct PortfolioSnapshot: Codable, Sendable, Equatable {
    public let equity: Double
    public let cash: Double
    public let positions: [String: Position]

    /// Positions sorted by exposure, largest first -- the order a trader
    /// actually scans them in.
    public var rankedPositions: [Position] {
        positions.values.sorted { $0.notional > $1.notional }
    }

    public init(equity: Double, cash: Double, positions: [String: Position]) {
        self.equity = equity
        self.cash = cash
        self.positions = positions
    }
}

public struct EngineStats: Codable, Sendable, Equatable {
    public let ticks: UInt64
    public let decisions: UInt64
    public let fills: UInt64
    public let rejected: UInt64
    public let exits: UInt64
    public let halted: Bool
    public let equity: Double
    /// Where approved orders go: "sim", or a venue name like "alpaca-paper".
    public let venue: String
    /// True only when orders risk real money.
    public let live: Bool

    /// What the operator most needs to know about this engine, at a glance.
    public var mode: VenueMode {
        if live { return .live }
        return venue == "sim" ? .sim : .paper
    }

    /// Share of decisions the risk gate turned away.
    ///
    /// A high rejection rate is not a fault -- it is the gate working. Watching
    /// this number *shift* is what matters: a sudden drop can mean limits were
    /// loosened, a spike can mean the strategy is fighting its own risk config.
    public var rejectionRate: Double {
        decisions == 0 ? 0 : Double(rejected) / Double(decisions)
    }

    public init(ticks: UInt64 = 0, decisions: UInt64 = 0, fills: UInt64 = 0,
                rejected: UInt64 = 0, exits: UInt64 = 0,
                halted: Bool = false, equity: Double = 0,
                venue: String = "sim", live: Bool = false) {
        self.ticks = ticks
        self.decisions = decisions
        self.fills = fills
        self.rejected = rejected
        self.exits = exits
        self.halted = halted
        self.equity = equity
        self.venue = venue
        self.live = live
    }

    private enum CodingKeys: String, CodingKey {
        case ticks, decisions, fills, rejected, exits, halted, equity, venue, live
    }

    /// `venue` and `live` arrived after the first release of the engine API, so
    /// an older engine that omits them still decodes, reported as simulated.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ticks = try c.decode(UInt64.self, forKey: .ticks)
        decisions = try c.decode(UInt64.self, forKey: .decisions)
        fills = try c.decode(UInt64.self, forKey: .fills)
        rejected = try c.decode(UInt64.self, forKey: .rejected)
        exits = try c.decode(UInt64.self, forKey: .exits)
        halted = try c.decode(Bool.self, forKey: .halted)
        equity = try c.decode(Double.self, forKey: .equity)
        venue = try c.decodeIfPresent(String.self, forKey: .venue) ?? "sim"
        live = try c.decodeIfPresent(Bool.self, forKey: .live) ?? false
    }
}

/// Simulated fills, a broker's paper account, or real money.
public enum VenueMode: String, Sendable, Equatable {
    case sim = "SIM", paper = "PAPER", live = "LIVE"
}

public struct Health: Codable, Sendable, Equatable {
    public let status: String
    public let uptime: String
}

/// A point on the equity curve, sampled by the dashboard rather than reported
/// by the engine -- the engine has no reason to retain history it does not use.
public struct EquityPoint: Sendable, Identifiable, Equatable {
    public let id = UUID()
    public let time: Date
    public let equity: Double

    public init(time: Date, equity: Double) {
        self.time = time
        self.equity = equity
    }
}
