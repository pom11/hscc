import SwiftUI
import WidgetKit
import ActivityKit

/// HSCCLiveActivityMonitor — the ActivityKit configuration for the
/// cluster-health Live Activity.
///
/// Hosted by the `HSCCLiveActivityMonitor` app-extension target beside the
/// cluster-monitor and running-cards activities (see
/// `HSCCLiveActivityMonitorBundle`): one extension hosts several Live Activity
/// types, each with its own `ActivityConfiguration`, which is why the
/// extension's `@main` is a `WidgetBundle` rather than a single `Widget`.
///
/// Live Activities are PUSH-ONLY — this extension never fetches. It renders
/// whatever the app last pushed into `ClusterHealthActivityAttributes.ContentState`
/// (the serving state from `/v1/autodown/status` and the node tally from
/// `/v1/cluster/monitor`, projected to the reduced `ClusterHealthSignals`) and
/// ages honestly via the pushed `updatedAt`.
///
/// Dynamic Island:
///   * compact leading  — the fleet glyph, coloured by the reading, plus the
///     secondary the glance rule chose (the node tally) when the primary is a
///     load or heat number.
///   * compact trailing — the PRIMARY metric: the busiest GPU's utilisation,
///     else the hottest GPU's temperature, else the tally, else `!` / `—`.
///     All four strings come from `ClusterHealthReading.glance`, which owns the
///     order — the region decides nothing.
///   * expanded         — headline + failure line + the per-host rows and the
///     fleet totals, then the down nodes.
///   * minimal          — the glyph plus the one primary metric.
///
/// Lock Screen — the row: headline, age, the per-host `GPU% / GPU Temp /
/// GPU Power` lines the CLI prints (up to `hostLineLimit`, then `+n`), the total
/// draw and the serving run/wait totals, then the down-node labels or the one
/// sentence that replaces them.
///
/// Every word, number and tone comes from `ClusterHealthReading` (in
/// `Sources/Shared/ClusterHealthActivity.swift`), never from a local decision,
/// so the gate that executes those rules covers what this file draws: a blind
/// read renders its failure label, never `0/0 up`, never a mint "Serving", and
/// never a `0 W` for a fleet that never answered.
struct ClusterHealthLiveActivity: Widget {
    var body: some WidgetConfiguration {
        let style = WidgetStyle.resolved(for: .liveActivityClusterHealth)
        return ActivityConfiguration(for: ClusterHealthActivityAttributes.self) { context in
            LockScreenClusterHealthView(context: context)
                .widgetStyle(style)
                .activityBackgroundTint(style.backgroundColor)
                .activitySystemActionForegroundColor(style.primaryTextColor)
        } dynamicIsland: { context in
            // The reading is decided once, then handed to every region, so the
            // compact and expanded forms of one state can never disagree.
            let signals = context.state.signals
            let tone = ClusterHealthReading.tone(signals)
            let glance = ClusterHealthReading.glance(signals)
            return DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    ClusterHealthGlyph(tone: tone).widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.center) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ClusterHealthReading.headline(signals))
                            .font(style.font(.headline))
                            .foregroundColor(style.primaryTextColor)
                            .lineLimit(1)
                        if let failureLabel = signals.failureLabel, !failureLabel.isEmpty {
                            // Stale is not quiet: the tally below may be
                            // last-known, and this says why it is not new.
                            Text("stale · \(failureLabel)")
                                .font(style.font(.caption2))
                                .foregroundColor(style.warnColor)
                                .lineLimit(1)
                        }
                        if let feed = ClusterHealthReading.feedText(model: signals.modelLabel,
                                                                    genTps: signals.genTps) {
                            Text(feed)
                                .font(style.font(.caption2))
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                    .widgetStyle(style)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(ClusterHealthReading.pushedAgeText(updatedAt: context.state.updatedAt,
                                                            now: Date()))
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 3) {
                        // The host table first: in the expanded Island the
                        // headline above already states the state and the tally,
                        // so what the bottom region adds is the per-host detail.
                        ClusterHealthHostLines(signals: signals,
                                               limit: ClusterHealthActivityAttributes.islandHostLineLimit,
                                               labelFont: style.font(size: 9, weight: .semibold))
                        ClusterHealthLoadSummary(signals: signals,
                                                 font: style.font(size: 9, weight: .semibold))
                        ClusterHealthDetail(signals: signals,
                                            limit: ClusterHealthActivityAttributes.downNodeLimit,
                                            labelFont: style.font(size: 10, weight: .semibold)).widgetStyle(style)
                    }
                }
            } compactLeading: {
                // The glyph stays — it is the state — and the glance's secondary
                // (the node tally) sits beside it, so adding a load metric did
                // not take the tally away from the only two-slot region this
                // activity has.
                HStack(spacing: 2) {
                    ClusterHealthGlyph(tone: tone).widgetStyle(style)
                    if let secondary = glance.secondary {
                        Text(secondary)
                            .font(style.font(size: 10, weight: .semibold))
                            .foregroundColor(ClusterHealthToneColour.color(tone, style: style))
                            .lineLimit(1)
                    }
                }
                .widgetStyle(style)
            } compactTrailing: {
                Text(glance.text)
                    .font(style.font(size: 11, weight: .semibold))
                    .foregroundColor(ClusterHealthGlanceColour.color(glance, tone: tone, style: style))
                    .lineLimit(1)
            } minimal: {
                // One primary metric, and nothing else: the minimal region is a
                // glance at a glance, and a second number there is a number the
                // operator cannot read.
                HStack(spacing: 2) {
                    ClusterHealthGlyph(tone: tone).widgetStyle(style)
                    Text(glance.text)
                        .font(style.font(size: 11, weight: .semibold))
                        .foregroundColor(ClusterHealthGlanceColour.color(glance, tone: tone, style: style))
                        .lineLimit(1)
                }
                .widgetStyle(style)
            }
        }
    }
}

