import SwiftUI

/// Fleet view (B2): health, stats, throughput, streams, and autoscale reads
/// from the HSCC API over Tailscale.
///
/// Reached from the Cluster tab via the "Fleet" link. Each read is its own
/// section with its own `LoadState`, so one degraded endpoint never blanks the
/// rest of the screen. Errors surface `HSCCError.localizedDescription`, never a
/// raw dump. Every section shows its `speak` one-liner (B5 reuses it for voice).
/// Every section routes through `Offline.load`, so an unreachable cluster
/// surfaces last-known data (clearly marked stale) instead of a hard "failed" —
/// a transient blip must not make the whole fleet look idle or down.
struct FleetView: View {
    let client: HSCCClient

    @State private var health = LoadState<HealthResponse>.idle
    @State private var stats = LoadState<FleetStatsResponse>.idle
    @State private var throughput = LoadState<FleetThroughputResponse>.idle
    @State private var streams = LoadState<FleetStreamsResponse>.idle
    @State private var autoscale = LoadState<AutoscaleResponse>.idle

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                healthSection
                throughputSection
                statsSection
                streamsSection
                autoscaleSection
            }
            .padding()
        }
        .navigationTitle("Fleet")
        .refreshable { await loadAll() }
        .task {
            if health.value == nil, !health.isLoading {
                await loadAll()
            }
        }
    }

    // MARK: - Load

    private func loadAll() async {
        async let h: Void = loadHealth()
        async let s: Void = loadStats()
        async let t: Void = loadThroughput()
        async let st: Void = loadStreams()
        async let a: Void = loadAutoscale()
        _ = await (h, s, t, st, a)
    }

    private func loadHealth() async {
        if health.value == nil { health = .loading }
        health = await Offline.load(health,
                                    cacheKey: "/v1/health",
                                    client: client) {
            try await client.health()
        }
    }

    private func loadStats() async {
        if stats.value == nil { stats = .loading }
        stats = await Offline.load(stats,
                                   cacheKey: "/v1/fleet/stats",
                                   client: client) {
            try await client.fleetStats()
        }
    }

    private func loadThroughput() async {
        if throughput.value == nil { throughput = .loading }
        throughput = await Offline.load(throughput,
                                        cacheKey: "/v1/fleet/throughput",
                                        client: client) {
            try await client.fleetThroughput()
        }
    }

    private func loadStreams() async {
        if streams.value == nil { streams = .loading }
        streams = await Offline.load(streams,
                                     cacheKey: "/v1/fleet/streams",
                                     client: client) {
            try await client.fleetStreams()
        }
    }

    private func loadAutoscale() async {
        if autoscale.value == nil { autoscale = .loading }
        autoscale = await Offline.load(autoscale,
                                       cacheKey: "/v1/autoscale",
                                       client: client) {
            try await client.autoscale()
        }
    }

    // MARK: - Health (5-check verify)

    @ViewBuilder
    private var healthSection: some View {
        HSSectionCard(title: "Health", systemImage: "stethoscope") {
            switch health {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadHealth() }
                }
                healthBody(state)
            case .loaded(let state):
                healthBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func healthBody(_ state: HealthResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(state.speak, systemImage: state.ok ? "checkmark.seal.fill" : "xmark.seal.fill")
                .font(.subheadline)
                .foregroundColor(state.ok ? Theme.Semantic.ok : Theme.Semantic.bad)
            if state.checks.isEmpty {
                emptyLabel("No health checks reported.")
            } else {
                ForEach(state.checks) { check in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: HealthCheckIndicator.icon(check.ok))
                            .foregroundColor(HealthCheckIndicator.tint(check.ok))
                        VStack(alignment: .leading, spacing: 2) {
                            Text(check.name)
                                .font(.body)
                            if let detail = check.detail, !detail.isEmpty {
                                Text(detail)
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    // MARK: - Throughput

    @ViewBuilder
    private var throughputSection: some View {
        HSSectionCard(title: "Throughput", systemImage: "speedometer") {
            switch throughput {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadThroughput() }
                }
                throughputBody(state)
            case .loaded(let state):
                throughputBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func throughputBody(_ state: FleetThroughputResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)

            if let fleet = state.fleet {
                HStack(spacing: 10) {
                    statBadge(value: "\(fleet.nodes_ok ?? 0)/\(fleet.nodes_total ?? 0)",
                              label: "nodes ok",
                              color: (fleet.nodes_ok ?? 0) >= (fleet.nodes_total ?? 1) ? Theme.Semantic.ok : Theme.Semantic.warn)
                    statBadge(value: fmt(fleet.prompt_tokens), label: "prompt",
                              color: Theme.Semantic.onSurfaceMuted)
                    statBadge(value: fmt(fleet.generation_tokens), label: "generation",
                              color: Theme.Semantic.onSurfaceMuted)
                }
                HStack(spacing: 10) {
                    statBadge(value: fmt(fleet.running), label: "running",
                              color: Theme.Semantic.onSurface)
                    statBadge(value: fmt(fleet.waiting), label: "waiting",
                              color: Theme.Semantic.warn)
                }
            } else {
                emptyLabel("No throughput data.")
            }
        }
    }

    // MARK: - Stats

    @ViewBuilder
    private var statsSection: some View {
        HSSectionCard(title: "Stats", systemImage: "chart.bar") {
            switch stats {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadStats() }
                }
                statsBody(state)
            case .loaded(let state):
                statsBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func statsBody(_ state: FleetStatsResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)

            if let completions = state.completions {
                statBadge(value: "\(completions.total)", label: "work items",
                          color: Theme.Semantic.onSurface)

                if let byProfile = completions.by_profile, !byProfile.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("By profile").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        ForEach(byProfile.sorted { $0.value > $1.value }, id: \.key) { key, value in
                            row(key, value: "\(value)")
                        }
                    }
                }
            } else {
                emptyLabel("No stats reported.")
            }
        }
    }

    // MARK: - Streams

    @ViewBuilder
    private var streamsSection: some View {
        HSSectionCard(title: "Streams", systemImage: "point.3.connected.trianglepath.dotted") {
            switch streams {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadStreams() }
                }
                streamsBody(state)
            case .loaded(let state):
                streamsBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func streamsBody(_ state: FleetStreamsResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if state.streams.isEmpty {
                emptyLabel("No daemon streams reported.")
            } else {
                let sorted = state.streams.sorted { $0.key < $1.key }
                ForEach(sorted, id: \.key) { name, stream in
                    HStack(spacing: 8) {
                        Image(systemName: stream.ok == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundColor(stream.ok == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(name)
                                .font(.body)
                            if let message = stream.message, !message.isEmpty {
                                Text(message)
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                        Spacer()
                        if let ts = stream.timestamp {
                            Text(shortTimestamp(ts))
                                .font(.caption2)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }

    // MARK: - Autoscale

    @ViewBuilder
    private var autoscaleSection: some View {
        HSSectionCard(title: "Autoscale", systemImage: "arrow.up.arrow.down.circle") {
            switch autoscale {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadAutoscale() }
                }
                autoscaleBody(state)
            case .loaded(let state):
                autoscaleBody(state)
            default:
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func autoscaleBody(_ state: AutoscaleResponse) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
            if let reason = state.reason, !reason.isEmpty {
                Text(reason)
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    // MARK: - Shared building blocks


    private func statBadge(value: String, label: String, color: Color) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.title3.bold())
                .foregroundColor(color)
            Text(label)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }

    private func row(_ title: String, value: String) -> some View {
        HStack {
            Text(title)
                .font(.body)
            Spacer()
            Text(value)
                .font(.body.monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private func errorLabel(_ message: String) -> some View { HSErrorLabel(message: message) }
    private func emptyLabel(_ text: String) -> some View { HSEmptyLabel(message: text) }

    // MARK: - Formatting

    private func fmt(_ value: Double?) -> String {
        guard let value else { return "—" }
        if value == value.rounded() {
            return String(Int(value))
        }
        return String(format: "%.1f", value)
    }

    private func shortTimestamp(_ iso: String) -> String {
        // "2026-08-20T19:00:00+00:00" → "19:00".
        guard iso.count >= 16 else { return iso }
        let start = iso.index(iso.startIndex, offsetBy: 11)
        let end = iso.index(iso.startIndex, offsetBy: 16)
        return String(iso[start..<end])
    }
}
