import SwiftUI

/// Live agent activity feed (the flight recorder) — the iOS surface for the
/// card's live-agent-activity feature.
///
/// Backed by `GET /v1/activity/feed` (routes_activity.py). It shows WHO is
/// running, WHICH tool they just called, on WHICH card — newest first — so an
/// operator can see what the fleet is doing at a glance. Read-only (no
/// `confirm`); reloads on pull-to-refresh and on first appearance.
///
/// Two kinds of row in one timeline:
///   * "Running" — a worker is on a card (emitted even when that profile has
///     no tool call in the window, so "who is on what" is never blank).
///   * "Tool" — a specific tool the profile just called, tied to its card.
///
/// LIST RECIPE (spec §5, Phase 2 C3): the old ScrollView of hand-rolled
/// `surfaceRaised` cards — the app's second row dialect — is gone. One plain
/// List; each row = leading status dot, title (headline) over machine
/// caption, trailing time + `HSStatusChip`. No boxes, no in-content headers.
///
/// Tap-to-trace: tapping a row pushes `ActivityTraceView` with that entry's
/// trace metadata (profile, session id, card, tool, timestamp) — the operator
/// can then open that profile's Sessions hub to see the full session.
struct ActivityFeedView: View {
    let client: HSCCClient?

    @State private var feed = LoadState<ActivityFeedResponse>.idle

    var body: some View {
        Group {
            if let client {
                switch feed {
                case .idle, .loading:
                    HSLoading("Loading…")
                case .failed(let message):
                    HSError("Couldn't load the feed", message: message) {
                        Task { await load(client) }
                    }
                case .stale(let state, let ageMessage):
                    feedList(state, staleMessage: ageMessage, client: client)
                case .loaded(let state):
                    feedList(state, staleMessage: nil, client: client)
                }
            } else {
                HSConnectGate(systemImage: "waveform.path.ecg",
                              verb: "to watch the live feed")
            }
        }
        .navigationTitle("Activity")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { if let client { await load(client) } }
        .task {
            if let client, feed.value == nil, !feed.isLoading {
                await load(client)
            }
        }
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        // Only show the explicit spinner on a first load (no value to fall back
        // to). On a refresh we keep holding the last-known value so a transient
        // failure degrades to `.stale` (last-known data, clearly labelled)
        // instead of blanking the screen.
        if feed.value == nil {
            feed = .loading
        }
        feed = await Offline.load(feed,
                                  cacheKey: EndpointPath.activityFeed,
                                  client: client) {
            try await client.activityFeed()
        }
    }

    // MARK: - List (the shared recipe)

