import Charts
import SwiftUI
import TradebotKit

/// Explicitly main-actor isolated rather than relying on it.
///
/// Swift 6 makes `View` implicitly `@MainActor`, but 5.10 does not -- and this
/// view reads `@MainActor` state on `DashboardModel`. Stating the isolation
/// keeps the code compiling on both.
///
/// Layout, top to bottom: a HALTED banner (only when halted), the hero
/// (equity + three quiet stats), the equity chart, then two columns -- the
/// decision feed (the point of the app: the AI proposes, the gate decides) and
/// the open positions. Window title and engine URL live in the title bar.
@MainActor
struct DashboardView: View {
    let model: DashboardModel

    private var connected: Bool { model.connection.isLive || !model.hasEverConnected }

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            if model.stats.halted {
                haltedBanner
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
            hero
            equityChart
            HStack(alignment: .top, spacing: 16) {
                DecisionFeed(model: model)
                PositionsPanel(model: model)
                    .frame(width: 316)
            }
            .frame(maxHeight: .infinity)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        // Values stay on screen when the engine drops; dimming says "stale".
        .opacity(connected ? 1 : 0.6)
        .animation(.smooth(duration: 0.3), value: model.stats.halted)
        .animation(.smooth(duration: 0.3), value: connected)
        .background(Color(nsColor: .windowBackgroundColor))
        // Text(format:) takes its locale from the environment, so pin it or a
        // non-US Mac renders "US$25,000" instead of "$25,000".
        .environment(\.locale, Locale(identifier: "en_US"))
        .navigationTitle("Tradebot")
        .navigationSubtitle(model.engineURL.absoluteString)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                ModeBadge(stats: model.stats)
                ConnectionPill(model: model)
            }
        }
    }

    // MARK: - Banner

    private var haltedBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.octagon.fill")
                .foregroundStyle(.red)
            Text("Trading halted — daily loss limit reached. The gate is blocking every new proposal.")
                .font(.system(size: 13, weight: .medium))
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .frame(height: 32)
        .background(Color.red.opacity(0.12),
                    in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    // MARK: - Hero

    private var hero: some View {
        HStack(alignment: .bottom, spacing: 24) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Equity")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text(model.stats.equity, format: .currency(code: "USD").locale(Locale(identifier: "en_US")))
                    .font(.system(size: 34, weight: .semibold))
                    .monospacedDigit()
                    .contentTransition(.numericText(value: model.stats.equity))
                    .animation(.smooth(duration: 0.4), value: model.stats.equity)
                sessionChange
            }
            Spacer(minLength: 0)
            HStack(alignment: .bottom, spacing: 28) {
                HeroStat(value: "\(model.stats.decisions)", label: "Proposals")
                HeroStat(value: "\(model.stats.rejected)", label: "Blocked by gate")
                dailyLossStat
            }
        }
        .redacted(reason: model.hasEverConnected ? [] : .placeholder)
    }

    private var sessionChange: some View {
        let change = model.sessionChange
        let fraction = model.sessionReturn
        let tint = changeColor(fraction)
        let symbol = abs(fraction) < 0.0001 ? "minus" : (change >= 0 ? "arrow.up.right" : "arrow.down.right")
        return HStack(spacing: 6) {
            Image(systemName: symbol).imageScale(.small)
            Text(change, format: .currency(code: "USD").locale(Locale(identifier: "en_US")).sign(strategy: .always()))
            Text(fraction, format: .percent.precision(.fractionLength(2)).sign(strategy: .always()))
            Text("this session")
                .foregroundStyle(.secondary)
                .fontWeight(.regular)
        }
        .font(.system(size: 13, weight: .medium))
        .monospacedDigit()
        .foregroundStyle(tint)
    }

    /// Daily loss against the kill-switch limit: the number that matters,
    /// shown relative to its limit rather than as a bare drawdown.
    private var dailyLossStat: some View {
        let loss = model.dailyLoss
        let limit = model.stats.limits?.maxDailyDrawdown
        let used = model.dailyLossUsed ?? 0
        let tint: Color = model.stats.halted ? .red : (used > 0.6 ? .orange : .secondary)
        return VStack(alignment: .trailing, spacing: 3) {
            HStack(spacing: 4) {
                Text(loss, format: .percent.precision(.fractionLength(1)))
                if let limit {
                    Text("/ \(limit.formatted(.percent.precision(.fractionLength(0...1))))")
                        .foregroundStyle(.secondary)
                }
            }
            .font(.system(size: 17, weight: .semibold))
            .monospacedDigit()
            HStack(spacing: 8) {
                if limit != nil {
                    Capsule().fill(Color.secondary.opacity(0.18))
                        .frame(width: 56, height: 4)
                        .overlay(alignment: .leading) {
                            Capsule().fill(tint).frame(width: max(4, 56 * used), height: 4)
                        }
                }
                Text("Daily loss").font(.system(size: 11)).foregroundStyle(.secondary)
            }
        }
        .help("Loss since the session start, against the limit at which the kill switch halts trading.")
    }

    // MARK: - Chart

    private var equityChart: some View {
        Group {
            if model.equityHistory.count < 2 {
                HStack {
                    Spacer()
                    Text("Waiting for samples…")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    Spacer()
                }
            } else {
                let domain = model.equityDomain
                let first = model.equityHistory[0].time
                let last = model.equityHistory[model.equityHistory.count - 1]
                let tint = changeColor(model.sessionReturn)
                // A fresh session draws left to right instead of stretching
                // two samples across the whole width.
                let xEnd = max(first.addingTimeInterval(120), last.time)
                Chart {
                    ForEach(model.equityHistory) { point in
                        AreaMark(x: .value("Time", point.time),
                                 yStart: .value("Floor", domain.lowerBound),
                                 yEnd: .value("Equity", point.equity))
                            .interpolationMethod(.monotone)
                            .foregroundStyle(LinearGradient(
                                colors: [tint.opacity(0.16), tint.opacity(0)],
                                startPoint: .top, endPoint: .bottom))
                        LineMark(x: .value("Time", point.time),
                                 y: .value("Equity", point.equity))
                            .interpolationMethod(.monotone)
                            .foregroundStyle(tint)
                            .lineStyle(StrokeStyle(lineWidth: 1.5))
                    }
                    if let start = model.sessionStartEquity, domain.contains(start) {
                        RuleMark(y: .value("Start", start))
                            .lineStyle(StrokeStyle(lineWidth: 0.75, dash: [3, 3]))
                            .foregroundStyle(Color.secondary.opacity(0.6))
                            .annotation(position: .top, alignment: .leading, spacing: 2) {
                                Text("Start").font(.system(size: 10)).foregroundStyle(.tertiary)
                            }
                    }
                    PointMark(x: .value("Time", last.time), y: .value("Equity", last.equity))
                        .symbolSize(36)
                        .foregroundStyle(tint)
                }
                .chartXScale(domain: first...xEnd)
                .chartYScale(domain: domain)
                .chartLegend(.hidden)
                .chartXAxis {
                    AxisMarks(values: .stride(by: .minute)) { value in
                        AxisValueLabel {
                            if let d = value.as(Date.self) { Text(clock(d, seconds: false)) }
                        }
                            .font(.system(size: 11))
                            .foregroundStyle(Color.secondary.opacity(0.7))
                    }
                }
                .chartYAxis {
                    AxisMarks(position: .trailing, values: .automatic(desiredCount: 3)) { value in
                        AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5))
                            .foregroundStyle(Color.secondary.opacity(0.25))
                        AxisValueLabel {
                            if let v = value.as(Double.self) {
                                Text(v, format: .number.precision(.fractionLength(0)))
                                    .font(.system(size: 11)).monospacedDigit()
                                    .foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
            }
        }
        .frame(height: 170)
    }
}

// MARK: - Header pieces

private struct ModeBadge: View {
    let stats: EngineStats

    var body: some View {
        let mode = stats.mode
        Text(mode.rawValue)
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(mode == .live ? Color.white : Color.secondary)
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background {
                switch mode {
                case .live: Capsule().fill(Color.red)
                case .paper: Capsule().fill(Color.secondary.opacity(0.14))
                case .sim: Capsule().strokeBorder(Color.secondary.opacity(0.5), lineWidth: 1)
                }
            }
            .help(mode == .live ? "Orders on \(stats.venue) risk real money."
                                : "Orders go to \(stats.venue). No real money.")
    }
}

private struct ConnectionPill: View {
    let model: DashboardModel

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 10).padding(.vertical, 4)
        .background(Color.secondary.opacity(0.12), in: Capsule())
        .help(help)
    }

    private var label: String {
        switch model.connection {
        case .live: return "Connected"
        case .connecting: return "Connecting…"
        case .failing: return model.hasEverConnected ? "Reconnecting…" : "Disconnected"
        }
    }

    private var color: Color {
        switch model.connection {
        case .live: return .green
        case .connecting, .failing: return .orange
        }
    }

    private var help: String {
        var text = "Polling \(model.engineURL.absoluteString) once per second."
        if case .failing(let reason) = model.connection { text = reason }
        if let last = model.lastUpdate {
            text += " Last update " + clock(last, seconds: true) + "."
        }
        return text
    }
}