// MARK: - Shared rendering pieces

/// The activity's glyph: the fleet symbol, coloured by what the last read was.
/// Colour carries the state here so the compact regions stay text free.
struct ClusterHealthGlyph: View {
    @Environment(\.widgetStyle) private var style
    let tone: ClusterHealthTone

    var body: some View {
        Image(systemName: "square.stack.3d.up").widgetIcon(style)
            .foregroundColor(ClusterHealthToneColour.color(tone, style: style))
    }
}

/// The one place a `ClusterHealthTone` becomes a `Color`. The tone itself is
/// decided in the Foundation-only slice above (a `Color` there could not be
/// compiled by the macOS gate); mapping it once here is what keeps the Island's
/// compact regions and the Lock Screen header from inventing their own green.
enum ClusterHealthToneColour {
    static func color(_ tone: ClusterHealthTone, style: WidgetStyle) -> Color {
        switch tone {
        case .ok: return style.okColor
        case .warn: return style.warnColor
        case .bad: return style.badColor
        case .neutral: return style.secondaryTextColor
        }
    }
}

/// The colour of a `ClusterMetricLevel`, in `WidgetStyle` tokens.
///
/// `nil` — a column nobody thresholds, which is what GPU% is — is PRIMARY ink,
/// never the "unreadable" grey, and `.unknown` (a metric that was not read) is
/// grey rather than green: painting an absent metric like an idle one is §6
/// rule 1. That is deliberately the same mapping `CLIMonitorHostTableView.ink`
/// applies to the widget's host table, so the tile and this bubble cannot paint
/// one threshold two different ways; unifying the two copies into one shared
/// ink function is carded rather than done here, because that file is the
/// `systemMedium` slice's subject and this card must not widen a sibling's diff.
enum ClusterHealthMetricInk {
    static func color(_ level: ClusterMetricLevel?, style: WidgetStyle) -> Color {
        switch level ?? .ok {
        case .ok, .dim: return style.primaryTextColor
        case .warn: return style.warnColor
        case .critical: return style.badColor
        case .unknown: return style.secondaryTextColor
        }
    }
}

/// The colour of the compact / minimal primary metric.
///
/// A `heat` glance carries the threshold verdict of the host that HOLDS the peak,
/// so it paints with that (a hot GPU goes amber/red in the Island, which is the
/// whole reason a temperature is a legal primary there). Every other kind is
/// painted with the fleet's tone — including `load`, because neither the CLI nor
/// the view-model thresholds GPU utilisation, and a load number that went red
/// for a reason no other surface agrees on would be this file inventing a rule.
enum ClusterHealthGlanceColour {
    static func color(_ glance: ClusterHealthGlance,
                      tone: ClusterHealthTone,
                      style: WidgetStyle) -> Color {
        if glance.kind == .heat, let level = glance.level {
            return ClusterHealthMetricInk.color(level, style: style)
        }
        return ClusterHealthToneColour.color(tone, style: style)
    }
}

/// The per-host lines: one row per host with the CLI's `GPU%`, `GPU Temp` and
/// `GPU Power` cells, drawn on the Lock Screen and in the expanded Island.
///
/// The strings are the pushed state's, which are `ClusterMonitorViewModel`'s,
/// which are `host_table()`'s — this view concatenates nothing and formats
/// nothing. It clips to `limit` through `ClusterHealthReading.hostLineList` and
/// counts the rest with `hostsMoreText` against the route's own host count,
/// because a table that stops at four hosts without saying so reads as "the
/// fleet is four hosts".
struct ClusterHealthHostLines: View {
    @Environment(\.widgetStyle) private var style
    let signals: ClusterHealthSignals
    let limit: Int
    let labelFont: Font

