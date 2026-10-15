import SwiftUI

/// Scheduled-job roster (Phase 3) — the read-only cron view.
///
/// Consumes `GET /v1/cron/list` (routes_cron.py) and shows the operator the
/// WHOLE scheduled-job population — active and paused — each with its
/// schedule, next/last run, and last outcome. Backed by
/// `hscc_daemon.autodown.list_all_cron_jobs`, so the roster reflects the same
/// on-disk source of truth the daemon itself uses.
///
/// LIST RECIPE (spec §5, Phase 2 C3): one plain `List` — leading status dot,
/// job name (`headline`) over the mono schedule line, trailing `HSStatusChip`
/// for active/paused. The old `HSSectionCard` box and the bare `ProgressView`
/// are gone; states use `HSLoading` / `HSError` / `HSEmpty` like every other
/// screen.
///
/// READ-ONLY by design: there are no mutating controls here. Creating,
/// editing, pausing or deleting a job is deliberately OUT OF SCOPE — a future
/// card if the operator wants it. A tap never fires a request.
///
/// The roster is offline-aware (`Offline.load`, cacheKey `/v1/cron/list`):
/// because the client caches this no-param GET under its plain path, a later
/// unreachable window can still show the last-known roster clearly marked
/// stale instead of a blank screen.
struct CronView: View {
    let client: HSCCClient?

    @State private var list = LoadState<CronListResponse>.idle

    var body: some View {
        Group {
            if let client {
                switch list {
                case .idle, .loading:
                    HSLoading("Loading…")
                case .failed(let message):
                    HSError("Couldn't load the roster", message: message) {
                        Task { await load(client) }
                    }
                case .stale(let state, let ageMessage):
                    rosterList(state, staleMessage: ageMessage, client: client)
                case .loaded(let state):
                    rosterList(state, staleMessage: nil, client: client)
                }
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Scheduled Jobs")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { if let client { await load(client) } }
        .task {
            if let client, list.value == nil, !list.isLoading {
                await load(client)
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "calendar.badge.clock", verb: "to see the scheduled jobs")
    }

    // MARK: - Load

    private func load(_ client: HSCCClient) async {
        list = await Offline.load(list,
                                  cacheKey: EndpointPath.cronList,
                                  client: client) {
            try await client.cronList()
        }
    }

    // MARK: - List (the shared recipe)

    /// One plain `List` for the roster: the recipe's dot · title · caption
    /// rows, `HSEmpty` for the no-jobs case, `StaleBanner` riding as a
    /// section. The old `HSSectionCard` box (the app's second row dialect)
    /// is gone — the nav title is the header, like every other screen.
    private func rosterList(_ state: CronListResponse,
                            staleMessage: String?,
                            client: HSCCClient) -> some View {
        let jobs = state.jobs ?? []
        // Active first, then paused; alphabetical inside each group.
        let sorted = jobs.sorted { a, b in
            if a.isActive != b.isActive { return a.isActive && !b.isActive }
            return (a.name ?? a.id) < (b.name ?? b.id)
        }
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
                Section {
                    Text(state.speak)
                        .font(.footnote.italic())
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
            }
            if sorted.isEmpty {
                Section {
                    HSEmpty("Idle",
                            message: "No scheduled jobs.",
                            systemImage: "calendar.badge.clock")
                }
            } else {
                Section("Scheduled jobs") {
                    ForEach(sorted) { job in
                        jobRow(job)
                    }
                }
            }
        }
        .listStyle(.plain)
    }

    @ViewBuilder
    private func jobRow(_ job: CronJob) -> some View {
        HStack(alignment: .top, spacing: Theme.Spacing.md.rawValue) {
            HSStatusDot(statusColor(job))
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                // Job name; fall back to the id for an unnamed job.
                Text(job.name ?? job.id)
                    .font(.headline)
                    .multilineTextAlignment(.leading)
                HSMetaLine([
                    job.schedule_display,
                    nextRunText(job),
                ])
                lastRunLine(job)
                if let err = job.last_error, !err.isEmpty {
                    Label("Last run failed: \(err)", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.bad)
                }
            }
            Spacer(minLength: 0)
            HSStatusChip(job.isActive ? "active" : "paused",
                         systemImage: job.isActive ? "checkmark" : "pause",
                         color: job.isActive ? Theme.Semantic.ok : Theme.Semantic.neutral)
        }
        // One VoiceOver element per job (spec §9): the rotor walks jobs, not
        // fragments of them.
        .accessibilityElement(children: .combine)
    }

    /// The status dot colour: active → ok, paused → neutral, and a failed last
    /// run elevated to `warn` so an errored but enabled job may still be
    /// firing on a bad note. Falls back to neutral for unknown state.
    private func statusColor(_ job: CronJob) -> Color {
        if job.last_status == "error" {
            return Theme.Semantic.warn
        }
        if job.isActive { return Theme.Semantic.ok }
        return Theme.Semantic.neutral
    }

    /// "next in 2h" / "next <date>" from `next_run_at` — tolerant parse,
    /// falls back to the raw ISO string (never blanks).
    private func nextRunText(_ job: CronJob) -> String? {
        guard let ts = job.next_run_at, !ts.isEmpty else { return nil }
        return "next \(friendlyTimestamp(ts))"
    }

    /// "last ran <t>" from `last_run_at` + the outcome when it wasn't ok.
    private func lastRunLine(_ job: CronJob) -> some View {
        var parts: [String] = []
        if let ts = job.last_run_at, !ts.isEmpty {
            parts.append("last \(friendlyTimestamp(ts))")
        }
        if let status = job.last_status {
            parts.append("result \(status)")
        }
        if parts.isEmpty { return AnyView(EmptyView()) }
        return AnyView(
            HSMetaLine(parts)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        )
    }

    /// Parse an ISO-8601 timestamp tolerantly (fractional seconds then plain)
    /// and render "MMM d HH:mm"; fall back to the raw string, never a blank.
    private func friendlyTimestamp(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        let out = DateFormatter()
        out.dateFormat = "MMM d HH:mm"
        return out.string(from: date)
    }
}
