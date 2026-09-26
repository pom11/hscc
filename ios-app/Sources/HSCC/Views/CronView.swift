import SwiftUI

/// Scheduled-job roster (Phase 3) — the read-only cron view.
///
/// Consumes `GET /v1/cron/list` (routes_cron.py) and shows the operator the
/// WHOLE scheduled-job population — active and paused — each with its
/// schedule, next/last run, and last outcome. Backed by
/// `hscc_daemon.autodown.list_all_cron_jobs`, so the roster reflects the same
/// on-disk source of truth the daemon itself uses.
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
        ScrollView {
            if let client {
                VStack(alignment: .leading, spacing: 16) {
                    rosterSection(client)
                }
                .padding()
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Scheduled Jobs")
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

    // MARK: - Roster

    @ViewBuilder
    private func rosterSection(_ client: HSCCClient) -> some View {
        HSSectionCard(title: "Scheduled jobs", systemImage: "calendar.badge.clock") {
            switch list {
            case .loading:
                ProgressView()
            case .failed(let message):
                HSErrorLabel(message: message, retry: { Task { await load(client) } })
                    .foregroundColor(Theme.Semantic.bad)
            case .stale(let state, let ageMessage):
                VStack(alignment: .leading, spacing: 10) {
                    StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                        Task { await load(client) }
                    }
                    rosterBody(state)
                }
            case .loaded(let state):
                rosterBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func rosterBody(_ state: CronListResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)

            let jobs = state.jobs ?? []
            if jobs.isEmpty {
                HSEmptyLabel(message: "No scheduled jobs.")
            } else {
                // Active first, then paused — each a job row.
                let sorted = jobs.sorted { a, b in
                    if a.isActive != b.isActive { return a.isActive && !b.isActive }
                    return (a.name ?? a.id) < (b.name ?? b.id)
                }
                ForEach(sorted) { job in
                    jobRow(job)
                }
            }
        }
    }

    @ViewBuilder
    private func jobRow(_ job: CronJob) -> some View {
        HStack(alignment: .top, spacing: 10) {
            HSStatusDot(statusColor(job))
            VStack(alignment: .leading, spacing: 4) {
                // Job name; fall back to the id for an unnamed job.
                HStack(spacing: 6) {
                    Text(job.name ?? job.id)
                        .font(.body.weight(.medium))
                    if job.isActive {
                        HSStatusChip("active", systemImage: "checkmark", color: Theme.Semantic.ok)
                    } else {
                        HSStatusChip("paused", systemImage: "pause", color: Theme.Semantic.neutral)
                    }
                }
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
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
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
