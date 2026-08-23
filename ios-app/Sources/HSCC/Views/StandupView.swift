import SwiftUI

/// Phase B3 — the daily digest (GET /v1/standup).
///
/// Shows what needs attention: review/blocked cards (needs_you), failing
/// verifies, stale cards, running work, project drift, and unreadable
/// projects. The server-derived `speak` one-liner is surfaced as the summary
/// at the top. READ-ONLY: every interaction is a navigation or a pull-to-
/// refresh — nothing here dispatches, merges, applies, or stops anything.
struct StandupView: View {
    @EnvironmentObject private var settings: SettingsStore

    @State private var standup: StandupResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    /// Ordered section (title, key path on the response, accent color).
    private let sections: [(String, KeyPath<StandupResponse, [StandupRow]?>, Color)] = [
        ("Needs You", \.needs_you, .orange),
        ("Failing", \.failing, .red),
        ("Stale", \.stale, .yellow),
        ("Running", \.running, .blue),
        ("Drift", \.drift, .purple),
        ("Unreadable", \.unreadable, .secondary),
    ]

    var body: some View {
        NavigationStack {
            Group {
                if let loadError {
                    errorView(loadError)
                } else if let standup {
                    content(standup)
                } else {
                    ProgressView("Loading…")
                }
            }
            .navigationTitle("Standup")
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button {
                        Task { await load() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(isLoading)
                }
            }
            .task { await load() }
        }
    }

    @ViewBuilder
    private func content(_ digest: StandupResponse) -> some View {
        List {
            // The server-computed summary line (design §B: read `speak` aloud).
            Section {
                Label(digest.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }

            ForEach(sections, id: \.0) { title, keyPath, color in
                let rows = digest[keyPath: keyPath] ?? []
                if !rows.isEmpty {
                    Section(title) {
                        ForEach(rows) { row in
                            rowView(row, accent: color)
                        }
                    }
                }
            }

            if isEmpty(digest) {
                Section {
                    Text("Nothing needs attention.")
                        .foregroundColor(.secondary)
                }
            }
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func rowView(_ row: StandupRow, accent: Color) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(row.displayTitle)
                .font(.body)
            HStack(spacing: 6) {
                if let board = row.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(.secondary)
                }
                if let project = row.project, !project.isEmpty {
                    Text(project).font(.caption).foregroundColor(.secondary)
                }
                let kind = row.displayKind
                if !kind.isEmpty {
                    Text(kind).font(.caption).padding(.horizontal, 6).padding(.vertical, 2)
                        .background(accent.opacity(0.15), in: Capsule())
                        .foregroundColor(accent)
                }
            }
            if let age = row.age_seconds {
                Text(timeAgo(age)).font(.caption2).foregroundColor(.secondary)
            }
        }
    }

    @ViewBuilder
    private func errorView(_ err: HSCCError) -> some View {
        ContentUnavailableView {
            Label("Couldn't load standup", systemImage: "exclamationmark.triangle")
        } description: {
            Text(err.localizedDescription)
        } actions: {
            Button("Try again") { Task { await load() } }
        }
    }

    private func isEmpty(_ digest: StandupResponse) -> Bool {
        sections.allSatisfy { (digest[keyPath: $0.1] ?? []).isEmpty }
    }

    private func timeAgo(_ seconds: Int) -> String {
        let interval = TimeInterval(max(0, seconds))
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: Date(timeIntervalSinceNow: -interval),
                                        relativeTo: Date())
    }

    // MARK: - Loading

    private func load() async {
        guard !isLoading else { return }
        guard settings.isConfigured,
              let token = settings.token,
              let port = Int(settings.port) else {
            loadError = .invalidURL
            return
        }
        isLoading = true
        defer { isLoading = false }
        let client = HSCCClient(host: settings.host, port: port, token: token)
        do {
            standup = try await client.standup()
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}