    var body: some View {
        let rows = ClusterHealthReading.hostLineList(signals.hostLines, limit: limit)
        if !rows.isEmpty {
            VStack(alignment: .leading, spacing: 1) {
                ForEach(rows, id: \.label) { line in
                    HStack(spacing: 6) {
                        Text(line.label)
                            .font(labelFont)
                            .foregroundColor(line.isDown ? style.badColor : style.secondaryTextColor)
                            .lineLimit(1)
                        Spacer(minLength: 2)
                        // No verdict on GPU%: `host_table` prints that column
                        // unstyled and the view-model gives it no level, so it
                        // is primary ink — or grey, when the cell is the CLI's
                        // `-` for a metric the host could not read.
                        Text(line.gpuPct)
                            .font(labelFont)
                            .foregroundColor(cellInk(line.gpuPct, nil))
                            .monospacedDigit()
                        Text(line.gpuTemp)
                            .font(labelFont)
                            .foregroundColor(cellInk(line.gpuTemp, line.gpuTempLevel))
                            .monospacedDigit()
                        Text(line.gpuPower)
                            .font(labelFont)
                            .foregroundColor(cellInk(line.gpuPower, line.gpuPowerLevel))
                            .monospacedDigit()
                    }
                    .accessibilityLabel(accessibility(for: line))
                }
                if let more = ClusterHealthReading.hostsMoreText(shown: rows.count,
                                                                listed: signals.nodesTotal) {
                    Text(more)
                        .font(style.font(.caption2))
                        .foregroundColor(style.secondaryTextColor)
                }
            }
        }
    }

    /// A cell the CLI could not read (`-`) is grey whatever its verdict claims,
    /// because the verdict for an unread metric is `.unknown` anyway; a real
    /// cell paints with its level.
    private func cellInk(_ text: String, _ level: ClusterMetricLevel?) -> Color {
        if text == CLIMonitorFormat.dash { return style.secondaryTextColor }
        return ClusterHealthMetricInk.color(level, style: style)
    }

    /// One sentence per row, built from the same three cells a sighted operator
    /// reads — the VoiceOver route must not be a second, softer summary.
    private func accessibility(for line: ClusterHealthHostLine) -> String {
        "\(line.label), GPU \(line.gpuPct), GPU temp \(line.gpuTemp), draw \(line.gpuPower)"
            + (line.isDown ? ", not answering" : "")
    }
}

/// The fleet totals line: total GPU draw and the serving table's run/wait
/// totals. Both are `—`-shaped absence, never zeros, and each part appears only
/// if it was told.
///
/// The draw is painted with the worst per-host power verdict (grey when no host
/// reported a limit to measure against); the queue is painted with the CLI's own
/// load rule — `CLIMonitorFormat.loadLevel`, the ONE threshold `cluster_live.py`
/// applies (a waiting queue is warn, a busy engine is ok, an idle one is dim) —
/// mirrored by the view-model and never re-decided here.
struct ClusterHealthLoadSummary: View {
    @Environment(\.widgetStyle) private var style
    let signals: ClusterHealthSignals
    let font: Font

    var body: some View {
        let power = ClusterHealthReading.totalPowerText(signals.totalPowerW)
        let load = ClusterHealthReading.servingLoadText(running: signals.servingRunning,
                                                        waiting: signals.servingWaiting)
        if power != nil || load != nil {
            HStack(spacing: 6) {
                if let power {
                    HStack(spacing: 3) {
                        Image(systemName: "bolt.fill")
                            .font(style.font(.caption2))
                        Text(power)
                            .font(font)
                            .monospacedDigit()
                    }
                    .foregroundColor(ClusterHealthMetricInk.color(signals.powerLevel, style: style))
                }
                if let load {
                    Text(load)
                        .font(font)
                        .monospacedDigit()
                        .foregroundColor(ClusterHealthMetricInk.color(loadLevel, style: style))
                }
                Spacer(minLength: 2)
            }
            .accessibilityLabel([power.map { "total draw \($0)" },
                                 load.map { "serving \($0)" }]
                    .compactMap { $0 }
                    .joined(separator: ", "))
        }
    }

    /// The CLI's own colour rule for a queue, applied to the fleet totals. A
    /// total that was not told has no verdict — `.unknown`, grey — rather than
    /// the `.dim` the CLI reserves for a queue it measured and found empty.
    private var loadLevel: ClusterMetricLevel {
        guard signals.servingRunning != nil || signals.servingWaiting != nil else { return .unknown }
        return CLIMonitorFormat.loadLevel(running: signals.servingRunning ?? 0,
                                          waiting: signals.servingWaiting ?? 0)
    }
}
/// The detail region: the down nodes by name, or the one sentence that stands
/// in for them. Names come from the pushed state and are clipped to `limit`,
/// because a pushed ContentState is untrusted input to a fixed-height region.
struct ClusterHealthDetail: View {
    @Environment(\.widgetStyle) private var style
    let signals: ClusterHealthSignals
    let limit: Int
    let labelFont: Font

