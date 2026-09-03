import SwiftUI

/// Read-only log view (t_2eda26a6) — tail the daemon, API, or worker log
/// from the phone.
///
/// This is the operator's "what just wedged?" surface, replacing SSH for the
/// common case of wanting the most recent lines. Design constraints:
///
///   * **Read-only** — there is no mutate/confirm path anywhere in this view.
///   * **Bounded** — it requests at most `maxLines` (200) recent lines and
///     renders exactly what comes back. It NEVER loads a whole file or fetches
///     an unbounded body into memory.
///   * **Pull-to-refresh** — SwiftUI `.refreshable` on the ScrollView.
///   * **Redacted** — every line is passed through `LogRedactor` before it is
///     displayed, so the view only ever holds redacted text. The backend is
///     required to redact first and this is a second line of defence.
///
/// SECURITY: no raw (unredacted) log line is ever stored, printed, or written
/// by the app. `LogRedactor.redact` is the only place raw text exists, as a
/// transient function argument.
struct LogsView: View {
    let client: HSCCClient?

    /// How many recent lines the view ever requests (bounds the tail — never
    /// an unbounded file, and the client further caps at 200 server-side).
    private let maxLines = 200

    @State private var source: LogSource = .daemon
    @State private var state = LoadState<LogsResponse>.idle

    var body: some View {
        NavigationStack {
            Group {
                if let client {
                    content(client)
                } else {
                    HSConnectGate(systemImage: "doc.text.magnifyingglass",
                                  verb: "to tail logs")
                }
            }
            .navigationTitle("Logs")
        }
    }

    // MARK: - Configured content

    private func content(_ client: HSCCClient) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                sourcePicker

                switch state {
                case .loading:
                    logPlaceholder("Fetching \(source.label.lowercased()) log…")
                        .overlay { ProgressView() }
                case .failed(let message):
                    HSErrorLabel(message: message)
                case .stale(let entries, let ageMessage):
                    StaleBanner(age: ageMessage,
                                reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                    logBody(entries)
                case .loaded(let entries):
                    logBody(entries)
                default:
                    // idle: nothing loaded yet — let .task trigger the first load.
                    ProgressView().frame(maxWidth: .infinity, minHeight: 200)
                }
            }
            .padding()
        }
        .refreshable { await load(client) }
        .task {
            if state.value == nil, !state.isLoading { await load(client) }
        }
        .onChange(of: source) {
            Task { await load(client) }
        }
    }

    // MARK: - Source picker

    private var sourcePicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("Log source", selection: $source) {
                ForEach(LogSource.allCases) { s in
                    Text(s.label).tag(s)
                }
            }
            .pickerStyle(.segmented)
            Text(source.subtitle)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    // MARK: - Log body

    /// The redacted, bounded tail. `entries` are ALREADY redacted (see
    /// `load`); this method never touches raw text.
    @ViewBuilder
    private func logBody(_ rawEntries: [LogEntry]) -> some View {
        // Defence in depth: redact again right before render, even though
        // `load` already redacted. Cheap and guarantees the view can never
        // hold a raw line even if a future caller skips the load path.
        let entries = LogRedactor.redactMany(rawEntries)
        if entries.isEmpty {
            HSEmptyLabel(message: "No \(source.label.lowercased()) log lines returned.")
        } else {
            LazyVStack(alignment: .leading, spacing: 0) {
                ForEach(entries) { entry in
                    LogRow(entry: entry)
                    if entry.id != entries.last?.id {
                        Divider()
                    }
                }
            }
            .background(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Theme.Semantic.surfaceRaised)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .strokeBorder(Theme.Semantic.onSurface.opacity(0.08), lineWidth: 1)
            )
            footer
        }
    }

    private var footer: some View {
        Text("Showing up to the \(maxLines) most recent lines. Redacted client-side.")
            .font(.caption2)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func logPlaceholder(_ text: String) -> some View {
        Text(text)
            .font(.callout)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
            .frame(maxWidth: .infinity, minHeight: 200)
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        let current = state
        state = .loading
        do {
            let raw = try await client.logs(source: source, limit: maxLines)
            // Redact client-side BEFORE the bytes are ever rendered/stored in
            // the view. `state` only ever holds redacted entries.
            state = .loaded(LogRedactor.redactMany(raw))
        } catch {
            if let cached = client.cachedValue(LogsResponse.self, for: "/v1/logs") {
                state = .stale(LogRedactor.redactMany(cached),
                               "showing tail fetched earlier")
            } else if let held = current.value {
                state = .stale(LogRedactor.redactMany(held), "showing earlier tail")
            } else {
                state = .failed(operatorErrorMessage(error))
            }
        }
    }
}

/// One log row: severity badge + timestamp + redacted line, monospaced so
/// aligned text reads like a real terminal tail.
private struct LogRow: View {
    let entry: LogEntry

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if let level = entry.level, !level.isEmpty {
                Text(level)
                    .font(.caption2.weight(.bold))
                    .foregroundColor(tint)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(tint.opacity(0.14))
                    .clipShape(Capsule())
            }
            VStack(alignment: .leading, spacing: 2) {
                if let ts = entry.timestamp, !ts.isEmpty {
                    Text(ts)
                        .font(.caption2.monospaced())
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                Text(entry.line ?? "")
                    .font(.footnote.monospaced())
                    .foregroundColor(Theme.Semantic.onSurface)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }

    private var tint: Color {
        switch (entry.level ?? "").uppercased() {
        case "ERROR", "FATAL", "CRITICAL", "PANIC": return Theme.Semantic.bad
        case "WARN", "WARNING":                      return Theme.Semantic.warn
        case "DEBUG", "TRACE":                       return Theme.Semantic.neutral
        default:                                     return Theme.Semantic.ok
        }
    }
}
