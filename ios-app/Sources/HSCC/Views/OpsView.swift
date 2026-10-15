import SwiftUI

/// Ops / Health tab (C6) — the operational health surface.
///
/// Shows the full `hscc verify` per-check pass/fail, daemon status + every
/// health stream, trigger rules + last run, pending escalations, and profile
/// task counts. Each read is its own section with its own `LoadState`, so one
/// degraded endpoint never blanks the rest. Errors surface the real message.
/// Fleet-wide mutations live in Fleet's Control section (t_2a29a7ab retired
/// `FleetControlView` as a screen) and autodown controls in AutodownView; this
/// view hosts the operator's trigger/escalation actions — "run triggers now"
/// and "perform escalations" — both confirm-gated via `MutationButton`. A tap
/// never fires a request by itself.
///
/// LIST RECIPE (spec §5, Phase 2 C3): the five `HSSectionCard` boxes are now
/// five `Section`s of one plain `List`, each keeping its own `LoadState`. The
/// two operator actions move OUT of the scroll (spec §5: "today buried
/// mid-scroll") into a sticky bottom bar under `safeAreaInset` — still
/// confirm-gated, now always reachable.
struct OpsView: View {
    let client: HSCCClient?

    @State private var verify = LoadState<VerifyResponse>.idle
    @State private var daemon = LoadState<DaemonStatusResponse>.idle
    @State private var triggers = LoadState<TriggersResponse>.idle
    @State private var escalations = LoadState<EscalationsResponse>.idle
    @State private var profiles = LoadState<ProfilesResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    verifySection(client)
                    daemonSection
                    triggersSection(client: client)
                    escalateSection(client: client)
                    profilesSection
                }
                .listStyle(.plain)
                // Sticky operator actions (spec §5). They used to sit at the
                // bottom of two mid-scroll cards, so reaching them meant
                // finding them first; now they ride the safe area.
                .safeAreaInset(edge: .bottom) { actionBar(client: client) }
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Ops")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { if client != nil { await loadAll() } }
        .task {
            if client != nil, verify.value == nil, !verify.isLoading {
                await loadAll()
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "stethoscope", verb: "to see health")
    }

    // MARK: - Load

    private func loadAll() async {
        guard let client else { return }
        async let v: Void = loadVerify(client)
        async let d: Void = loadDaemon(client)
        async let t: Void = loadTriggers(client)
        async let e: Void = loadEscalations(client)
        async let p: Void = loadProfiles(client)
        _ = await (v, d, t, e, p)
    }

    private func loadVerify(_ client: HSCCClient) async {
        verify = await Offline.load(verify,
                                    cacheKey: EndpointPath.verify,
                                    client: client) {
            try await client.verify()
        }
    }

    private func loadDaemon(_ client: HSCCClient) async {
        daemon = .loading
        do { daemon = .loaded(try await client.daemonStatus()) }
        catch { daemon = .failed(errorMessage(for: error)) }
    }

    private func loadTriggers(_ client: HSCCClient) async {
        triggers = .loading
        do { triggers = .loaded(try await client.triggers()) }
        catch { triggers = .failed(errorMessage(for: error)) }
    }

    private func loadEscalations(_ client: HSCCClient) async {
        escalations = .loading
        do { escalations = .loaded(try await client.escalations()) }
        catch { escalations = .failed(errorMessage(for: error)) }
    }

    private func loadProfiles(_ client: HSCCClient) async {
        profiles = .loading
        do { profiles = .loaded(try await client.profiles()) }
        catch { profiles = .failed(errorMessage(for: error)) }
    }

    private func errorMessage(for error: Error) -> String {
        operatorErrorMessage(error)
    }

    // MARK: - Sticky operator actions (spec §5)

    /// The two confirm-gated operator actions, pinned to the bottom instead
    /// of buried at the end of a mid-scroll card. Each is still a
    /// `MutationButton`: one tap arms, the confirm names the consequence, and
    /// only then does the request fire. Escalations keeps its `destructive`
    /// role (it mutates tasks and notifies humans); Run Triggers is not
    /// destructive.
    private func actionBar(client: HSCCClient) -> some View {
        HStack(spacing: Theme.Spacing.md.rawValue) {
            MutationButton(
                title: "Run Triggers Now",
                systemImage: "bolt.circle",
                destructive: false,
                prompt: "Re-evaluate all trigger rules now? Enabled rules may fire actions (notify, auto-restart, block pipeline) immediately instead of waiting for the daemon's next cycle.",
                run: {
                    let result = try await client.triggersRun()
                    triggers = .loaded(result)
                    return result.speak.isEmpty ? "Trigger engine run." : result.speak
                }
            )
            MutationButton(
                title: "Perform Escalations",
                systemImage: "arrow.up.right.circle.fill",
                destructive: true,
                prompt: "Run pending escalations for real now? This reassigns repeatedly-failing tasks to the strong tier and notifies a human for each one — it is not a dry run.",
                run: {
                    let result = try await client.escalateRun()
                    escalations = .loaded(result)
                    return result.speak.isEmpty ? "Escalations run." : result.speak
                }
            )
        }
        .padding(Theme.Spacing.md.rawValue)
        .background(Theme.Semantic.surface)
    }

    // MARK: - Verify (per-check pass/fail)

    @ViewBuilder
    private func verifySection(_ client: HSCCClient) -> some View {
        Section("Verify") {
            switch verify {
            case .idle, .loading:
                loadingRow("Loading checks…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadVerify(client) } })
            case .stale(let state, let ageMessage):
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadVerify(client) }
                }
                verifyBody(state)
            case .loaded(let state):
                verifyBody(state)
            }
        }
    }

    @ViewBuilder
    private func verifyBody(_ state: VerifyResponse) -> some View {
        Label(state.speak, systemImage: state.ok ? "checkmark.seal.fill" : "xmark.seal.fill")
            .font(.subheadline)
            .foregroundColor(state.ok ? Theme.Semantic.ok : Theme.Semantic.bad)
        // Spec §5: the byte-twin `verifyBody`/`healthBody` collapsed to ONE
        // HealthCheckList shared with Fleet — the row layout cannot drift.
        HealthCheckList(state.checks, emptyMessage: "The cluster reported no verify checks.")
    }

    // MARK: - Daemon status

    @ViewBuilder
    private var daemonSection: some View {
        Section("Daemon") {
            switch daemon {
            case .idle, .loading:
                loadingRow("Loading daemon status…")
            case .failed(let message):
                errorLabel(message, retry: { Task { if let c = client { await loadDaemon(c) } } })
            case .loaded(let state):
                Label(state.speak, systemImage: state.daemon_running == true ? "checkmark.seal.fill" : "xmark.seal.fill")
                    .font(.subheadline)
                    .foregroundColor(state.daemon_running == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                if let pid = state.pid {
                    LabeledContent("PID") { Text("\(pid)") }
                }
                if let streams = state.streams, !streams.isEmpty {
                    let sorted = streams.sorted { $0.key < $1.key }
                    ForEach(sorted, id: \.key) { name, stream in
                        HStack(alignment: .top, spacing: Theme.Spacing.sm.rawValue) {
                            Image(systemName: stream.ok == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundColor(stream.ok == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                                Text(name)
                                    .font(.body)
                                if let msg = stream.message, !msg.isEmpty {
                                    Text(msg)
                                        .font(.caption)
                                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                }
                            }
                            Spacer(minLength: 0)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            case .stale:
                // daemon/escalations are not offline-cached (they are live
                // probes); `.stale` is unreachable for them.
                EmptyView()
            }
        }
    }

    // MARK: - Triggers

    @ViewBuilder
    private func triggersSection(client: HSCCClient) -> some View {
        Section {
            switch triggers {
            case .idle, .loading:
                loadingRow("Loading triggers…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadTriggers(client) } })
            case .loaded(let state):
                Text(state.speak)
                    .font(.footnote.italic())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                if let lastRun = state.last_run, let msg = lastRun.message, !msg.isEmpty {
                    Text("Last run: \(msg)")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                let rules = state.rules ?? []
                if rules.isEmpty {
                    HSEmpty("No rules",
                            message: "No trigger rules configured.",
                            systemImage: "bolt")
                } else {
                    ForEach(rules) { rule in
                        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                            Text(rule.id)
                                .font(.headline)
                            if let title = rule.trigger_params?.title, !title.isEmpty {
                                Text(title)
                                    .font(.caption)
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                            if let metric = rule.condition?.metric, !metric.isEmpty {
                                HStack(spacing: Theme.Spacing.xs.rawValue) {
                                    Text("when \(metric)")
                                    if let op = rule.condition?.op { Text(op).foregroundColor(Theme.Semantic.onSurfaceMuted) }
                                    if let value = rule.condition?.value {
                                        Text(DisplayJSON.render(value)).foregroundColor(Theme.Semantic.onSurfaceMuted)
                                    }
                                }
                                .font(.hsccMono(.caption))
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            case .stale:
                EmptyView()
            }
        } header: {
            Text("Triggers")
        } footer: {
            // The actions themselves are sticky at the bottom (spec §5); the
            // footer only explains what they do.
            Text("Both actions are confirm-gated: the confirm names the consequence, and the request fires only after you accept.")
        }
    }

    // MARK: - Escalations

    @ViewBuilder
    private func escalateSection(client: HSCCClient) -> some View {
        Section("Escalations") {
            switch escalations {
            case .idle, .loading:
                loadingRow("Loading escalations…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadEscalations(client) } })
            case .loaded(let state):
                Text(state.speak)
                    .font(.footnote.italic())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                let es = state.escalations ?? []
                if es.isEmpty {
                    HSEmpty("Nothing pending",
                            message: "No escalations are waiting.",
                            systemImage: "arrow.up.right.circle")
                } else {
                    ForEach(es.indices, id: \.self) { i in
                        Text(DisplayJSON.render(es[i]))
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            case .stale:
                EmptyView()
            }
        }
    }

    // MARK: - Profiles

    @ViewBuilder
    private var profilesSection: some View {
        Section("Profiles") {
            switch profiles {
            case .idle, .loading:
                loadingRow("Loading profiles…")
            case .failed(let message):
                errorLabel(message, retry: { Task { if let c = client { await loadProfiles(c) } } })
            case .loaded(let state):
                Text(state.speak)
                    .font(.footnote.italic())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                let counts = state.counts ?? [:]
                if counts.isEmpty {
                    HSEmpty("Idle",
                            message: "No profiles running tasks.",
                            systemImage: "person.3")
                } else {
                    ForEach(counts.sorted { $0.value > $1.value }, id: \.key) { profile, n in
                        HStack {
                            Text(profile)
                                .font(.body)
                            Spacer()
                            Text("\(n)")
                                .font(.hsccMono(.body, weight: .regular))
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                        .accessibilityLabel("\(profile): \(n) task\(n == 1 ? "" : "s")")
                    }
                }
            case .stale:
                EmptyView()
            }
        }
    }

    // MARK: - Shared building blocks

    private func loadingRow(_ label: String) -> some View {
        HStack(spacing: Theme.Spacing.sm.rawValue) {
            ProgressView()
            Text(label)
                .font(.subheadline)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private func errorLabel(_ message: String, retry: (() -> Void)? = nil) -> some View {
        HSErrorLabel(message: message, retry: retry)
    }
}
