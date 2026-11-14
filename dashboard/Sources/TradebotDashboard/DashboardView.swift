import Charts
import SwiftUI
import TradebotKit

/// Explicitly main-actor isolated rather than relying on it.
///
/// Swift 6 makes `View` implicitly `@MainActor`, but 5.10 does not -- and this
/// view reads `@MainActor` state on `DashboardModel`. Stating the isolation
/// keeps the code compiling on both.
@MainActor
struct DashboardView: View {
    let model: DashboardModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    statRow
                    equityChart
                    positionsTable
                }
                .padding(20)
            }
        }
        .background(Color(nsColor: .windowBackgroundColor))
    }

    // MARK: - Header

    private var header: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Tradebot Engine")
                    .font(.system(size: 17, weight: .semibold))
                Text(model.engineURL.absoluteString)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if model.stats.halted {
                Label("HALTED", systemImage: "exclamationmark.octagon.fill")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(Color.red, in: Capsule())
                    .help("Daily drawdown kill switch has tripped — trading is stopped.")
            }
            connectionPill
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
    }

    private var connectionPill: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(connectionColor)
                .frame(width: 7, height: 7)
            Text(model.connection.label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10).padding(.vertical, 5)
        .background(Color.secondary.opacity(0.12), in: Capsule())
        .help(connectionHelp)
    }

    private var connectionColor: Color {
        switch model.connection {
        case .live: return .green
        case .connecting: return .orange
        case .failing: return .red
        }
    }

    private var connectionHelp: String {
        if case .failing(let reason) = model.connection { return reason }
        return "Polling the engine once per second."
    }

    // MARK: - Stat tiles

    private var statRow: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
            StatTile(label: "Equity",
                     value: money(model.stats.equity),
                     tone: .neutral)
            StatTile(label: "Session return",
                     value: percent(model.sessionReturn, signed: true),
                     tone: model.sessionReturn >= 0 ? .good : .bad)
            StatTile(label: "Observed drawdown",
                     value: percent(model.observedDrawdown),
                     tone: model.observedDrawdown > 0 ? .bad : .neutral)
            StatTile(label: "Fills", value: "\(model.stats.fills)", tone: .neutral)
            StatTile(label: "Rejected",
                     value: "\(model.stats.rejected)",
                     detail: percent(model.stats.rejectionRate) + " of decisions",
                     tone: .neutral)
            StatTile(label: "Bracket exits", value: "\(model.stats.exits)", tone: .neutral)
        }
    }

    // MARK: - Equity curve

    private var equityChart: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel("Equity", trailing: model.lastUpdate.map(freshness))
            Group {
                if model.equityHistory.count < 2 {
                    placeholder("Waiting for samples…")
                } else {
                    let domain = model.equityDomain
                    Chart(model.equityHistory) { point in
                        // The fill starts at the axis floor, not at zero, or the
                        // area would force the scale back down to zero.
                        AreaMark(x: .value("Time", point.time),
                                 yStart: .value("Floor", domain.lowerBound),
                                 yEnd: .value("Equity", point.equity))
                            .foregroundStyle(chartTint.opacity(0.12))
                        LineMark(x: .value("Time", point.time),
                                 y: .value("Equity", point.equity))
                            .foregroundStyle(chartTint)
                            .lineStyle(StrokeStyle(lineWidth: 2))
                    }
                    .chartYScale(domain: domain)
                    .chartXAxis { AxisMarks(values: .automatic(desiredCount: 4)) }
                    .frame(height: 200)
                }
            }
            .padding(14)
            .background(cardBackground)
        }
    }

    /// Green when up on the session, red when down — the colour carries the
    /// same information as the number, so the chart reads at a glance.
    private var chartTint: Color { model.sessionReturn >= 0 ? .green : .red }

    // MARK: - Positions

    private var positionsTable: some View {
        VStack(alignment: .leading, spacing: 8) {
            SectionLabel("Open positions",
                         trailing: "\(model.portfolio.positions.count)")
            Group {
                if model.portfolio.rankedPositions.isEmpty {
                    placeholder("Flat — no open positions.")
                        .padding(14)
                } else {
                    VStack(spacing: 0) {
                        PositionHeaderRow()
                        ForEach(model.portfolio.rankedPositions) { position in
                            Divider()
                            PositionRow(position: position)
                        }
                    }
                }
            }
            .background(cardBackground)
        }
    }

    // MARK: - Shared chrome

    private func placeholder(_ text: String) -> some View {
        HStack {
            Spacer()
            Text(text).font(.system(size: 12)).foregroundStyle(.secondary)
            Spacer()
        }
        .frame(height: 60)
    }

    private var cardBackground: some View {
        RoundedRectangle(cornerRadius: 10)
            .fill(Color(nsColor: .controlBackgroundColor))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .stroke(Color.secondary.opacity(0.15), lineWidth: 1)
            )
    }

    private func freshness(_ date: Date) -> String {
        let seconds = Int(Date().timeIntervalSince(date))
        return seconds < 2 ? "just now" : "\(seconds)s ago"
    }
}

