import SwiftUI

/// Autodown control — the operator's most-used surface (C6).
///
/// Shows the live autodown report (enabled, state, idle limit, blocked-by,
/// cron notes, watchdog block) and hosts the confirm-gated controls: enable
/// (with an idle-minutes picker), disable, wake, and cancel. Every mutation
/// goes through `MutationButton`'s confirm dialog and sends `confirm: true`;
/// nothing here fires from a single tap.
///
/// Wake is special: the API returns `state: waking` immediately and runs
/// autoup() on a background thread (it can block ~9 minutes). This view shows
/// a waking state and polls /v1/autodown/status while `state == "waking"`
/// rather than blocking the UI or claiming success early.
struct AutodownView: View {
    let client: HSCCClient?

    @State private var status = LoadState<AutodownStatusResponse>.idle
    /// Idle-minutes value for the enable picker.
    @State private var idleMinutes = 30
    /// Force-arming toggle for enable (overrides the cron guard).
    @State private var force = false
    /// True while a wake is in flight and we're polling for the outcome.
    @State private var waking = false
    /// The "waking" hold message shown during the wake poll (from the API).
    @State private var wakeMessage: String?

    /// Idle-minute presets offered to the operator when enabling.
    private let idleOptions = [10, 20, 30, 60, 90, 120]

