import Foundation

/// Errors surfaced to the UI. Each case maps to something a user can act on --
/// "the engine isn't running" is a different problem from "the engine replied
/// with garbage", and the dashboard says which.
public enum EngineError: Error, LocalizedError, Equatable {
    case unreachable(String)
    case badStatus(Int)
    case decoding(String)

    public var errorDescription: String? {
        switch self {
        case .unreachable(let detail):
            return "Cannot reach the engine — is it running? (\(detail))"
        case .badStatus(let code):
            return "Engine returned HTTP \(code)"
        case .decoding(let detail):
            return "Unexpected response format: \(detail)"
        }
    }
}

/// Transport seam. Tests substitute a stub so the client's decoding, error
/// mapping, and retry behavior are verifiable without a live server or a
/// network stack.
public protocol HTTPTransport: Sendable {
    func get(_ url: URL) async throws -> (Data, Int)
    func post(_ url: URL, body: Data) async throws -> (Data, Int)
}

/// URLSession-backed transport.
public struct URLSessionTransport: HTTPTransport {
    private let session: URLSession

    public init(timeout: TimeInterval = 5) {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = timeout
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)
    }

    public func get(_ url: URL) async throws -> (Data, Int) {
        try await send(URLRequest(url: url))
    }

    public func post(_ url: URL, body: Data) async throws -> (Data, Int) {
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.httpBody = body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        return try await send(req)
    }

    private func send(_ request: URLRequest) async throws -> (Data, Int) {
        do {
            let (data, response) = try await session.data(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            return (data, code)
        } catch {
            throw EngineError.unreachable(error.localizedDescription)
        }
    }
}

/// Read-only client for the Go execution engine.
///
/// The dashboard deliberately exposes no way to place a trade. It is an
/// observability surface: a monitoring tool that can send orders is a
/// monitoring tool that can fat-finger the book.
public struct EngineClient: Sendable {
    public let baseURL: URL
    private let transport: HTTPTransport
    private let decoder: JSONDecoder

    public init(baseURL: URL, transport: HTTPTransport = URLSessionTransport()) {
        self.baseURL = baseURL
        self.transport = transport
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = ISO8601.strategy
        self.decoder = decoder
    }

    public func health() async throws -> Health {
        try await fetch("health")
    }

    public func stats() async throws -> EngineStats {
        try await fetch("stats")
    }

    public func portfolio() async throws -> PortfolioSnapshot {
        try await fetch("positions")
    }

    /// The decision journal from `since` (inclusive) onward. Throws
    /// `badStatus(404)` on an engine that predates the endpoint; callers treat
    /// that as "feed unavailable", not as an outage.
    public func decisions(since: UInt64? = nil) async throws -> DecisionPage {
        try await fetch("decisions", query: since.map { [URLQueryItem(name: "since", value: String($0))] } ?? [])
    }

    /// Fetches stats and portfolio together so the UI never renders equity
    /// from one instant beside positions from another.
    public func snapshot() async throws -> (EngineStats, PortfolioSnapshot) {
        async let stats = stats()
        async let portfolio = portfolio()
        return try await (stats, portfolio)
    }

    private func fetch<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        var url = baseURL.appendingPathComponent(path)
        if !query.isEmpty, var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            parts.queryItems = query
            url = parts.url ?? url
        }
        let (data, code) = try await transport.get(url)
        guard (200..<300).contains(code) else { throw EngineError.badStatus(code) }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw EngineError.decoding(String(describing: error))
        }
    }
}