private struct HeroStat: View {
    let value: String
    let label: String

    var body: some View {
        VStack(alignment: .trailing, spacing: 3) {
            Text(value)
                .font(.system(size: 17, weight: .semibold))
                .monospacedDigit()
            Text(label).font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}

// MARK: - Decision feed

private struct DecisionFeed: View {
    let model: DashboardModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            content
        }
        .panel()
        .frame(minWidth: 460, maxWidth: .infinity, maxHeight: .infinity)
    }

    private var header: some View {
        HStack {
            Text("Decisions").font(.system(size: 13, weight: .semibold))
            Spacer()
            if model.decisionsSupported != false, model.stats.decisions > 0 {
                let blocked = Int(model.stats.rejected)
                let approved = max(0, Int(model.stats.decisions) - blocked)
                Text("\(approved) approved · \(blocked) blocked")
                    .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    @ViewBuilder private var content: some View {
        if model.decisionsSupported == false {
            EmptyState(icon: "arrow.triangle.2.circlepath",
                       title: "This engine doesn't publish decisions",
                       detail: "Update the engine to see the risk gate's verdicts here.")
        } else if model.decisions.isEmpty {
            EmptyState(icon: "checkmark.shield",
                       title: "Waiting for the first decision…",
                       detail: "Proposals from the strategy appear here with the risk gate's verdict.")
        } else {
            ScrollView {
                VStack(spacing: 0) {
                    if model.decisionsGap {
                        Text("Some earlier decisions were missed.")
                            .font(.system(size: 11)).foregroundStyle(.tertiary)
                            .padding(.vertical, 6)
                    }
                    ForEach(model.decisions) { item in
                        DecisionRow(item: item, minConfidence: model.stats.limits?.minConfidence)
                            .transition(.push(from: .top))
                        Divider().padding(.leading, 12)
                    }
                }
                .animation(.snappy(duration: 0.35), value: model.decisions.first?.seq)
            }
        }
    }
}

private struct DecisionRow: View {
    let item: DecisionItem
    let minConfidence: Double?
    @State private var flash: Bool

    init(item: DecisionItem, minConfidence: Double?) {
        self.item = item
        self.minConfidence = minConfidence
        // Only rows that arrive live get the highlight, not the backlog read
        // at launch.
        _flash = State(initialValue: Date().timeIntervalSince(item.at) < 5)
    }

    var body: some View {
        Group {
            switch item.kind {
            case .decision: proposal
            case .exit: quiet(icon: "arrow.uturn.backward", tint: .secondary,
                              title: "Exit \(item.symbol ?? "")", detail: exitDetail)
            case .halt: quiet(icon: "exclamationmark.octagon.fill", tint: .red,
                              title: "Trading halted", detail: item.displayReason)
            case .other(let kind): quiet(icon: "circle", tint: .secondary,
                                         title: kind.capitalized, detail: item.displayReason)
            }
        }
        .padding(.horizontal, 12)
        .background(flash ? flashTint.opacity(0.12) : Color.clear)
        .onAppear {
            guard flash else { return }
            withAnimation(.easeOut(duration: 1.2)) { flash = false }
        }
        .help(helpText)
    }

    private var flashTint: Color { item.isApproved ? .blue : .orange }

    private var proposal: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 0) {
                Text(clock(item.at, seconds: true))
                    .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                    .frame(width: 64, alignment: .leading)
                Text(item.side ?? "")
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                    .frame(width: 36, alignment: .leading)
                Text(item.symbol ?? "—")
                    .font(.system(size: 13, weight: .medium))
                    .frame(width: 84, alignment: .leading)
                if let confidence = item.confidence {
                    ConfidenceMeter(value: confidence, floor: minConfidence)
                }
                Spacer(minLength: 8)
                VerdictChip(approved: item.isApproved)
            }
            HStack(spacing: 0) {
                Spacer().frame(width: 64 + 36)
                Text(item.displayReason)
                    .font(.system(size: 12)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.tail)
            }
        }
        .padding(.vertical, 8)
        .frame(minHeight: 48)
    }

