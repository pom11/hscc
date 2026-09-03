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
    // The per-node monitor is LAZY: it is intentionally NOT part of loadAll()
    // and stays idle until the operator taps "Load per-node monitor". The
    // /v1/cluster/monitor route costs ~3.02s (SSH subprocess to every node) and
    // must never gate the screen's initial render or pull-to-refresh.
    @State private var monitor = LoadState<ClusterMonitorResponse>.idle

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                healthSection
                throughputSection
                statsSection
                streamsSection
                autoscaleSection
                monitorSection
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

    private func loadMonitor() async {
        if monitor.value == nil { monitor = .loading }
        monitor = await Offline.load(monitor,
                                     cacheKey: "/v1/cluster/monitor",
                                     client: client) {
            try await client.clusterMonitor()
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

                if let byDay = completions.by_day, !byDay.isEmpty {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("By day").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                        let maxValue = byDay.values.max() ?? 1
                        ForEach(byDay.keys.sorted(), id: \.self) { date in
                            let value = byDay[date] ?? 0
                            HStack(spacing: Theme.Spacing.sm.rawValue) {
                                Text(shortDay(date))
                                    .font(.caption2)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                    .frame(width: 44, alignment: .leading)
                                RoundedRectangle(cornerRadius: 4, style: .continuous)
                                    .fill(Theme.Semantic.ok)
                                    .frame(width: CGFloat(value) / CGFloat(maxValue) * 140, height: 8)
                                Text("\(value)")
                                    .font(.caption.monospacedDigit())
                                    .foregroundColor(Theme.Semantic.onSurface)
                            }
                        }
                    }
                }
            } else {
                emptyLabel("No stats reported.")
            }

            if let activity = state.activity,
               !(activity.top_tools?.isEmpty ?? true) ||
               !(activity.tool_calls_by_profile?.isEmpty ?? true) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Activity").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                    if let tools = activity.top_tools, !tools.isEmpty {
                        ForEach(tools.compactMap(Self.parseToolPair), id: \.0) { name, count in
                            row(name, value: "\(count)")
                        }
                    }
                    if let byProfile = activity.tool_calls_by_profile, !byProfile.isEmpty {
                        ForEach(byProfile.sorted { $0.value > $1.value }, id: \.key) { key, value in
                            row(key, value: "\(value)")
                        }
                    }
                }
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

    // MARK: - Per-node monitor (lazy, on-demand)

    @ViewBuilder
    private var monitorSection: some View {
        HSSectionCard(title: "Per-node monitor", systemImage: "gauge.medium") {
            switch monitor {
            case .idle:
                // LAZY via Offline.load FIFO semantics: nothing is fetched until
                // the operator opts in. Button opens with a note about the cost.
                Button {
                    Task { await loadMonitor() }
                } label: {
                    Label("Load per-node monitor", systemImage: "arrow.down.circle")
                        .font(.subheadline)
                }
                Text("Samples each node over SSH — takes a few seconds.")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            case .loading:
                ProgressView("Sampling every node…")
            case .failed(let message):
                VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
                    errorLabel(message)
                    Button("Try again") {
                        Task { await loadMonitor() }
                    }
                    .font(.subheadline)
                }
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadMonitor() }
                }
                monitorBody(state)
            case .loaded(let state):
                monitorBody(state)
            }
        }
    }

    @ViewBuilder
    private func monitorBody(_ state: ClusterMonitorResponse) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.md.rawValue) {
            Text(state.speak)
                .font(.subheadline)
                .italic()
                .foregroundColor(Theme.Semantic.onSurfaceMuted)

            if state.hosts.isEmpty {
                emptyLabel("No nodes reported.")
            } else {
                ForEach(state.hosts) { host in
                    monitorNodeCard(host)
                }
            }
        }
    }

    /// One node's saturation + heat readout. Makes load and temperature visible
    /// at a glance via colored bars: GPU/CPU/RAM utilization and VRAM usage are
    /// shown as proportional bars tinted by `saturationColor`, temperatures by
    /// `heatColor`. A node that failed to be sampled shows its error instead.
    @ViewBuilder
    private func monitorNodeCard(_ host: ClusterMonitorHost) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            // Header: hostname (mono) + address + slots.
            let displayName = host.sample.flatMap(\.hostname).flatMap {
                $0.isEmpty ? nil : $0
            } ?? host.host
            HStack(alignment: .firstTextBaseline, spacing: Theme.Spacing.xs.rawValue) {
                Text(displayName)
                    .font(.headline)
                    .font(.hsccMono(16, weight: .semibold))
                    .foregroundColor(Theme.Semantic.onSurface)
                    .lineLimit(1)
                Spacer(minLength: 0)
                HSStatusDot(host.sample != nil ? Theme.Semantic.ok : Theme.Semantic.bad)
            }

            if let error = host.error, !error.isEmpty {
                HSErrorLabel(message: error)
            } else if let sample = host.sample {
                metricsBlock(sample)
                // Slots line under the metrics.
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    HSMetaLine([
                        host.host,
                        slotsText(host),
                        sample.uptimeText.map { "up \($0)" },
                    ])
                    Spacer(minLength: 0)
                }
            } else {
                HSEmptyLabel(message: "No sample for this node.")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(Theme.Spacing.sm.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }

    /// The GPU + RAM/CPU readouts for one sampled node.
    @ViewBuilder
    private func metricsBlock(_ s: NodeSample) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            // GPU utilization + name.
            if let gpuName = s.gpu_name, !gpuName.isEmpty {
                Text(gpuName)
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .lineLimit(1)
            }
            if let util = s.gpuUtilPct {
                monitorBar(label: "GPU",
                           percent: util,
                           text: "\(fmt(util))%",
                           color: saturationColor(util))
            }
            // VRAM usage (used/total + percent bar).
            if let total = s.gpuMemTotalMB, total > 0 {
                let usedPct = s.gpuMemUsedPct ?? (total > 0 ? (s.gpuMemUsedMB ?? 0) / total * 100 : 0)
                monitorBar(label: "VRAM",
                           percent: usedPct,
                           text: "\(fmtMB(s.gpuMemUsedMB)) / \(fmtMB(total))",
                           color: saturationColor(usedPct))
            }
            // GPU temperature — the heat signal that has throttled a node before.
            if let temp = s.gpuTempC {
                monitorBar(label: "GPU temp",
                           percent: temp / 100,
                           text: "\(fmt(temp))°C",
                           color: heatColor(temp))
            }
            // RAM utilization.
            if let mem = s.memUsedPct {
                monitorBar(label: "RAM",
                           percent: mem,
                           text: "\(fmt(mem))%",
                           color: saturationColor(mem))
            } else if let total = s.memTotalMB, let used = s.memUsedMB, total > 0 {
                let pct = used / total * 100
                monitorBar(label: "RAM",
                           percent: pct,
                           text: "\(fmtMB(used)) / \(fmtMB(total))",
                           color: saturationColor(pct))
            }
            // CPU utilization + temperature.
            if let cpu = s.cpuUsagePct {
                monitorBar(label: "CPU",
                           percent: cpu,
                           text: "\(fmt(cpu))%",
                           color: saturationColor(cpu))
            }
            if let cpuTemp = s.cpuTempC {
                monitorBar(label: "CPU temp",
                           percent: cpuTemp / 100,
                           text: "\(fmt(cpuTemp))°C",
                           color: heatColor(cpuTemp))
            }
        }
    }

    /// A labeled horizontal saturation bar: `label` + a proportional fill + the
    /// value text on the right. `percent` is 0...100; clamped into the bar.
    private func monitorBar(label: String, percent: Double, text: String, color: Color) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            Text(label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                .frame(width: 52, alignment: .leading)
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule()
                        .fill(Theme.Semantic.surfaceRaised)
                    Capsule()
                        .fill(color)
                        .frame(width: geo.size.width * CGFloat(min(max(percent, 0), 100)) / 100)
                }
            }
            .frame(height: 8)
            Text(text)
                .font(.caption.monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurface)
                .frame(width: 86, alignment: .trailing)
        }
    }

    /// Saturation ramp for utilization/VRAM percents (higher = busier).
    /// Ok < 70, warn 70...90, bad > 90. (RAM on these DGX boxes legitimately
    /// sits ~96% used with buff/cache counted as used, so a full RAM bar reads
    /// as amber — busy but normal, NOT a fault. GPU/VRAM are the true signals.)
    private func saturationColor(_ percent: Double) -> Color {
        if percent > 90 { return Theme.Semantic.bad }
        if percent >= 70 { return Theme.Semantic.warn }
        return Theme.Semantic.ok
    }

    /// Heat ramp for temperatures in °C. Designed around a DGX Spark's thermal
    /// envelope — the node that throttled runs hottest, so >85 is the danger
    /// band, 70...85 busy but fine, <70 cool.
    private func heatColor(_ celsius: Double) -> Color {
        if celsius > 85 { return Theme.Semantic.bad }
        if celsius >= 70 { return Theme.Semantic.warn }
        return Theme.Semantic.ok
    }

    /// "1/1" or "1 used · 1 free" — slots for a node.
    private func slotsText(_ host: ClusterMonitorHost) -> String? {
        let used = host.used_slots, free = host.free_slots
        if let used, let free {
            let total = used + free
            return total > 0 ? "\(used)/\(total) slots" : nil
        }
        return nil
    }

    /// Format a MB integer ("119681" → "117G") into a readable size.
    private func fmtMB(_ mb: Double?) -> String {
        guard let mb else { return "—" }
        let gb = mb / 1024
        return "\(fmt(gb))G"
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

    /// "2026-08-27" → "08-27" (drop the year; by_day keys are full ISO dates).
    private func shortDay(_ iso: String) -> String {
        guard iso.count >= 10 else { return iso }
        let start = iso.index(iso.startIndex, offsetBy: 5)
        let end = iso.index(iso.startIndex, offsetBy: 10)
        return String(iso[start..<end])
    }

    /// Parse one `top_tools` entry — `["test_tool", 134]` → ("test_tool", 134).
    /// Returns nil for malformed pairs so they're safely skipped.
    private static func parseToolPair(_ pair: [JSONValue]) -> (String, Int)? {
        guard pair.count >= 2,
              let name = pair[0].string,
              case .int(let count) = pair[1] else { return nil }
        return (name, count)
    }
}
