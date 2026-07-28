import AppKit
import SwiftUI
import TradebotKit

/// Owns the model for the process lifetime.
///
/// Polling is deliberately not tied to a view: a monitoring tool should keep
/// following the book whether or not a window happens to be on screen, and the
/// equity history it accumulates should survive a window close.
@MainActor
final class DashboardHost {
    static let shared = DashboardHost()
    let model: DashboardModel

    private init() {
        model = DashboardModel(client: EngineClient(baseURL: Self.engineURL()))
    }

    /// Engine location is overridable so one binary can watch a local engine
    /// or a remote one:
    ///   open TradebotDashboard.app --args --url http://host:9000
    ///
    /// Reads only process arguments, so it is safe to call before the main
    /// actor is running -- the probe path needs it at startup.
    nonisolated static func engineURL() -> URL {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--url"), i + 1 < args.count,
           let url = URL(string: args[i + 1]) {
            return url
        }
        return URL(string: "http://127.0.0.1:8080")!
    }
}

@main
struct TradebotDashboardApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    init() {
        // A headless connectivity check, so the client stack can be verified
        // over SSH or in CI where no window server exists.
        if CommandLine.arguments.contains("--probe") {
            probeAndExit()
        }
    }

    var body: some Scene {
        WindowGroup("Tradebot Engine") {
            DashboardView(model: DashboardHost.shared.model)
                .frame(minWidth: 860, minHeight: 620)
        }
        .windowResizability(.contentMinSize)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Launched from SwiftPM output rather than a normal bundle install,
        // the process can start as an accessory with no dock tile or key
        // window. Promoting it makes it behave like an ordinary app.
        NSApplication.shared.setActivationPolicy(.regular)
        NSApplication.shared.activate(ignoringOtherApps: true)
        MainActor.assumeIsolated {
            DashboardHost.shared.model.start()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

/// Fetches one snapshot, prints it, and exits. Never returns.
private func probeAndExit() -> Never {
    let url = DashboardHost.engineURL()
    let client = EngineClient(baseURL: url)
    let done = DispatchSemaphore(value: 0)
    nonisolated(unsafe) var exitCode: Int32 = 0

    Task {
        defer { done.signal() }
        do {
            let (stats, portfolio) = try await client.snapshot()
            print("engine:    \(url.absoluteString)")
            print("equity:    \(portfolio.equity)")
            print("cash:      \(portfolio.cash)")
            print("halted:    \(stats.halted)")
            print("fills:     \(stats.fills)   rejected: \(stats.rejected)   exits: \(stats.exits)")
            if portfolio.positions.isEmpty {
                print("positions: (flat)")
            } else {
                print("positions:")
                for position in portfolio.rankedPositions {
                    let side = position.side == .buy ? "LONG " : "SHORT"
                    print(String(format: "  %@ %-9@ qty %.6f @ %.2f",
                                 side, position.symbol, position.qty, position.avgPrice))
                }
            }
        } catch {
            FileHandle.standardError.write(Data("probe failed: \(error.localizedDescription)\n".utf8))
            exitCode = 1
        }
    }

    done.wait()
    exit(exitCode)
}
