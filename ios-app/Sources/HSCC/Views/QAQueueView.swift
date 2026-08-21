import SwiftUI

/// Phase B3 — the QA queue (GET /v1/qa/queue).
///
/// Shows the pre-merge QA rows (cards needing a VERIFY run) and the manual-QA
/// store (product-manager style verification entries). READ-ONLY: nothing here
/// checks off a manual entry or mutates any state — a tap only navigates or
/// refreshes. Any mutation of manual-QA lands in B4.
struct QAQueueView: View {
    @EnvironmentObject private var settings: SettingsStore

    @State private var qa: QAQueueResponse?
    @State private var loadError: HSCCError?
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if let loadError {
                    errorView(loadError)
                } else if let qa {
                    content(qa)
                } else {
                    ProgressView("Loading…")
                }
            }
            .navigationTitle("QA")
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
    private func content(_ response: QAQueueResponse) -> some View {
        List {
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }

            // Pre-merge QA rows.
            Section("Needs QA") {
                if response.queue.isEmpty {
                    Text("Nothing needs manual testing.")
                        .foregroundColor(.secondary)
                } else {
                    ForEach(response.queue) { row in
                        qaRow(row)
                    }
                }
            }

            // Manual-QA store.
            let manual = response.manual_qa ?? []
            Section("Manual verification") {
                if manual.isEmpty {
                    Text("No manual entries.")
                        .foregroundColor(.secondary)
                } else {
                    ForEach(manual) { entry in
                        manualRow(entry)
                    }
                }
            }
        }
        .refreshable { await load() }
    }

    @ViewBuilder
    private func qaRow(_ row: QARow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: row.verify_passed == true ? "checkmark.circle.fill"
                            : row.verify_run == true ? "clock"
                            : row.verify_configured == true ? "circle.dashed"
                            : "questionmark.circle")
                    .foregroundColor(row.verify_passed == true ? .green : .secondary)
                Text(row.displayTitle)
                    .font(.body)
            }
            HStack(spacing: 6) {
                if let project = row.project, !project.isEmpty {
                    Text(project).font(.caption).foregroundColor(.secondary)
                }
                if let branch = row.branch, !branch.isEmpty {
                    Text(branch).font(.caption).monospaced().foregroundColor(.secondary)
                }
                if let files = row.files_changed {
                    Text("\(files) files").font(.caption).foregroundColor(.secondary)
                }
            }
            if let verify = row.verify, !verify.isEmpty {
                Text(verify).font(.caption2).foregroundColor(.secondary)
                    .lineLimit(2)
            }
        }
    }

    @ViewBuilder
    private func manualRow(_ entry: ManualQARow) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: entry.checked == true ? "checkmark.square.fill"
                                                    : "square")
                .foregroundColor(entry.checked == true ? .green : .secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.displayDescription)
                    .font(.body)
                    .strikethrough(entry.checked == true)
                if let project = entry.project, !project.isEmpty {
                    Text(project).font(.caption).foregroundColor(.secondary)
                }
            }
        }
    }

    @ViewBuilder
    private func errorView(_ err: HSCCError) -> some View {
        ContentUnavailableView {
            Label("Couldn't load QA queue", systemImage: "exclamationmark.triangle")
        } description: {
            Text(err.localizedDescription)
        } actions: {
            Button("Try again") { Task { await load() } }
        }
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
            qa = try await client.qaQueue()
            loadError = nil
        } catch {
            loadError = (error as? HSCCError) ?? .transport(underlying: nil)
        }
    }
}
