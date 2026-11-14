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
    /// Latest price and open P&L as the engine reports them (API v2). Both are
    /// nil from an older engine, which the UI shows as "—" rather than zero.
    public let mark: Double?
    public let unrealizedPnL: Double?

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
        case mark
        case unrealizedPnL = "unrealized_pnl"
    }

    public init(symbol: String, qty: Double, avgPrice: Double,
                stopLoss: Double = 0, takeProfit: Double = 0,
                mark: Double? = nil, unrealizedPnL: Double? = nil) {
        self.symbol = symbol
        self.qty = qty
        self.avgPrice = avgPrice
        self.stopLoss = stopLoss
        self.takeProfit = takeProfit
        self.mark = mark
        self.unrealizedPnL = unrealizedPnL
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol = try c.decode(String.self, forKey: .symbol)
        qty = try c.decode(Double.self, forKey: .qty)
        avgPrice = try c.decode(Double.self, forKey: .avgPrice)
        stopLoss = try c.decodeIfPresent(Double.self, forKey: .stopLoss) ?? 0
        takeProfit = try c.decodeIfPresent(Double.self, forKey: .takeProfit) ?? 0
        // The engine omits nothing today, but a position whose symbol has no
        // tick yet reports mark 0; treat that as "unknown", not a price.
        let rawMark = try c.decodeIfPresent(Double.self, forKey: .mark)
        mark = (rawMark ?? 0) > 0 ? rawMark : nil
        let rawPnL = try c.decodeIfPresent(Double.self, forKey: .unrealizedPnL)
        unrealizedPnL = mark == nil ? nil : rawPnL
    }

    /// Where `price` sits between stop and target, 0 at the stop and 1 at the
    /// target. Stop is always the left end, so for a short (stop above entry)
    /// the scale runs right-to-left in price terms and "left = danger" holds.
    /// Nil when the position has no bracket.
    public func bracketFraction(at price: Double) -> Double? {
        guard stopLoss > 0, takeProfit > 0, stopLoss != takeProfit else { return nil }
        let f = (price - stopLoss) / (takeProfit - stopLoss)
        return min(max(f, 0), 1)
    }

    /// Fractional distance from the mark to the stop and to the target.
    public func distanceToStop(from price: Double) -> Double? {
        guard stopLoss > 0, price > 0 else { return nil }
        return abs(price - stopLoss) / price
    }

    public func distanceToTarget(from price: Double) -> Double? {
        guard takeProfit > 0, price > 0 else { return nil }
        return abs(takeProfit - price) / price
    }

    /// Open P&L as a fraction of the cost basis.
    public var unrealizedFraction: Double? {
        guard let pnl = unrealizedPnL, notional > 0 else { return nil }
        return pnl / notional
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
    /// The kill switch's baseline, from the engine, so the session change
    /// survives a dashboard relaunch. Nil from an engine that predates it.
    public let sessionStartEquity: Double?
    public let limits: RiskLimits?

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
                venue: String = "sim", live: Bool = false,
                sessionStartEquity: Double? = nil, limits: RiskLimits? = nil) {
        self.ticks = ticks
        self.decisions = decisions
        self.fills = fills
        self.rejected = rejected
        self.exits = exits
        self.halted = halted
        self.equity = equity
        self.venue = venue
        self.live = live
        self.sessionStartEquity = sessionStartEquity
        self.limits = limits
    }

    private enum CodingKeys: String, CodingKey {
        case ticks, decisions, fills, rejected, exits, halted, equity, venue, live
        case sessionStartEquity = "session_start_equity"
        case limits
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
        sessionStartEquity = try c.decodeIfPresent(Double.self, forKey: .sessionStartEquity)
        limits = try c.decodeIfPresent(RiskLimits.self, forKey: .limits)
    }
}

/// The risk gate's configured limits, all fractions (0.05 is 5%).
public struct RiskLimits: Codable, Sendable, Equatable {
    public let maxDailyDrawdown: Double
    public let maxOpenPositions: Int
    public let minConfidence: Double
    public let maxPositionFraction: Double

    public init(maxDailyDrawdown: Double, maxOpenPositions: Int,
                minConfidence: Double, maxPositionFraction: Double) {
        self.maxDailyDrawdown = maxDailyDrawdown
        self.maxOpenPositions = maxOpenPositions
        self.minConfidence = minConfidence
        self.maxPositionFraction = maxPositionFraction
    }

    enum CodingKeys: String, CodingKey {
        case maxDailyDrawdown = "max_daily_drawdown_pct"
        case maxOpenPositions = "max_open_positions"
        case minConfidence = "min_confidence"
        case maxPositionFraction = "max_position_pct"
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
