import SwiftUI

/// Watchdog & self-heal history (t_b5ce7935) — a timeline of what the daemon's
/// automation actually DID: watchdog restarts, pipeline blocks/escalations,
/// autodown transitions, and recovery relaunches — with timestamps, outcomes
/// and durations.
///
/// This is the companion to LogsView ("what just wedged?") and OpsView
/// ("current rules"): it answers "what did the cluster decide while I was
/// away?" The server (GET /v1/daemon/history) reverse-scans a bounded window
/// of daemon.log, classifies the automated-action lines, and COLLAPSES the
/// 30s-cycle repeats into distinct events carrying ``repeats``/``last_ts`` —
/// so a multi-hour "autodown in effect" window renders as one timeline entry,
/// not hundreds of identical rows.
///
/// Design constraints (same as LogsView):
///   * Read-only — no mutate/confirm path anywhere.
///   * Bounded — requests at most `maxEvents` distinct events; the server
///     clamps further and reads O(window), never the whole log.
///   * Honest states — loading / error+retry / empty via the shared
///     HSLoading / HSError / HSEmpty components; pull-to-refresh.
/// Machine-produced values (timestamps, counts, ids) use the mono face.
struct SelfHealHistoryView: View {
    let client: HSCCClient?

    /// How many distinct events the view ever requests (bounded; server caps
    /// likewise so the payload is always small).
    private let maxEvents = 200

    @State private var state = LoadState<[HistoryEvent]>.idle

    var body: some View {
        NavigationStack {
            Group {
                if let client {
                    content(client)
                } else {
                    HSConnectGate(systemImage: "clock.arrow.circlepath",
                                  verb: "to see automation history")
                }
            }
            .navigationTitle("Self-Heal History")
        }
    }

    // MARK: - Configured content

    private func content(_ client: HSCCClient) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.lg.rawValue) {
                summaryCard

                switch state {
                case .loading:
                    HSLoading("Scanning daemon log…")
                        .frame(minHeight: 300)
                case .failed(let message):
                    HSError("Couldn't load history",
                            message: message) {
                        Task { await load(client) }
                    }
                    .frame(minHeight: 300)
                case .stale(let events, let ageMessage):
                    StaleBanner(age: ageMessage,
                                reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                    timelineBody(events)
                case .loaded(let events):
                    timelineBody(events)
                default:
                    // idle: nothing loaded yet — let .task trigger the first load.
                    HSLoading("Loading automation history…")
                        .frame(minHeight: 300)
                }
            }
            .padding()
        }
        .refreshable { await load(client) }
        .task {
            if state.value == nil, !state.isLoading { await load(client) }
        }
    }

    // MARK: - Summary card

    /// A compact count strip: how many of each kind appear in the loaded
    /// timeline. Reveals the shape of a night's automation at a glance.
    @ViewBuilder
    private var summaryCard: some View {
        if let events = state.value, !events.isEmpty {
            VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                HStack(spacing: Theme.Spacing.md.rawValue) {
                    summaryBlock("Restarts", count(events, .restart), Theme.Semantic.warn, "arrow.triangle.2.circlepath")
                    summaryBlock("Blocks", count(events, .block), Theme.Semantic.bad, "hand.raised.fill")
                    summaryBlock("Autodown", count(events, .autodown), Theme.Semantic.warn, "powersleep")
                    summaryBlock("Recoveries", count(events, .recovery), Theme.Semantic.ok, "arrow.clockwise.heart")
                }
                Text("Distinct automated decisions in the recent window — run-collapsed.")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding()
            .background(
                RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                    .fill(Theme.Semantic.surfaceRaised)
            )
        }
    }

    private func count(_ events: [HistoryEvent], _ kind: HistoryEventKind) -> Int {
        events.lazy.filter { $0.kind == kind }.count
    }

    private func summaryBlock(_ title: String, _ n: Int, _ color: Color, _ icon: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            HStack(spacing: Theme.Spacing.xxs.rawValue) {
                Image(systemName: icon).font(.caption).foregroundColor(color)
                Text(title).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            Text("\(n)")
                .font(.hsccMono(20, weight: .bold))
                .foregroundColor(Theme.Semantic.onSurface)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Timeline

    @ViewBuilder
    private func timelineBody(_ events: [HistoryEvent]) -> some View {
        // Server returns oldest-first (it scans backward and reverses). For a
        // chronological "what happened most recently" read we show newest on
        // top.
        let newestFirst = events.reversed()
        if newestFirst.isEmpty || events.isEmpty {
            HSEmpty("No automation history",
                    message: "No watchdog restarts, blocks, autodown or recovery events were found in the recent window.",
                    systemImage: "clock.arrow.circlepath")
        } else {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(Array(newestFirst.enumerated()), id: \.element.id) { index, event in
                    HistoryRow(event: event)
                    if index != newestFirst.count - 1 { Divider() }
                }
            }
            .background(
                RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                    .fill(Theme.Semantic.surfaceRaised)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                    .strokeBorder(Theme.Semantic.onSurface.opacity(0.08), lineWidth: 1)
            )
            footerText(events.count)
        }
    }

    private func footerText(_ n: Int) -> some View {
        Text("\(n) distinct automated \(n == 1 ? "decision" : "decisions") in the recent window. Read-only.")
            .font(.caption2)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, Theme.Spacing.xs.rawValue)
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        let current = state
        state = .loading
        do {
            let response = try await client.daemonHistory(limit: maxEvents)
            state = .loaded(response.events)
        } catch {
            if let held = current.value, !held.isEmpty {
                state = .stale(held, "showing earlier history")
            } else {
                state = .failed(operatorErrorMessage(error))
            }
        }
    }
}

