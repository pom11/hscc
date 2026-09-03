import SwiftUI

/// Ops / Health tab (C6) — the operational health surface.
///
/// Shows the full `hscc verify` per-check pass/fail, daemon status + every
/// health stream, trigger rules + last run, pending escalations, and profile
/// task counts. Each read is its own section with its own `LoadState`, so one
/// degraded endpoint never blanks the rest. Errors surface the real message.
/// Fleet-wide mutations live in FleetControlView and autodown controls in
/// AutodownView; this view hosts the operator's trigger/escalation actions —
/// "run triggers now" and "perform escalations" — both confirm-gated via
/// `MutationButton`. A tap never fires a request by itself.
struct OpsView: View {
    let client: HSCCClient?

    @State private var verify = LoadState<VerifyResponse>.idle
    @State private var daemon = LoadState<DaemonStatusResponse>.idle
    @State private var triggers = LoadState<TriggersResponse>.idle
    @State private var escalations = LoadState<EscalationsResponse>.idle
    @State private var profiles = LoadState<ProfilesResponse>.idle

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        verifySection
                        daemonSection
                        triggersSection(client: client)
                        escalateSection(client: client)
                        profilesSection
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Ops")
            .refreshable { if client != nil { await loadAll() } }
            .task {
                if client != nil, verify.value == nil, !verify.isLoading {
                    await loadAll()
                }
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

    // MARK: - Verify (per-check pass/fail)

    @ViewBuilder
    private var verifySection: some View {
        if let client {
            HSSectionCard(title: "Verify", systemImage: "checkmark.seal") {
                switch verify {
                case .loading:
                    ProgressView()
                case .failed(let message):
                    errorLabel(message)
                case .stale(let state, let ageMessage):
                    VStack(alignment: .leading, spacing: 10) {
                        StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                            Task { await loadVerify(client) }
                        }
                        verifyBody(state)
                    }
                case .loaded(let state):
                    verifyBody(state)
                default:
                    EmptyView()
                }
            }
        }
    }

    @ViewBuilder
    private func verifyBody(_ state: VerifyResponse) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(state.speak, systemImage: state.ok ? "checkmark.seal.fill" : "xmark.seal.fill")
                .font(.subheadline)
                .foregroundColor(state.ok ? Theme.Semantic.ok : Theme.Semantic.bad)
            if state.checks.isEmpty {
                emptyLabel("No checks reported.")
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

    // MARK: - Daemon status

    @ViewBuilder
    private var daemonSection: some View {
        HSSectionCard(title: "Daemon", systemImage: "server.rack") {
            switch daemon {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 10) {
                    Label(state.speak, systemImage: state.daemon_running == true ? "checkmark.seal.fill" : "xmark.seal.fill")
                        .font(.subheadline)
                        .foregroundColor(state.daemon_running == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                    if let pid = state.pid {
                        LabeledContent("PID") { Text("\(pid)") }
                    }
                    if let streams = state.streams, !streams.isEmpty {
                        Divider()
                        let sorted = streams.sorted { $0.key < $1.key }
                        ForEach(sorted, id: \.key) { name, stream in
                            HStack(alignment: .top, spacing: 8) {
                                Image(systemName: stream.ok == true ? "checkmark.circle.fill" : "xmark.circle.fill")
                                    .foregroundColor(stream.ok == true ? Theme.Semantic.ok : Theme.Semantic.bad)
                                VStack(alignment: .leading, spacing: 2) {
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
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Triggers

    @ViewBuilder
    private func triggersSection(client: HSCCClient) -> some View {
        HSSectionCard(title: "Triggers", systemImage: "bolt") {
            switch triggers {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 10) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    if let lastRun = state.last_run, let msg = lastRun.message, !msg.isEmpty {
                        Text("Last run: \(msg)")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                    let rules = state.rules ?? []
                    if rules.isEmpty {
                        emptyLabel("No trigger rules configured.")
                    } else {
                        ForEach(rules) { rule in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(rule.id)
                                    .font(.body.weight(.medium))
                                if let title = rule.trigger_params?.title, !title.isEmpty {
                                    Text(title)
                                        .font(.caption)
                                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                                }
                                if let metric = rule.condition?.metric, !metric.isEmpty {
                                    HStack(spacing: 6) {
                                        Text("when \(metric)")
                                        if let op = rule.condition?.op { Text(op).foregroundColor(Theme.Semantic.onSurfaceMuted) }
                                        if let value = rule.condition?.value {
                                            Text(displayJSON(value)).foregroundColor(Theme.Semantic.onSurfaceMuted)
                                        }
                                    }
                                    .font(.caption)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 2)
                        }
                    }
                    // Operator action: force a trigger-engine run now instead
                    // of waiting for the daemon's periodic cycle. Confirm-gated
                    // because enabled rules may fire notify / auto_restart /
                    // block_pipeline actions immediately.
                    Divider()
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
                }
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Escalations

    @ViewBuilder
    private func escalateSection(client: HSCCClient) -> some View {
        HSSectionCard(title: "Escalations", systemImage: "arrow.up.right.circle") {
            switch escalations {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 6) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    if let es = state.escalations, !es.isEmpty {
                        ForEach(es.indices, id: \.self) { i in
                            Text(displayJSON(es[i]))
                                .font(.caption)
                                .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        }
                    }
                    // Operator action: actually perform the pending failure
                    // escalations (reassign to the strong tier + notify a
                    // human), rather than the read-only dry-run the GET shows.
                    // Confirm-gated — this mutates tasks and notifies humans.
                    Divider()
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
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Profiles

    @ViewBuilder
    private var profilesSection: some View {
        HSSectionCard(title: "Profiles", systemImage: "person.3") {
            switch profiles {
            case .loading:
                ProgressView()
            case .failed(let message):
                errorLabel(message)
            case .loaded(let state):
                VStack(alignment: .leading, spacing: 8) {
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    let counts = state.counts ?? [:]
                    if counts.isEmpty {
                        emptyLabel("No profiles running tasks.")
                    } else {
                        ForEach(counts.sorted { $0.value > $1.value }, id: \.key) { profile, n in
                            HStack {
                                Text(profile)
                                    .font(.body)
                                Spacer()
                                Text("\(n)")
                                    .font(.body.monospacedDigit())
                                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                            }
                        }
                    }
                }
            default:
                EmptyView()
            }
        }
    }

    // MARK: - Shared building blocks


    private func errorLabel(_ message: String) -> some View { HSErrorLabel(message: message) }
    private func emptyLabel(_ text: String) -> some View { HSEmptyLabel(message: text) }

    private func displayJSON(_ value: JSONValue) -> String {
        switch value {
        case .string(let s): return s
        case .int(let n): return "\(n)"
        case .double(let d): return String(format: "%.2f", d)
        case .bool(let b): return b ? "true" : "false"
        case .null: return "null"
        case .object(let o):
            // Render small objects readably instead of the opaque "<complex>"
            // placeholder. Escalation entries are {task, action, category}
            // (live /v1/escalate) — the operator needs to SEE which tasks are
            // pending, not a placeholder that hides every field.
            let items = o.keys.sorted().compactMap { key -> String? in
                guard let v = o[key] else { return nil }
                return key + ": " + displayJSON(v)
            }
            return items.joined(separator: " · ")
        case .array(let a):
            return "[" + a.map { displayJSON($0) }.joined(separator: ", ") + "]"
        }
    }
}