    private func quiet(icon: String, tint: Color, title: String, detail: String) -> some View {
        HStack(spacing: 0) {
            Text(clock(item.at, seconds: true))
                .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                .frame(width: 64, alignment: .leading)
            Image(systemName: icon)
                .font(.system(size: 11)).foregroundStyle(tint)
                .frame(width: 20, alignment: .leading)
            Text(title)
                .font(.system(size: 12, weight: .medium)).foregroundStyle(.secondary)
            Text("  \(detail)")
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .lineLimit(1).truncationMode(.tail)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8)
    }

    private var exitDetail: String {
        var parts = [item.displayReason]
        if let price = item.price { parts.append("@ " + price.formatted(.number.precision(.fractionLength(2)))) }
        return parts.filter { !$0.isEmpty }.joined(separator: " ")
    }

    private var helpText: String {
        var parts = [item.reason]
        if let id = item.clientID { parts.append("id \(id)") }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

}

/// Confidence with a tick at the gate's minimum, so a low-confidence block is
/// visible before the reason is read.
private struct ConfidenceMeter: View {
    let value: Double
    let floor: Double?

    var body: some View {
        HStack(spacing: 8) {
            Text(value, format: .percent.precision(.fractionLength(0)))
                .font(.system(size: 12)).monospacedDigit()
                .frame(width: 36, alignment: .trailing)
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18)).frame(width: 32, height: 3)
                Capsule().fill(Color.secondary.opacity(0.65))
                    .frame(width: 32 * min(max(value, 0), 1), height: 3)
                if let floor {
                    Rectangle().fill(Color.primary.opacity(0.55))
                        .frame(width: 1, height: 7)
                        .offset(x: 32 * min(max(floor, 0), 1))
                }
            }
            .frame(width: 32, height: 8)
        }
    }
}

