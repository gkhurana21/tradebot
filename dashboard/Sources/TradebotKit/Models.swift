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
                halted: Bool = false, equity: Double = 0) {
        self.ticks = ticks
        self.decisions = decisions
        self.fills = fills
        self.rejected = rejected
        self.exits = exits
        self.halted = halted
        self.equity = equity
    }
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
