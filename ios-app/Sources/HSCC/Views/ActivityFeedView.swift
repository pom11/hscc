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
/// Tap-to-trace: tapping a row pushes `ActivityTraceView` with that entry's
/// trace metadata (profile, session id, card, tool, timestamp) — the operator
/// can then open that profile's Sessions hub to see the full session.
struct ActivityFeedView: View {
    let client: HSCCClient?

    @State private var feed = LoadState<ActivityFeedResponse>.idle

    var body: some View {
        ScrollView {
            if let client {
                VStack(alignment: .leading, spacing: 16) {
                    listSection(client)
                }
                .padding()
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Activity")
        .refreshable { if let client { await load(client) } }
        .task {
            if let client, feed.value == nil, !feed.isLoading {
                await load(client)
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "waveform.path.ecg")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to watch the live feed.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        feed = .loading
        do {
            feed = .loaded(try await client.activityFeed())
        } catch {
            feed = .failed(errorMessage(for: error))
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
    }

    // MARK: - List

    private func listSection(_ client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Cluster Activity", systemImage: "waveform.path.ecg")
                .font(.headline)
            switch feed {
            case .loading:
                ProgressView()
                    .frame(maxWidth: .infinity, alignment: .leading)
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 8) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(.secondary)
                    let items = state.entries ?? []
                    if items.isEmpty {
                        emptyLabel("No agents running right now.")
                    } else {
                        ForEach(items) { entry in
                            entryRow(entry)
                            if entry.id != items.last?.id {
                                Divider()
                            }
                        }
                    }
                }
            default:
                EmptyView()
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    private func entryRow(_ entry: ActivityEntry) -> some View {
        NavigationLink {
            ActivityTraceView(entry: entry)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    kindBadge(entry)
                    Text(entry.profile ?? "unknown profile")
                        .font(.headline)
                        .lineLimit(1)
                    Spacer()
                    if let at = entry.at {
                        Text(timeLabel(at))
                            .font(.caption2.monospacedDigit())
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                if entry.isRunning {
                    Text("running \(entry.card_title ?? "a card")"
                         + cardRef(entry.card_id))
                        .font(.subheadline)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                } else {
                    HStack(spacing: 6) {
                        Image(systemName: "wrench.and.screwdriver")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.ok)
                        Text(entry.tool ?? "tool")
                            .font(.subheadline)
                            .foregroundColor(Theme.Semantic.onSurface)
                    }
                    if let cardTitle = entry.card_title {
                        Text("on \(cardTitle)" + cardRef(entry.card_id))
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            }
        }
        .buttonStyle(.plain)
    }

    /// e.g. " (t_abc)" — a compact card reference suffix, or "" when unknown.
    private func cardRef(_ cardID: String?) -> String {
        guard let cardID, !cardID.isEmpty else { return "" }
        return " (\(cardID))"
    }

    private func kindBadge(_ entry: ActivityEntry) -> some View {
        let tint: Color = entry.isRunning ? Theme.Semantic.warn : Theme.Semantic.ok
        return Text(entry.kindLabel)
            .font(.caption2.bold())
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(tint.opacity(0.18)))
            .foregroundColor(tint)
    }

    /// A compact "how long ago" label for an ISO timestamp (newest-first feed
    /// reads best as relative time). Falls back to the raw timestamp.
    private func timeLabel(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        let seconds = Date().timeIntervalSince(date)
        return Offline.agePhrase(seconds)
    }

    private func errorLabel(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.bad)
    }

    private func emptyLabel(_ text: String) -> some View {
        Label(text, systemImage: "tray")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
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
            VStack(alignment: .leading, spacing: 16) {
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
        VStack(alignment: .leading, spacing: 4) {
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
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }
}
