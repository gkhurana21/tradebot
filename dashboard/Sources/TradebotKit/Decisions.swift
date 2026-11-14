import Foundation

/// Parses the engine's RFC 3339 timestamps. Go emits nanosecond precision with
/// trailing zeros trimmed (`...:31.586526Z`, `...:31Z`, `...:31.5Z`), and
/// `ISO8601DateFormatter` accepts only one of "with fraction" or "without", so
/// the fraction is split off and added back by hand.
public enum ISO8601 {
    public static func parse(_ text: String) -> Date? {
        var base = text
        var fraction = 0.0
        if let dot = text.firstIndex(of: ".") {
            var end = text.index(after: dot)
            while end < text.endIndex, text[end].isASCII, text[end].isNumber {
                end = text.index(after: end)
            }
            let digits = text[text.index(after: dot)..<end]
            guard !digits.isEmpty, let value = Double("0." + digits) else { return nil }
            fraction = value
            base = String(text[..<dot]) + String(text[end...])
        }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        guard let whole = formatter.date(from: base) else { return nil }
        return whole.addingTimeInterval(fraction)
    }

    /// JSON decoding strategy for every engine response.
    public static let strategy = JSONDecoder.DateDecodingStrategy.custom { decoder in
        let text = try decoder.singleValueContainer().decode(String.self)
        guard let date = parse(text) else {
            throw DecodingError.dataCorrupted(.init(
                codingPath: decoder.codingPath,
                debugDescription: "Not an ISO-8601 date: \(text)"))
        }
        return date
    }
}

public enum DecisionKind: Sendable, Equatable {
    case decision, exit, halt
    case other(String)
}

/// One entry of the engine's decision journal (`GET /decisions`).
public struct DecisionItem: Decodable, Sendable, Identifiable, Equatable {
    public let seq: UInt64
    public let at: Date
    public let kind: DecisionKind
    public let clientID: String?
    public let symbol: String?
    public let side: String?
    public let confidence: Double?
    public let verdict: String?
    public let approved: Bool?
    public let reason: String
    public let qty: Double?
    public let price: Double?

    public var id: UInt64 { seq }

    /// A decision the gate approved. Falls back to the verdict word so an
    /// entry without the boolean still classifies.
    public var isApproved: Bool { approved ?? (verdict == "approved") }

    public init(seq: UInt64, at: Date, kind: DecisionKind = .decision,
                clientID: String? = nil, symbol: String? = nil, side: String? = nil,
                confidence: Double? = nil, verdict: String? = nil, approved: Bool? = nil,
                reason: String = "", qty: Double? = nil, price: Double? = nil) {
        self.seq = seq; self.at = at; self.kind = kind; self.clientID = clientID
        self.symbol = symbol; self.side = side; self.confidence = confidence
        self.verdict = verdict; self.approved = approved; self.reason = reason
        self.qty = qty; self.price = price
    }

    enum CodingKeys: String, CodingKey {
        case seq, at, kind, symbol, side, confidence, verdict, approved, reason, qty, price
        case clientID = "client_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(UInt64.self, forKey: .seq)
        at = try c.decode(Date.self, forKey: .at)
        switch try c.decodeIfPresent(String.self, forKey: .kind) ?? "decision" {
        case "decision": kind = .decision
        case "exit": kind = .exit
        case "halt": kind = .halt
        case let other: kind = .other(other)
        }
        clientID = try c.decodeIfPresent(String.self, forKey: .clientID)
        symbol = try c.decodeIfPresent(String.self, forKey: .symbol)
        side = try c.decodeIfPresent(String.self, forKey: .side)
        confidence = try c.decodeIfPresent(Double.self, forKey: .confidence)
        verdict = try c.decodeIfPresent(String.self, forKey: .verdict)
        approved = try c.decodeIfPresent(Bool.self, forKey: .approved)
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
        qty = try c.decodeIfPresent(Double.self, forKey: .qty)
        price = try c.decodeIfPresent(Double.self, forKey: .price)
    }

    /// The gate's reason in plain words. The engine's wording carries the
    /// numbers (`confidence 0.30 < min 0.55`); this rewrites the common ones
    /// and otherwise just capitalises, so an unknown reason is still shown.
    public var displayReason: String {
        let r = reason
        if let m = Self.capture(#"^confidence ([0-9.]+) < min ([0-9.]+)"#, in: r),
           let have = Double(m[0]), let floor = Double(m[1]) {
            return "Confidence \(Self.pct(have)) is below the \(Self.pct(floor)) minimum"
        }
        if let m = Self.capture(#"^max open positions (\d+) reached"#, in: r) {
            return "\(m[0]) of \(m[0]) position slots in use"
        }
        if let m = Self.capture(#"^position at max notional ([0-9.]+)%"#, in: r) {
            let base = symbol.flatMap { $0.split(separator: "-").first.map(String.init) } ?? "Position"
            return "\(base) already at the \(m[0])% max position size"
        }
        if let m = Self.capture(#"^halted: daily drawdown ([0-9.]+)% .* ([0-9.]+)%"#, in: r) {
            return "Kill switch active (\(m[0])% daily loss, limit \(m[1])%)"
        }
        guard let first = r.first else { return "" }
        return first.uppercased() + r.dropFirst()
    }

    private static func pct(_ fraction: Double) -> String {
        "\(Int((fraction * 100).rounded()))%"
    }

    private static func capture(_ pattern: String, in text: String) -> [String]? {
        guard let re = try? NSRegularExpression(pattern: pattern),
              let match = re.firstMatch(in: text, range: NSRange(text.startIndex..., in: text))
        else { return nil }
        return (1..<match.numberOfRanges).compactMap { i in
            Range(match.range(at: i), in: text).map { String(text[$0]) }
        }
    }
}

/// `GET /decisions` response.
public struct DecisionPage: Decodable, Sendable, Equatable {
    public let nextSeq: UInt64
    public let oldestSeq: UInt64
    /// True when entries since the requested cursor were overwritten before
    /// the dashboard read them.
    public let gap: Bool
    public let items: [DecisionItem]

    public init(nextSeq: UInt64, oldestSeq: UInt64 = 1, gap: Bool = false, items: [DecisionItem] = []) {
        self.nextSeq = nextSeq; self.oldestSeq = oldestSeq; self.gap = gap; self.items = items
    }

    enum CodingKeys: String, CodingKey {
        case nextSeq = "next_seq", oldestSeq = "oldest_seq", gap, items
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nextSeq = try c.decode(UInt64.self, forKey: .nextSeq)
        oldestSeq = try c.decodeIfPresent(UInt64.self, forKey: .oldestSeq) ?? 1
        gap = try c.decodeIfPresent(Bool.self, forKey: .gap) ?? false
        items = try c.decodeIfPresent([DecisionItem].self, forKey: .items) ?? []
    }
}
