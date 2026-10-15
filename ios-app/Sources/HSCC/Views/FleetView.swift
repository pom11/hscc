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
///
/// LIST RECIPE (spec §5, Phase 2 C3): one plain `List` with one `Section` per
/// feed — Health · Throughput · Stats · Streams · Autoscale · Models, then the
/// two sections the retired `FleetControlView` folded into here ("Applied
/// template" · "Control", work-log item 5 / §8 row "FleetView · FleetControlView
/// | drawer · Fleet (+ Control section)"). The `HSSectionCard` boxes are gone;
/// every feed keeps its own `LoadState`, and a feed that is loading/failed shows
/// an inline row rather than a full-pane spinner so the feeds that DID load stay
/// visible. The check rows render through the shared `HealthCheckList` (the §5
/// byte-twin collapse with Ops); only `statBadge`/`statGrid` remain bespoke,
/// because they are value-over-label stat tiles rather than status chips.
///
/// THE MODELS SECTION (ruling `t_1897550c`, spec §5 as amended by PR #29):
/// `ModelsView` never existed, so this is NOT a merge — it is the new, additive,
/// READ-ONLY serving feed the mockup frames 7/8 always drew (`Models · 3
/// serving`). One row per workload of `GET /v1/cluster/status` — the same
/// `ClusterStatusResponse` / `ClusterWorkload` decode "Nodes & serving" reads —
/// so no new endpoint and no new model types. Read-only is the point: Stop and
/// Restart stay on "Nodes & serving" (which keeps its per-unit buttons and its
/// own unit grouping). Fleet gains a second, non-mutating view of the same feed;
/// the ruling deletes and moves nothing.
///
/// WHAT THE ROW CAN HONESTLY SAY: the mockup's caption is
/// "spark-01, spark-02 · 2 streams", but `/v1/cluster/status` carries neither a
/// node per workload nor a per-model stream count — the unit→node edge lives in
/// `/v1/cluster/hosts` (`ServingPlacement`), and stream counts are not in this
/// payload at all. The card rules out a second endpoint, so the caption shows
/// what the feed DOES carry: the same `tp`/`pp` + container id "Nodes & serving"
/// shows. Same reason the trailing chip says "no container" rather than the
/// mockup's "waking": this endpoint can prove a workload has a live container or
/// that it does not — it cannot prove which way it is heading, and guessing a
/// direction is the colour-lie class §9 bans.
struct FleetView: View {
    let client: HSCCClient?