    var body: some View {
        NavigationStack {
            ScrollView {
                if let client {
                    VStack(alignment: .leading, spacing: 16) {
                        statusSection
                        controlSection(client: client)
                        cronSection
                    }
                    .padding()
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Autodown")
            .refreshable { if client != nil { await loadStatus() } }
            .task {
                if client != nil, status.value == nil, !status.isLoading {
                    await loadStatus()
                }
            }
        }
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "timer")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to control autodown.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    // MARK: - Load

    private func loadStatus() async {
        guard let client else { return }
        // Don't flip to a spinner if we already have a value (e.g. during the
        // wake poll), unless this is the very first load.
        if status.value == nil {
            status = .loading
        }
        do {
            let fresh = try await client.autodownStatus()
            status = .loaded(fresh)
            // Mirror the server's idle_minutes into the picker when the saved
            // config changes underneath us.
            if let m = fresh.idle_minutes, m > 0, idleMinutes != m {
                idleMinutes = m
            }
            // End the wake poll once the state leaves "waking".
            if waking, (fresh.state ?? "") != "waking" {
                waking = false
                wakeMessage = nil
            }
        } catch {
            if status.value == nil {
                status = .failed(errorMessage(for: error))
            }
            // If we held a value, keep it on screen but surface the refresh error.
        }
    }

    private func errorMessage(for error: Error) -> String {
        (error as? HSCCError)?.localizedDescription ?? "Something went wrong."
    }

    // MARK: - Status

    @ViewBuilder
    private var statusSection: some View {
        switch status {
        case .loading:
            sectionCard(title: "Status", systemImage: "timer") { ProgressView() }
        case .failed(let message):
            sectionCard(title: "Status", systemImage: "timer") { errorLabel(message) }
        case .loaded(let state):
            sectionCard(title: "Status", systemImage: "timer") {
                VStack(alignment: .leading, spacing: 12) {
                    if waking, let wakeMessage {
                        wakingBanner(wakeMessage)
                    }

                    // Summary line (design §B).
                    Text(state.speak)
                        .font(.subheadline)
                        .italic()
                        .foregroundColor(.secondary)

                    // Key status fields.
                    statusRow("State", value: state.state ?? "unknown",
                              color: stateColor(state.state))
                    statusRow("Idle limit", value: idleMinutesLabel(state.idle_minutes))
                    statusRow("Enabled", value: state.enabled == true ? "Yes" : "No",
                              color: state.enabled == true ? .green : .secondary)
                    if state.watchdog_blocked == true {
                        // `intentional` is a STRING ("autodown") during a teardown,
                        // not a Bool — an intentional block is expected, not a fault.
                        statusRow("Watchdog block",
                                  value: state.watchdog_intentional == nil ? "active" : "intentional (\(state.watchdog_intentional!))",
                                  color: state.watchdog_intentional == nil ? .red : .secondary)
                    }
                    if let blockedBy = state.blocked_by, !blockedBy.isEmpty {
                        blockedByRow(blockedBy)
                    }
                    if state.force_armed == true {
                        Label("Force-armed (cron guard overridden)",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundColor(.orange)
                    }
                }
            }
        default:
            EmptyView()
        }
    }

    /// A distinct banner shown while a wake is in progress and we're polling
    /// the API for the outcome (wake can take ~9 minutes — never block the UI).
    private func wakingBanner(_ message: String) -> some View {
        HStack(alignment: .top, spacing: 8) {
            ProgressView()
            VStack(alignment: .leading, spacing: 2) {
                Text("Waking the fleet…")
                    .font(.subheadline.weight(.semibold))
                Text(message)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .fill(Theme.Semantic.surfaceElevated))
    }

    private func stateColor(_ state: String?) -> Color {
        switch state {
        case "up": return .green
        case "down": return .red
        case "waking": return .orange
        default: return .secondary
        }
    }

    private func idleMinutesLabel(_ minutes: Int?) -> String {
        guard let minutes else { return "—" }
        return "\(minutes) min"
    }

    private func statusRow(_ label: String, value: String, color: Color = .secondary) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(.body)
                .foregroundColor(.secondary)
            Spacer()
            Text(value)
                .font(.body.weight(.medium))
                .foregroundColor(color)
        }
    }

    private func blockedByRow(_ text: String) -> some View {
        Label(text, systemImage: "hand.raised.fill")
            .font(.caption)
            .foregroundColor(.orange)
    }

    // MARK: - Controls (all confirm-gated)

    @ViewBuilder
    private func controlSection(client: HSCCClient) -> some View {
        let enabled = value?.enabled == true
        sectionCard(title: "Controls", systemImage: "slider.horizontal.3") {
            VStack(alignment: .leading, spacing: 14) {
                if enabled {
                    // Disable — confirm-gated. Names what will happen.
                    MutationButton(
                        title: "Disable Autodown",
                        systemImage: "pause.circle",
                        destructive: false,
                        prompt: "Disable autodown? The serving layer is NOT restarted (use Wake to bring it up).",
                        run: {
                            let result = try await client.autodownDisable()
                            await reloadAfterMutation()
                            return result.message ?? "Autodown disabled."
                        }
                    )

                    // Wake — confirm-gated; enters the polling "waking" state.
                    MutationButton(
                        title: "Wake Now",
                        systemImage: "bolt.circle",
                        destructive: false,
                        prompt: "Force-wake the serving layer now? This starts every serving unit and can take up to ~9 minutes.",
                        run: {
                            let result = try await client.autodownWake()
                            beginWaking(result)
                            return result.message ?? "Wake initiated."
                        }
                    )

                    // Cancel — confirm-gated.
                    MutationButton(
                        title: "Cancel Teardown",
                        systemImage: "xmark.circle",
                        destructive: true,
                        prompt: "Cancel an in-progress autodown teardown? Any stop already issued stays stopped.",
                        run: {
                            let result = try await client.autodownCancel()
                            await reloadAfterMutation()
                            return result.message ?? "Cancel requested."
                        }
                    )
                } else {
                    // Enable — confirm-gated, with an idle-minutes picker and a
                    // force toggle. The confirmation names what enabling does.
                    Picker("Idle minutes", selection: $idleMinutes) {
                        ForEach(idleOptions, id: \.self) { m in
                            Text("\(m) min").tag(m)
                        }
                    }
                    .pickerStyle(.menu)

                    Toggle("Force (override cron guard)", isOn: $force)
                        .font(.subheadline)

                    MutationButton(
                        title: "Enable Autodown",
                        systemImage: "play.circle",
                        prompt: "Enable autodown\(force ? " (forced)" : "") with a \(idleMinutes)-min idle limit? The cluster will power down after \(idleMinutes) min idle.",
                        run: {
                            let result = try await client.autodownEnable(idleMinutes: idleMinutes, force: force)
                            await reloadAfterMutation()
                            return result.message ?? "Autodown enabled (\(idleMinutes) min)."
                        }
                    )
                }
            }
        }
    }

    // MARK: - Cron notes

    @ViewBuilder
    private var cronSection: some View {
        if let state = value {
            let cpuOnly = state.active_cron_cpu_only ?? []
            let model = state.active_cron_model ?? []
            if !cpuOnly.isEmpty || !model.isEmpty {
                sectionCard(title: "Active cron jobs", systemImage: "calendar") {
                    VStack(alignment: .leading, spacing: 8) {
                        if !model.isEmpty {
                            Label("Model-requiring jobs block autodown: \(model.joined(separator: ", "))",
                                  systemImage: "exclamationmark.triangle.fill")
                                .font(.caption)
                                .foregroundColor(.orange)
                        }
                        if !cpuOnly.isEmpty {
                            Label("CPU-only jobs run through idle: \(cpuOnly.joined(separator: ", "))",
                                  systemImage: "cpu")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
        }
    }

    private var value: AutodownStatusResponse? { status.value }

    /// After a mutation that changes config, refresh the status so the screen
    /// reflects the new reality.
    private func reloadAfterMutation() async {
        await loadStatus()
    }

    /// Enter the waking state and start polling until the state leaves
    /// "waking" (wake can take ~9 minutes).
    private func beginWaking(_ result: AutodownWakeResponse) {
        waking = true
        wakeMessage = result.message
        // Poll from a background task so the UI stays responsive.
        Task {
            while waking {
                try? await Task.sleep(nanoseconds: 5_000_000_000) // 5s
                await loadStatus()
            }
        }
    }

    // MARK: - Shared building blocks

    private func sectionCard<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
    }

    private func errorLabel(_ message: String) -> some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.subheadline)
            .foregroundColor(Theme.Semantic.bad)
    }
}