// MARK: - Components

private struct SectionLabel: View {
    let title: String
    let trailing: String?

    init(_ title: String, trailing: String? = nil) {
        self.title = title
        self.trailing = trailing
    }

    var body: some View {
        HStack {
            Text(title.uppercased())
                .font(.system(size: 10, weight: .semibold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
            Spacer()
            if let trailing {
                Text(trailing).font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }
    }
}

private enum Tone { case neutral, good, bad }

private struct StatTile: View {
    let label: String
    let value: String
    var detail: String? = nil
    let tone: Tone

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value)
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(color)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
            if let detail {
                Text(detail).font(.system(size: 10)).foregroundStyle(.tertiary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(Color(nsColor: .controlBackgroundColor))
                .overlay(
                    RoundedRectangle(cornerRadius: 10)
                        .stroke(Color.secondary.opacity(0.15), lineWidth: 1)
                )
        )
    }

    private var color: Color {
        switch tone {
        case .neutral: return .primary
        case .good: return .green
        case .bad: return .red
        }
    }
}

private struct PositionHeaderRow: View {
    var body: some View {
        HStack(spacing: 0) {
            cell("SYMBOL", width: 130, align: .leading)
            cell("SIDE", width: 70, align: .leading)
            cell("QTY", width: 110, align: .trailing)
            cell("ENTRY", width: 110, align: .trailing)
            cell("STOP", width: 110, align: .trailing)
            cell("TARGET", width: 110, align: .trailing)
            Spacer(minLength: 0)
        }
        .font(.system(size: 10, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }

    private func cell(_ text: String, width: CGFloat, align: Alignment) -> some View {
        Text(text).frame(width: width, alignment: align)
    }
}

private struct PositionRow: View {
    let position: Position

    var body: some View {
        HStack(spacing: 0) {
            Text(position.symbol)
                .font(.system(size: 12, weight: .medium))
                .frame(width: 130, alignment: .leading)

            Text(position.side == .buy ? "LONG" : "SHORT")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(position.side == .buy ? Color.green : Color.red)
                .frame(width: 70, alignment: .leading)

            numeric(qty(position.qty), width: 110)
            numeric(money(position.avgPrice), width: 110)
            numeric(position.stopLoss > 0 ? money(position.stopLoss) : "—", width: 110)
            numeric(position.takeProfit > 0 ? money(position.takeProfit) : "—", width: 110)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private func numeric(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .font(.system(size: 12, design: .monospaced))
            .monospacedDigit()
            .frame(width: width, alignment: .trailing)
    }
}

// MARK: - Formatting

private func money(_ value: Double) -> String {
    value.formatted(.number.precision(.fractionLength(2)).grouping(.automatic))
}

private func qty(_ value: Double) -> String {
    value.formatted(.number.precision(.fractionLength(0...6)))
}

private func percent(_ fraction: Double, signed: Bool = false) -> String {
    let style: FloatingPointFormatStyle<Double>.Percent = signed
        ? .percent.precision(.fractionLength(2)).sign(strategy: .always())
        : .percent.precision(.fractionLength(2))
    return fraction.formatted(style)
}