    @State private var health = LoadState<HealthResponse>.idle
    @State private var stats = LoadState<FleetStatsResponse>.idle
    @State private var throughput = LoadState<FleetThroughputResponse>.idle
    @State private var streams = LoadState<FleetStreamsResponse>.idle
    @State private var autoscale = LoadState<AutoscaleResponse>.idle
    /// The Models serving feed (ruling `t_1897550c`). Its own `LoadState`, so a
    /// dead `/v1/cluster/status` cannot blank the five feeds that answered — and
    /// the reverse. Same cache key "Nodes & serving" uses, so the two screens
    /// agree about what the last-known fleet was.
    @State private var serving = LoadState<ClusterStatusResponse>.idle
    /// The applied template — folded in from `FleetControlView` (work-log item 5)
    /// with its `Offline.load` + `AppliedTemplateStatus` render intact.
    @State private var templateStatus = LoadState<TemplateStatusResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    healthSection
                    throughputSection
                    statsSection
                    streamsSection
                    autoscaleSection
                    modelsSection
                    appliedTemplateSection
                    controlSection(client: client)
                }
                .listStyle(.plain)
                .refreshable { await loadAll() }
                .task {
                    if health.value == nil, !health.isLoading {
                        await loadAll()
                    }
                }
            } else {
                // The optional-client convention every other screen uses. This
                // is why FleetView's client became optional in this card (it was
                // the last non-optional one): a screen that now carries the
                // fleet-wide up/down mutations must NOT render them against the
                // old `FleetViewPlaceholder` — a gate that says "set your cluster
                // in Settings" beats a Bring-Fleet-Up button that can only fail.
                HSConnectGate(systemImage: "chart.bar", verb: "to see and control the fleet")
            }
        }
        .navigationTitle("Fleet")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - Load

    /// Every feed the screen owns, concurrently. The two reads folded in from
    /// `FleetControlView` ride the same gesture: one pull refreshes the whole
    /// screen, which is the point of the "Control section of Fleet" merge.
    private func loadAll() async {
        async let h: Void = loadHealth()
        async let s: Void = loadStats()
        async let t: Void = loadThroughput()
        async let st: Void = loadStreams()
        async let a: Void = loadAutoscale()
        async let m: Void = loadModels()
        async let ts: Void = loadTemplateStatus()
        _ = await (h, s, t, st, a, m, ts)
    }

    private func loadHealth() async {
        guard let client else { return }
        if health.value == nil { health = .loading }
        health = await Offline.load(health,
                                    cacheKey: "/v1/health",
                                    client: client) {
            try await client.health()
        }
    }

    private func loadStats() async {
        guard let client else { return }
        if stats.value == nil { stats = .loading }
        stats = await Offline.load(stats,
                                   cacheKey: "/v1/fleet/stats",
                                   client: client) {
            try await client.fleetStats()
        }
    }

    private func loadThroughput() async {
        guard let client else { return }
        if throughput.value == nil { throughput = .loading }
        throughput = await Offline.load(throughput,
                                        cacheKey: "/v1/fleet/throughput",
                                        client: client) {
            try await client.fleetThroughput()
        }
    }

    private func loadStreams() async {
        guard let client else { return }
        if streams.value == nil { streams = .loading }
        streams = await Offline.load(streams,
                                     cacheKey: "/v1/fleet/streams",
                                     client: client) {
            try await client.fleetStreams()
        }
    }

    private func loadAutoscale() async {
        guard let client else { return }
        if autoscale.value == nil { autoscale = .loading }
        autoscale = await Offline.load(autoscale,
                                       cacheKey: "/v1/autoscale",
                                       client: client) {
            try await client.autoscale()
        }
    }

    /// The serving feed. `Offline.load` like every other section here: a fleet
    /// that is briefly unreachable still has a last-known model list worth
    /// showing, marked stale.
    private func loadModels() async {
        guard let client else { return }
        if serving.value == nil { serving = .loading }
        serving = await Offline.load(serving,
                                     cacheKey: EndpointPath.clusterStatus,
                                     client: client) {
            try await client.clusterStatus()
        }
    }

    /// Folded in from `FleetControlView` verbatim (same cache key, same
    /// offline-aware helper) — the applied-status read now belongs to Fleet.
    private func loadTemplateStatus() async {
        guard let client else { return }
        templateStatus = await Offline.load(templateStatus,
                                            cacheKey: EndpointPath.templateStatus,
                                            client: client) {
            try await client.templateStatus()
        }
    }

    // MARK: - Health (5-check verify)

    /// Each of the five reads is a `Section` of the ONE List (spec §5: one
    /// section per feed — Health · Throughput · Stats · Streams · Autoscale
    /// · Models). The `HSSectionCard` boxes are gone; List supplies the
    /// separators and the caps section headers. Each keeps its own `LoadState`,
    /// so one degraded endpoint still cannot blank the rest of the screen.
    @ViewBuilder
    private var healthSection: some View {
        Section("Health") {
            switch health {
            case .idle, .loading:
                loadingRow("Loading health…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadHealth() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadHealth() }
                }
                healthBody(state)
            case .loaded(let state):
                healthBody(state)
            }
        }
    }

    @ViewBuilder
    private func healthBody(_ state: HealthResponse) -> some View {
        Label(state.speak, systemImage: state.ok ? "checkmark.seal.fill" : "xmark.seal.fill")
            .font(.subheadline)
            .foregroundColor(state.ok ? Theme.Semantic.ok : Theme.Semantic.bad)
        // Spec §5: was a byte-twin of Ops' `verifyBody`; both now render
        // through the one HealthCheckList so the check rows cannot drift.
        HealthCheckList(state.checks, emptyMessage: "No health checks reported.")
    }

    // MARK: - Throughput

    @ViewBuilder
    private var throughputSection: some View {
        Section("Throughput") {
            switch throughput {
            case .idle, .loading:
                loadingRow("Loading throughput…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadThroughput() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadThroughput() }
                }
                throughputBody(state)
            case .loaded(let state):
                throughputBody(state)
            }
        }
    }

    @ViewBuilder
    private func throughputBody(_ state: FleetThroughputResponse) -> some View {
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)

        if let fleet = state.fleet {
            // The stat tiles stay tiles (value over label), not status chips:
            // HSStatusChip has no value/label hierarchy to give them. They are
            // laid out by `statGrid` (three-up) so a List row can give them the
            // full width — an HStack of `maxWidth: .infinity` tiles inside a
            // row sizes to content and starts a horizontal scroll.
            statGrid([
                ("\(fleet.nodes_ok ?? 0)/\(fleet.nodes_total ?? 0)", "nodes ok",
                 (fleet.nodes_ok ?? 0) >= (fleet.nodes_total ?? 1) ? Theme.Semantic.ok : Theme.Semantic.warn),
                (fmt(fleet.prompt_tokens), "prompt", Theme.Semantic.onSurfaceMuted),
                (fmt(fleet.generation_tokens), "generation", Theme.Semantic.onSurfaceMuted),
                (fmt(fleet.running), "running", Theme.Semantic.onSurface),
                (fmt(fleet.waiting), "waiting", Theme.Semantic.warn),
            ])
        } else {
            HSEmpty("No throughput",
                    message: "No throughput data.",
                    systemImage: "speedometer")
        }
    }

    // MARK: - Stats

    @ViewBuilder
    private var statsSection: some View {
        Section("Stats") {
            switch stats {
            case .idle, .loading:
                loadingRow("Loading stats…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadStats() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadStats() }
                }
                statsBody(state)
            case .loaded(let state):
                statsBody(state)
            }
        }
    }

    @ViewBuilder
    private func statsBody(_ state: FleetStatsResponse) -> some View {
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)

        if let completions = state.completions {
            statGrid([(("\(completions.total)", "work items", Theme.Semantic.onSurface))])

            if let byProfile = completions.by_profile, !byProfile.isEmpty {
                Text("By profile")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                ForEach(byProfile.sorted { $0.value > $1.value }, id: \.key) { key, value in
                    row(key, value: "\(value)")
                }
            }

            if let byDay = completions.by_day, !byDay.isEmpty {
                Text("By day")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                let maxValue = byDay.values.max() ?? 1
                ForEach(byDay.keys.sorted(), id: \.self) { date in
                    let value = byDay[date] ?? 0
                    HStack(spacing: Theme.Spacing.sm.rawValue) {
                        Text(shortDay(date))
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            .frame(width: 44, alignment: .leading)
                        RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                            .fill(Theme.Semantic.ok)
                            .frame(width: CGFloat(value) / CGFloat(maxValue) * 140, height: 8)
                        Text("\(value)")
                            .font(.hsccMono(.caption, weight: .regular))
                            .foregroundColor(Theme.Semantic.onSurface)
                    }
                    .accessibilityLabel("\(date): \(value) work items")
                }
            }
        } else {
            HSEmpty("No stats",
                    message: "No stats reported.",
                    systemImage: "chart.bar")
        }

        if let activity = state.activity,
           !(activity.top_tools?.isEmpty ?? true) ||
           !(activity.tool_calls_by_profile?.isEmpty ?? true) {
            Text("Activity")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
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

    // MARK: - Streams

    @ViewBuilder
    private var streamsSection: some View {
        Section("Streams") {
            switch streams {
            case .idle, .loading:
                loadingRow("Loading streams…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadStreams() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadStreams() }
                }
                streamsBody(state)
            case .loaded(let state):
                streamsBody(state)
            }
        }
    }

    @ViewBuilder
    private func streamsBody(_ state: FleetStreamsResponse) -> some View {
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        if state.streams.isEmpty {
            HSEmpty("No streams",
                    message: "No daemon streams reported.",
                    systemImage: "point.3.connected.trianglepath.dotted")
        } else {
            let sorted = state.streams.sorted { $0.key < $1.key }
            ForEach(sorted, id: \.key) { name, stream in
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    Image(systemName: stream.ok == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundColor(stream.ok == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                    VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                        Text(name)
                            .font(.body)
                        if let message = stream.message, !message.isEmpty {
                            Text(message)
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    Spacer(minLength: 0)
                    if let ts = stream.timestamp {
                        Text(shortTimestamp(ts))
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }

    // MARK: - Autoscale

    @ViewBuilder
    private var autoscaleSection: some View {
        Section("Autoscale") {
            switch autoscale {
            case .idle, .loading:
                loadingRow("Loading autoscale…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadAutoscale() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadAutoscale() }
                }
                autoscaleBody(state)
            case .loaded(let state):
                autoscaleBody(state)
            }
        }
    }

    @ViewBuilder
    private func autoscaleBody(_ state: AutoscaleResponse) -> some View {
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        if let reason = state.reason, !reason.isEmpty {
            Text(reason)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    // MARK: - Models (the serving read — ruling t_1897550c)

    /// The read-only Models serving feed. New, additive, one `Section` of the
    /// one List — the ruling's "NOT a merge, must not delete or move anything".
    ///
    /// Same feed "Nodes & serving" reads (`GET /v1/cluster/status`,
    /// `ClusterStatusResponse`/`ClusterWorkload`, `client.clusterStatus()`), so
    /// no new endpoint and no new model types. The rows are deliberately not
    /// tappable and carry no buttons: the mutating surface for a single unit
    /// stays on "Nodes & serving" (t_65d791da owns that file).
    ///
    /// `speak` is the server-authored one-liner, so the count the section
    /// states and the count the feed reports can be checked against each other
    /// (same reason every other feed here prints it).
    @ViewBuilder
    private var modelsSection: some View {
        Section {
            switch serving {
            case .idle, .loading:
                loadingRow("Loading models…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadModels() } })
            case .stale(let state, let age):
                StaleBanner(age: age, reason: "Can't reach the cluster right now.") {
                    Task { await loadModels() }
                }
                modelsBody(state)
            case .loaded(let state):
                modelsBody(state)
            }
        } header: {
            // `Models · 3 serving` (mockup frames 7/8), derived from the rows
            // actually rendered — see `FleetServing.header`.
            if let count = serving.value?.workloads.count {
                Text(FleetServing.header(workloadCount: count))
            } else {
                Text("Models")
            }
        } footer: {
            Text("Read-only here: a workload's Stop / Restart live in “Nodes & serving”, where each unit is grouped under the node that runs it.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    @ViewBuilder
    private func modelsBody(_ state: ClusterStatusResponse) -> some View {
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        if state.workloads.isEmpty {
            HSEmpty("No models serving",
                    message: "Nothing is serving right now — a workload that is down does not appear in this feed. The Control section below brings the whole serving fleet up.",
                    systemImage: "cpu")
        } else {
            // `FleetServing.rows`, not `state.workloads` directly: the feed's
            // own `id` is the workload NAME, and the live feed repeats one
            // recipe name per container (3 replicas in the committed capture).
            // `rows(from:)` gives ForEach the unique ids that requires.
            ForEach(FleetServing.rows(from: state.workloads)) { row in
                modelRow(row)
            }
        }
    }

    /// One Models row, built from `FleetServing.Row` so the same strings drive
    /// the screen and the accessibility label (the label is asserted headlessly
    /// by `scripts/fleet_models_check.sh`).
    ///
    /// The whole row is ONE VoiceOver element, as the card requires: there are
    /// no buttons in this section, so nothing is lost by combining — unlike the
    /// serving screen, whose rows deliberately stay un-combined for their two
    /// confirm-gated buttons. The row is also `.accessibilityAddTraits(.isStaticText)`
    ///-free on purpose: a plain row is already announced as text; adding a trait
    /// would invite a tap affordance that does not exist.
    @ViewBuilder
    private func modelRow(_ row: FleetServing.Row) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(row.model)
                    .font(.headline)
                    .foregroundColor(Theme.Semantic.onSurface)
                    .textSelection(.enabled)
                Text(row.caption)
                    .font(.hsccMono(.caption))
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            Spacer(minLength: 0)
            HSStatusChip(row.chipText,
                         systemImage: row.state == .up ? "checkmark.circle.fill" : "circle.dashed",
                         color: row.state == .up ? Theme.Semantic.ok : Theme.Semantic.warn)
        }
        // §9: read-only row → combine into one element naming the model, what
        // the feed says about it, and its state.
        .accessibilityElement(children: .combine)
        .accessibilityLabel(row.accessibilityLabel)
    }

    // MARK: - Control (folded in from FleetControlView — work-log item 5, §8)

    /// The applied-template read, moved with `AppliedTemplateStatus` (the §5
    /// shared render) and its own offline-aware `Offline.load` intact. The
    /// retired screen kept its own header/refresh/load and only shared the rows;
    /// here the header is a `Section` header of this List, which is the §8 shape
    /// ("FleetView · FleetControlView | drawer · Fleet (+ Control section)").
    @ViewBuilder
    private var appliedTemplateSection: some View {
        Section("Applied template") {
            AppliedTemplateStatus(status: templateStatus) {
                Task { await loadTemplateStatus() }
            }
        }
    }

    /// Fleet-wide up/down, both confirm-gated `MutationButton`s with every
    /// prompt carried over VERBATIM from `FleetControlView` (a confirm dialog
    /// that has been reviewed and re-reviewed is not reworded as a side effect
    /// of a merge). Both reload the applied-template read exactly as the retired
    /// screen did, so the section below them stays honest after a mutation.
    ///
    /// `client` is passed in rather than unwrapped here: the call site is
    /// already inside `if let client`, and it keeps the "these buttons only
    /// exist when there IS a cluster" rule in one place.
    @ViewBuilder
    private func controlSection(client: HSCCClient) -> some View {
        Section {
            // Cluster Up — starts every serving unit. Confirm names it.
            MutationButton(
                title: "Bring Fleet Up",
                systemImage: "play.circle",
                prompt: "Bring the fleet up? This starts every serving unit in the cluster (orchestrator + workers).",
                run: {
                    let result = try await client.clusterUp()
                    await loadTemplateStatus()
                    await loadModels()
                    return result.message ?? "Fleet up issued."
                }
            )
            .accessibilityLabel("Bring serving fleet up")

            // Cluster Down — destructive. Confirmation NAMES what happens:
            // it stops ALL workloads fleet-wide.
            MutationButton(
                title: "Stop All Workloads",
                systemImage: "stop.circle",
                destructive: true,
                prompt: "Stop ALL workloads fleet-wide? This shuts down every serving unit across the entire cluster and interrupts any in-flight work.",
                run: {
                    let result = try await client.clusterDown()
                    await loadTemplateStatus()
                    await loadModels()
                    return result.message ?? "Fleet down issued."
                }
            )
            .accessibilityLabel("Stop all cluster workloads")
        } header: {
            Text("Control")
        } footer: {
            // Static copy, deliberately NOT conditioned on settings: this
            // section only renders inside `if let client`, so "there is a
            // cluster to act on" is already guaranteed by the gate itself — a
            // second, separately-computed claim about configuration could only
            // ever contradict the buttons above it (it would in the harness's
            // fixture mode, where the client exists but a keychain-seeded flag
            // does not). The original FleetControlView footer, scope-extended.
            Text("Bring the serving fleet up, or stop ALL workloads fleet-wide. Per-unit Stop / Restart stay in “Nodes & serving”.")
                .font(.footnote)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    // MARK: - Shared building blocks

    /// Inline loading line for a section that is still fetching. Inline (not
    /// the centred `HSLoading`) because the other four sections may already
    /// have content — one slow endpoint must not blank them.
    private func loadingRow(_ label: String) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            ProgressView()
            Text(label)
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private func statBadge(value: String, label: String, color: Color) -> some View {
        VStack(spacing: Theme.Spacing.xxs.rawValue) {
            Text(value)
                .font(.title3.bold())
                .foregroundColor(color)
            Text(label)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, Theme.Spacing.sm.rawValue)
        .background(
            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                .fill(Theme.Semantic.surfaceElevated)
        )
    }

    /// The stat tiles laid out three-up inside a List row. `GridRow`s are used
    /// rather than `HStack`s because a List row sizes to its content: a plain
    /// HStack of `maxWidth: .infinity` tiles would fight the row and clip.
    /// Tiles keep their value-over-label hierarchy (deliberately NOT
    /// `HSStatusChip` — a chip has no room for a value).
    private func statGrid(_ stats: [(value: String, label: String, color: Color)]) -> some View {
        let rows = stride(from: 0, to: stats.count, by: 3).map {
            Array(stats[$0..<min($0 + 3, stats.count)])
        }
        return Grid(alignment: .center,
                    horizontalSpacing: Theme.Spacing.md.rawValue,
                    verticalSpacing: Theme.Spacing.md.rawValue) {
            ForEach(rows.indices, id: \.self) { i in
                GridRow {
                    ForEach(rows[i].indices, id: \.self) { j in
                        statBadge(value: rows[i][j].value,
                                  label: rows[i][j].label,
                                  color: rows[i][j].color)
                            .gridColumnAlignment(.center)
                    }
                    // Pad a short final row so tile widths stay equal.
                    if rows[i].count < 3 {
                        ForEach(1...(3 - rows[i].count), id: \.self) { _ in
                            Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                        }
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
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

    private func errorLabel(_ message: String, retry: (() -> Void)? = nil) -> some View { HSErrorLabel(message: message, retry: retry) }

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