private struct VerdictChip: View {
    let approved: Bool

    var body: some View {
        let tint: Color = approved ? .blue : .orange
        HStack(spacing: 4) {
            Image(systemName: approved ? "checkmark.shield.fill" : "xmark.shield.fill")
                .foregroundStyle(tint)
            Text(approved ? "Approved" : "Blocked").foregroundStyle(.primary)
        }
        .font(.system(size: 11, weight: .semibold))
        .padding(.horizontal, 8).padding(.vertical, 3)
        .background(tint.opacity(0.14), in: Capsule())
    }
}

// MARK: - Positions

private struct PositionsPanel: View {
    let model: DashboardModel

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Positions").font(.system(size: 13, weight: .semibold))
                Spacer()
                if let limit = model.stats.limits?.maxOpenPositions {
                    Text("\(model.portfolio.positions.count) of \(limit) slots")
                        .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 10)
            Divider()
            if model.portfolio.rankedPositions.isEmpty {
                EmptyState(icon: "tray", title: "Flat", detail: "No open positions.")
            } else {
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(model.portfolio.rankedPositions) { position in
                            PositionBlock(position: position)
                            Divider().padding(.leading, 12)
                        }
                    }
                }
            }
        }
        .panel()
        .frame(maxHeight: .infinity)
    }
}

private struct PositionBlock: View {
    let position: Position