    private func feedList(_ state: ActivityFeedResponse,
                          staleMessage: String?,
                          client: HSCCClient) -> some View {
        let items = state.entries ?? []
        return List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage,
                                reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                }
            }
            if !state.speak.isEmpty {
                // The feed's one-line summary — a recipe caption line, not a
                // boxed label like the card dialect used.
                Section {
                    Text(state.speak)
                        .font(.footnote.italic())
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            if items.isEmpty {
                Section {
                    HSEmpty("Idle",
                            message: "No agents running right now.",
                            systemImage: "tray")
                }
            } else {
                Section("Latest") {
                    ForEach(items) { entry in
                        NavigationLink {
                            ActivityTraceView(entry: entry)
                        } label: {
                            feedRow(entry)
                        }
                    }
                }
            }
        }
        .listStyle(.plain)
    }

    /// One timeline row, per the recipe: dot · title over machine caption ·
    /// trailing time + chip. VoiceOver gets ONE combined element per event
    /// (spec §9) so the rotor walks the timeline line by line.
    private func feedRow(_ entry: ActivityEntry) -> some View {
        let tint = entry.isRunning ? Theme.Semantic.warn : Theme.Semantic.ok
        return HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.sm.rawValue) {
            HSStatusDot(tint)
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(entry.profile ?? "unknown profile")
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Text(entry.isRunning
                     ? "running " + (entry.card_title ?? "a card") + cardRef(entry.card_id)
                     : (entry.tool ?? "tool")
                        + (entry.card_title.map { " on " + $0 } ?? "")
                        + cardRef(entry.card_id))
                    .font(.hsccMono(.footnote))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
            Spacer(minLength: Theme.Spacing.sm.rawValue)
            VStack(alignment: .trailing, spacing: Theme.Spacing.xxs.rawValue) {
                if let at = entry.at {
                    // Spec §5: the relative time is TICKED, not computed once.
                    // `Text(timeLabel(at))` rendered "3 minutes ago" and then
                    // sat there for the rest of the session — a feed you keep
                    // open drifts into lying about how old its events are.
                    // TimelineView re-evaluates the label every 30 s while the
                    // feed is on screen (30 s because the smallest bucket the
                    // phrasing shows is a minute; a faster tick would repaint
                    // nothing).
                    TimelineView(.periodic(from: .now, by: 30)) { context in
                        Text(timeLabel(at, now: context.date))
                            .font(.hsccMono(.caption2))
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                kindBadge(entry)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilityLine(for: entry))
    }

    private func accessibilityLine(for entry: ActivityEntry) -> String {
        let who = entry.profile ?? "unknown profile"
        if entry.isRunning {
            return "Running, \(who), on \(entry.card_title ?? "a card")"
        }
        return "Tool call, \(who), \(entry.tool ?? "tool")"
    }

    // MARK: - Bits

    private func kindBadge(_ entry: ActivityEntry) -> some View {
        // The shared chip (spec §6 inventory: kindBadge is one of the six
        // bespoke badges HSStatusChip replaces). Colour role kept — running
        // warns, settled is ok.
        HSStatusChip(entry.kindLabel,
                     color: entry.isRunning ? Theme.Semantic.warn : Theme.Semantic.ok)
    }

    /// A compact "how long ago" label for an ISO timestamp (newest-first feed
    /// reads best as relative time). Falls back to the raw timestamp.
    /// `now` is injected by the row's `TimelineView` so the label ages without
    /// a manual refresh; the default keeps the helper usable from one-shot
    /// render sites.
    private func timeLabel(_ iso: String, now: Date = Date()) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        let seconds = now.timeIntervalSince(date)
        return Offline.agePhrase(seconds)
    }

    /// e.g. " (t_abc)" — a compact card reference suffix, or "" when unknown.
    private func cardRef(_ cardID: String?) -> String {
        guard let cardID, !cardID.isEmpty else { return "" }
        return " (\(cardID))"
    }
}

/// Tap-to-trace detail: the full trace metadata for one activity entry.
///
/// Tapping a feed row pushes here, answering "where does this lead?" — which
/// profile, which session, which card, which tool, at what time. It does NOT
/// attempt to replay the conversation (there is no deep session-trace viewer
/// in the app); it hands the operator the exact coordinates to pull the
/// session up from the Sessions hub.
struct ActivityTraceView: View {
    let entry: ActivityEntry

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Theme.Spacing.lg.rawValue) {
                header
                traceCard("Tool", value: entry.isRunning ? "—" : (entry.tool ?? "—"))
                traceCard("Profile", value: entry.profile ?? "—")
                traceCard("Card", value: entry.card_id ?? "—")
                if let title = entry.card_title, !title.isEmpty {
                    traceCard("Card title", value: title)
                }
                traceCard("Session", value: entry.session_id ?? (entry.isRunning ? "n/a (running row)" : "—"))
                traceCard("Board", value: entry.board ?? "—")
                if let pid = entry.pid {
                    traceCard("PID", value: "\(pid)")
                }
                if let started = entry.started_at {
                    traceCard("Started", value: started)
                }
                if let at = entry.at {
                    traceCard("At", value: at)
                }
            }
            .padding()
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .navigationTitle(entry.profile ?? "Trace")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(entry.isRunning ? "Running" : "Tool call")
                .font(.subheadline.bold())
                .foregroundColor(entry.isRunning ? Theme.Semantic.warn : Theme.Semantic.ok)
            Text(entry.isRunning
                 ? (entry.card_title ?? entry.card_id ?? "a card")
                 : (entry.tool ?? "unknown tool"))
                .font(.title2.bold())
        }
    }

    private func traceCard(_ label: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Text(label)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            Text(value)
                .font(.body)
                .textSelection(.enabled)
                .foregroundColor(Theme.Semantic.onSurface)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.card.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }
}
