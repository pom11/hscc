import SwiftUI

/// Phase B3 — the review queue (GET /v1/review/queue) with tap-through to a
/// read-only review detail (GET /v1/review/{id}).
///
/// The review detail endpoint is a DRY RUN: it only computes branch/merge facts
/// and never merges or closes a card. This view therefore offers NO button that
/// dispatches, merges, applies, or stops — a tap only navigates to the facts.
/// Mutating actions land in B4 behind an explicit confirm UI.
struct ReviewQueueView: View {
    @EnvironmentObject private var settings: SettingsStore

    @State private var queue: ReviewQueueResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if let loadError {
                    errorView(loadError)
                } else if let queue {
                    content(queue)
                } else {
                    ProgressView("Loading…")
                }
            }
            .navigationTitle("Review")
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
    private func content(_ response: ReviewQueueResponse) -> some View {
        List {
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }

            if response.queue.isEmpty {
                Section {
                    Text("Nothing awaiting review.")
                        .foregroundColor(.secondary)
                }
            }

            ForEach(response.queue) { row in
                NavigationLink {
                    ReviewDetailView(cardID: row.id)
                } label: {
                    rowView(row)
                }
            }
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func rowView(_ row: ReviewQueueRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(row.displayTitle)
                .font(.body)
            HStack(spacing: 6) {
                if let project = row.project, !project.isEmpty {
                    Text(project).font(.caption).foregroundColor(.secondary)
                }
                if let branch = row.branch, !branch.isEmpty {
                    Text(branch).font(.caption).monospaced().foregroundColor(.secondary)
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
            Label("Couldn't load review queue", systemImage: "exclamationmark.triangle")
        } description: {
            Text(err.localizedDescription)
        } actions: {
            Button("Try again") { Task { await load() } }
        }
    }

    private func timeAgo(_ seconds: Int) -> String {
        let interval = TimeInterval(max(0, seconds))
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: Date(timeIntervalSinceNow: -interval),
                                        relativeTo: Date())
    }

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
            queue = try await client.reviewQueue()
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}

/// Phase B3 — DRY-RUN review facts for one card (GET /v1/review/{id}).
///
/// Read-only by construction: the endpoint never merges and never closes. This
/// screen only presents the facts (branch state, diff stats, merge conflicts,
/// the VERIFY line) and the server's `speak` verdict. There is deliberately
/// no merge/apply button here — that is B4's confirm-gated surface.
struct ReviewDetailView: View {
    @EnvironmentObject private var settings: SettingsStore
    let cardID: String

    @State private var review: ReviewDetailResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        Group {
            if let loadError {
                ContentUnavailableView {
                    Label("Couldn't load review", systemImage: "exclamationmark.triangle")
                } description: {
                    Text(loadError.localizedDescription)
                } actions: {
                    Button("Try again") { Task { await load() } }
                }
            } else if let review {
                content(review)
            } else {
                ProgressView("Loading…")
            }
        }
        .navigationTitle(review?.displayTitle ?? "Review")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
    }

    @ViewBuilder
    private func content(_ review: ReviewDetailResponse) -> some View {
        List {
            // The whole reason this endpoint exists: the read-only verdict.
            Section("Verdict") {
                Label(review.speak, systemImage: "text.bubble")
                    .font(.subheadline)
                Label(review.mergeClause, systemImage: mergeIcon(review))
                    .font(.subheadline)
                    .foregroundColor(mergeColor(review))
            }

            Section("Card") {
                row("ID", value: review.id)
                row("Title", value: review.title)
                row("Board", value: review.board)
                row("Project", value: review.project)
            }

            Section("Branch") {
                row("Repo", value: review.repo)
                row("Branch", value: review.branch)
                row("Base", value: review.base)
                row("Subject", value: review.subject)
            }

            Section("Diff") {
                statRow("Files changed", Int(review.files_changed ?? 0))
                statRow("Insertions", Int(review.insertions ?? 0))
                statRow("Deletions", Int(review.deletions ?? 0))
            }

            if let verify = review.verify, !verify.isEmpty {
                Section("VERIFY") {
                    Text(verify)
                        .font(.body.monospaced())
                }
            }

            if let dependents = review.dependents, !dependents.isEmpty {
                Section("Dependents") {
                    ForEach(dependents, id: \.self) { Text($0) }
                }
            }

            Section {
                Text("Read-only dry run — nothing is merged or changed here.")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
        }
    }

    @ViewBuilder
    private func row(_ label: String, value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(label) { Text(value).textSelection(.enabled) }
        }
    }

    @ViewBuilder
    private func statRow(_ label: String, _ value: Int) -> some View {
        LabeledContent(label) { Text("\(value)") }
    }

    private func mergeIcon(_ review: ReviewDetailResponse) -> String {
        if let landed = review.landed, landed { return "checkmark.seal" }
        if let conflicts = review.conflicts, conflicts > 0 { return "exclamationmark.triangle" }
        return "checkmark.circle"
    }

    private func mergeColor(_ review: ReviewDetailResponse) -> Color {
        if let landed = review.landed, landed { return .green }
        if let conflicts = review.conflicts, conflicts > 0 { return .orange }
        return .green
    }

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
            review = try await client.reviewDetail(cardID)
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}