    var body: some View {
        let pnl = position.unrealizedPnL
        let tint = pnl.map { changeColor($0) } ?? Color.secondary
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline) {
                Text(position.symbol).font(.system(size: 13, weight: .medium))
                Text(position.side == .buy ? "LONG" : "SHORT")
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
                Spacer()
                if let pnl {
                    Text(pnl, format: .currency(code: "USD").locale(Locale(identifier: "en_US")).sign(strategy: .always()))
                        .font(.system(size: 13, weight: .semibold)).monospacedDigit()
                        .foregroundStyle(tint)
                } else {
                    Text("—").font(.system(size: 13)).foregroundStyle(.tertiary)
                }
            }
            HStack {
                Text("\(position.qty.magnitudeText) @ \(position.avgPrice.priceText)")
                    .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                Spacer()
                if let f = position.unrealizedFraction {
                    Text(f, format: .percent.precision(.fractionLength(2)).sign(strategy: .always()))
                        .font(.system(size: 11)).monospacedDigit().foregroundStyle(tint)
                }
            }
            BracketBar(position: position, tint: tint)
                .padding(.top, 4)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .help(helpText)
    }

    private var helpText: String {
        guard let mark = position.mark,
              let toStop = position.distanceToStop(from: mark),
              let toTarget = position.distanceToTarget(from: mark) else { return "" }
        return "\(toStop.formatted(.percent.precision(.fractionLength(1)))) to stop · "
            + "\(toTarget.formatted(.percent.precision(.fractionLength(1)))) to target"
    }
}

/// Stop on the left, target on the right, a tick at the entry and a dot at the
/// mark. The left end is the danger end for longs and shorts alike.
private struct BracketBar: View {
    let position: Position
    let tint: Color

    var body: some View {
        VStack(spacing: 3) {
            GeometryReader { geo in
                let w = geo.size.width
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.18)).frame(height: 4)
                    if let entry = position.bracketFraction(at: position.avgPrice) {
                        Rectangle().fill(Color.secondary.opacity(0.7))
                            .frame(width: 1, height: 8)
                            .offset(x: w * entry)
                    }
                    if let mark = position.mark, let f = position.bracketFraction(at: mark) {
                        Circle().fill(tint).frame(width: 8, height: 8)
                            .offset(x: min(max(w * f - 4, 0), w - 8))
                    }
                }
                .frame(height: 8)
            }
            .frame(height: 8)
            HStack {
                Text(position.stopLoss > 0 ? position.stopLoss.priceText : "—")
                Spacer()
                Text(position.takeProfit > 0 ? position.takeProfit.priceText : "—")
            }
            .font(.system(size: 10)).foregroundStyle(.tertiary).monospacedDigit()
        }
    }
}

// MARK: - Shared

private struct EmptyState: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: icon)
                .font(.system(size: 20)).foregroundStyle(.tertiary)
            Text(title).font(.system(size: 13, weight: .medium)).foregroundStyle(.secondary)
            Text(detail)
                .font(.system(size: 12)).foregroundStyle(.tertiary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private extension View {
    /// The one card style: plain surface, continuous corners, hairline edge.
    func panel() -> some View {
        self.background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(.separator)
        )
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// 24-hour wall-clock time. A fixed format, not the locale's, so the feed, the
/// chart axis and tooltips agree (en_US would drop the AM/PM marker and leave
/// "04:25" ambiguous).
private func clock(_ date: Date, seconds: Bool) -> String {
    let f = DateFormatter()
    f.locale = Locale(identifier: "en_US_POSIX")
    f.dateFormat = seconds ? "HH:mm:ss" : "HH:mm"
    return f.string(from: date)
}

/// Green or red by sign; anything under 0.01% (or a dollar-noise change)
/// stays neutral so the UI does not shout about rounding.
private func changeColor(_ value: Double) -> Color {
    abs(value) < 0.0001 ? .secondary : (value > 0 ? .green : .red)
}

private extension Double {
    var priceText: String {
        formatted(.number.precision(.fractionLength(2)).grouping(.automatic))
    }

    var magnitudeText: String {
        abs(self).formatted(.number.precision(.fractionLength(0...4)))
    }
}