/// One timeline row: an event kind chip + outcome label over a mono timestamp
/// and the redacted detail line, with a duration caption for collapsed runs.
private struct HistoryRow: View {
    let event: HistoryEvent

    private var color: Color { event.resolvedOutcome.tint }
    private var kindLabel: String { event.kind?.label ?? "Event" }
    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.md.rawValue) {
            // Leading marker on the timeline spine.
            Circle()
                .fill(color)
                .frame(width: 10, height: 10)
                .padding(.top, 6)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    HSStatusChip(kindLabel, systemImage: kindIcon, color: color)
                    Text(outcomeText)
                        .font(.caption.weight(.semibold))
                        .foregroundColor(color)
                    Spacer(minLength: 0)
                }

                if let detail = event.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.footnote.monospaced())
                        .foregroundColor(Theme.Semantic.onSurface)
                        .textSelection(.enabled)
                }

                HSMetaLine([timestampText, durationText].filter { !$0.isEmpty })
            }
            Spacer(minLength: 0)
        }
        .padding(Theme.Spacing.md.rawValue)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var outcomeText: String {
        switch event.resolvedOutcome {
        case .success: return "Success"
        case .failed:  return "Failed"
        case .cleared: return "Cleared"
        case .attempt: return "Attempted"
        case .wake:    return "Wake"
        case .started: return "Started"
        case .recon:   return "Reconciled"
        case .info:    return "Info"
        }
    }

    private var kindIcon: String {
        switch event.kind {
        case .restart:  return "arrow.triangle.2.circlepath"
        case .block:    return "hand.raised.fill"
        case .autodown: return "powersleep"
        case .recovery: return "arrow.clockwise.heart"
        case .escalate: return "exclamationmark.triangle"
        case nil:       return "clock.arrow.circlepath"
        }
    }

    /// Compact local timestamp from the ISO-8601 string the server serves.
    /// Parses tolerantly and falls back to the raw string — never blanks.
    private var timestampText: String {
        guard let ts = event.timestamp, !ts.isEmpty else { return "no timestamp" }
        // "2026-08-27T07:12:31" -> "Aug 27, 07:12"
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: ts)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: ts)
        }
        guard let date else { return ts }
        let out = DateFormatter()
        out.dateFormat = "MMM d HH:mm"
        return out.string(from: date)
    }

    /// Human duration for a collapsed run ("for 26 min") — empty for a single
    /// event or when the run dimensions are missing.
    private var durationText: String {
        guard let last = event.lastTs, let first = event.timestamp else { return "" }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var a = f.date(from: first)
        var b = f.date(from: last)
        if a == nil { f.formatOptions = [.withInternetDateTime]; a = f.date(from: first) }
        if b == nil { f.formatOptions = [.withInternetDateTime]; b = f.date(from: last) }
        guard let a, let b else { return "" }
        let seconds = Int(b.timeIntervalSince(a))
        guard seconds > 0 else { return "" }
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "for \(minutes) min" }
        let hours = minutes / 60
        return "for \(hours) h \(minutes % 60) min"
    }
}

/// Row accent colour per outcome (from the app's semantic roles). Lives in the
/// view layer — the model stays UI-free (Foundation only) so it can decode in
/// the headless fixture harness.
extension HistoryOutcome {
    var tint: Color {
        switch self {
        case .success, .cleared: return Theme.Semantic.ok
        case .failed:            return Theme.Semantic.bad
        case .recon, .wake, .attempt, .started: return Theme.Semantic.warn
        case .info:              return Theme.Semantic.neutral
        }
    }
}