    var body: some View {
        let names = ClusterHealthReading.downNodeList(signals.downNodes, limit: limit)
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 6) {
                Text(ClusterHealthReading.tallyText(up: signals.nodesUp, total: signals.nodesTotal))
                    .font(labelFont)
                    .foregroundColor(ClusterHealthToneColour.color(ClusterHealthReading.tone(signals), style: style))
                Text(ClusterHealthReading.downText(down: signals.nodesDown))
                    .font(labelFont)
                    .foregroundColor(style.secondaryTextColor)
                Spacer(minLength: 4)
            }
            if !names.isEmpty {
                // Which node is down is the point of a health surface — a tally
                // alone cannot be acted on. `.NNN` tails only (§7); the pushed
                // state cannot carry an address in the first place.
                HStack(spacing: 6) {
                    ForEach(names, id: \.self) { name in
                        HStack(spacing: 3) {
                            Circle()
                                .fill(style.badColor)
                                .frame(width: 6, height: 6)
                            Text(name)
                                .font(labelFont)
                                .foregroundColor(style.secondaryTextColor)
                                .lineLimit(1)
                        }
                    }
                    if let more = ClusterHealthReading.moreText(shown: names.count,
                                                                down: signals.nodesDown) {
                        Text(more)
                            .font(style.font(.caption2))
                            .foregroundColor(style.secondaryTextColor)
                    }
                    Spacer(minLength: 4)
                }
            }
        }
    }
}

// MARK: - Lock Screen / banner view

/// The Lock Screen (and banner) presentation of the cluster-health activity.
struct LockScreenClusterHealthView: View {
    @Environment(\.widgetStyle) private var style
    let context: ActivityViewContext<ClusterHealthActivityAttributes>

    var body: some View {
        let signals = context.state.signals
        VStack(alignment: .leading, spacing: 8) {
            // Header: glyph + headline + age.
            HStack(spacing: 8) {
                ClusterHealthGlyph(tone: ClusterHealthReading.tone(signals))
                Text(ClusterHealthReading.headline(signals))
                    .font(style.font(.headline))
                    .foregroundColor(style.primaryTextColor)
                    .lineLimit(1)
                Spacer()
                Text(ClusterHealthReading.pushedAgeText(updatedAt: context.state.updatedAt,
                                                        now: Date()))
                    .font(style.font(.caption2))
                    .foregroundColor(style.secondaryTextColor)
            }
            .widgetHeader(style)

            // The host table: the CLI's three load columns per host, then the
            // fleet totals. Above the down-node detail on purpose — the Lock
            // Screen is the one surface with room for the table, and a health
            // surface whose per-host rows push the outage note off the bottom
            // has optimised for the wrong line. `ClusterHealthDetail` is short
            // (two names) and always follows.
            ClusterHealthHostLines(signals: signals,
                                   limit: ClusterHealthActivityAttributes.hostLineLimit,
                                   labelFont: style.font(size: 11, weight: .semibold)).widgetStyle(style)

            ClusterHealthLoadSummary(signals: signals,
                                     font: style.font(size: 11, weight: .semibold)).widgetStyle(style)

            ClusterHealthDetail(signals: signals,
                                limit: ClusterHealthActivityAttributes.downNodeLimit,
                                labelFont: style.font(size: 11, weight: .semibold))

            if let feed = ClusterHealthReading.feedText(model: signals.modelLabel,
                                                        genTps: signals.genTps) {
                Text(feed)
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
                    .lineLimit(1)
            }

            if let downLine = emptyLine(signals) {
                Text(downLine)
                    .font(style.font(.caption))
                    .foregroundColor(style.secondaryTextColor)
            }
        }
        .padding()
    }

    /// A blind read with nothing to fall back on says so; a read that answered
    /// and found every node healthy says the fleet is up. Those two must never
    /// share a line — that collapse is exactly how a dead cluster reads as fine.
    private func emptyLine(_ signals: ClusterHealthSignals) -> String? {
        if signals.isEmpty {
            if let failureLabel = signals.failureLabel, !failureLabel.isEmpty {
                return "\(failureLabel) — no cluster reading to show"
            }
            return "No cluster reading yet"
        }
        if signals.nodesDown == 0 && signals.downNodes.isEmpty {
            return "All nodes answering"
        }
        return nil
    }
}
