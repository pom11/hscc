import SwiftUI

/// Autodown control + self-heal history — ONE destination (spec §3.1 drawer row
/// "Autodown & self-heal"; §8 migration map: "AutodownView + SelfHealHistoryView
/// → drawer · MERGED, history as a section").
///
/// Shows the live autodown report (enabled, state, idle limit, blocked-by,
/// cron notes, watchdog block) and hosts the confirm-gated controls: enable
/// (with an idle-minutes picker), disable, wake, and cancel. Every mutation
/// goes through `MutationButton`'s confirm dialog and sends `confirm: true`;
/// nothing here fires from a single tap.
///
/// The self-heal timeline (was its own screen, `SelfHealHistoryView`, retired
/// by this merge) is the trailing Section of the same List: what the daemon's
/// automation actually DID — watchdog restarts, pipeline blocks/escalations,
/// autodown transitions and recovery relaunches — with timestamps, outcomes
/// and durations. Read-only and bounded (at most `maxEvents` distinct events;
/// the server clamps further and reads O(window), never the whole log) and it
/// COLLAPSES the 30s-cycle repeats into distinct events carrying
/// `repeats`/`last_ts`, so a multi-hour "autodown in effect" window renders as
/// one timeline entry, not hundreds of identical rows.
///
/// Why they belong on one screen: every timeline row is either an autodown
/// transition or something a wake/recovery interacts with, and the operator
/// reads the history to judge the very control this screen holds. One screen,
/// one pull-to-refresh, and the history keeps its own LoadState — a dead
/// `/v1/daemon/history` cannot blank the autodown report (spec §5).
///
/// Wake is special: the API returns `state: waking` immediately and runs
/// autoup() on a background thread (it can block ~9 minutes). This view shows
/// a waking state and polls /v1/autodown/status while `state == "waking"`
/// rather than blocking the UI or claiming success early.
///
/// LIST RECIPE (spec §5, Phase 2 C3, landed for this screen in 79be2dd5): four
/// `Section`s of ONE plain `List` (Status · Controls · Active cron jobs ·
/// Self-heal history). List supplies the grouping, the caps headers and the
/// separators, and `speak` is a footnote caption line instead of a boxed italic
/// subheadline. The status fields are `LabeledContent` rows. Each block keeps
/// its own `LoadState` and its loading/failed states render as INLINE rows —
/// never a full-pane spinner — because the sibling sections may have content
/// and the retry control has to stay reachable (spec §5's per-section rule).
struct AutodownView: View {
    let client: HSCCClient?

    @State private var status = LoadState<AutodownStatusResponse>.idle
    /// The self-heal timeline — its own LoadState, so it loads (and fails)
    /// independently of the autodown status (§5 per-section rule).
    @State private var history = LoadState<[HistoryEvent]>.idle
    /// Idle-minutes value for the enable picker.
    @State private var idleMinutes = 30
    /// Force-arming toggle for enable (overrides the cron guard).
    @State private var force = false
    /// True while a wake is in flight and we're polling for the outcome.
    @State private var waking = false
    /// The "waking" hold message shown during the wake poll (from the API).
    @State private var wakeMessage: String?
    /// Presents the fleet-wake Live Activity (Dynamic Island / Lock Screen).
    /// Started from `beginWaking`; ends with success/failure. Independent of
    /// the in-app poll, so the operator can leave the app and still see the wake.
    @State private var liveActivity = LiveActivityManager()

    /// Idle-minute presets offered to the operator when enabling.
    private let idleOptions = [10, 20, 30, 60, 90, 120]

    /// How many distinct history events this screen ever requests (bounded;
    /// the server caps likewise so the payload stays small).
    private let maxEvents = 200

    var body: some View {
        Group {
            if let client {
                List {
                    statusSection
                    // Only show actionable controls once we actually KNOW the
                    // current enabled state — see controlSection.
                    if let value {
                        controlSection(client: client, value: value)
                    }
                    cronSection
                    historySection
                }
                .listStyle(.plain)
                .refreshable { await loadAll() }
                .task {
                    if status.value == nil, !status.isLoading {
                        await loadAll()
                    }
                }
            } else {
                notConfiguredView
            }
        }
        .navigationTitle("Autodown & self-heal")
        .navigationBarTitleDisplayMode(.inline)
    }

    // MARK: - Not configured

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "timer", verb: "to control autodown and read the self-heal timeline")
    }

    // MARK: - Load

    private func loadStatus() async {
        guard let client else { return }
        // Don't flip to a spinner if we already have a value (e.g. during the
        // wake poll), unless this is the very first load.
        if status.value == nil {
            status = .loading
        }
        status = await Offline.load(status,
                                    cacheKey: EndpointPath.autodownStatus,
                                    client: client) {
            try await client.autodownStatus()
        }
        // Mirror the server's idle_minutes into the picker when the saved
        // config changes underneath us.
        if let fresh = status.value,
           let m = fresh.idle_minutes, m > 0, idleMinutes != m {
            idleMinutes = m
        }
        // End the wake poll once the state leaves "waking".
        if waking, (status.value?.state ?? "") != "waking" {
            waking = false
            wakeMessage = nil
        }
    }

    /// Both reads fire together on first load and on pull-to-refresh, but each
    /// writes its own `LoadState` and neither awaits the other's success: a
    /// slow or dead `/v1/daemon/history` must not delay or blank the autodown
    /// report the operator came for (spec §5, per-section LoadState).
    private func loadAll() async {
        async let s: Void = loadStatus()
        async let h: Void = loadHistory()
        _ = await (s, h)
    }

    /// History is deliberately NOT routed through `Offline.load`: it is a
    /// query-parameter GET (`?limit=`) and `HSCCClient.get` only persists
    /// cache-free reads, so there is no persisted last-known value to fall
    /// back to. What we CAN do honestly is keep showing whatever this session
    /// already loaded and say it is earlier history.
    private func loadHistory() async {
        guard let client else { return }
        let current = history
        // Same rule as status: don't replace content with a spinner on a refresh.
        if history.value == nil {
            history = .loading
        }
        do {
            let response = try await client.daemonHistory(limit: maxEvents)
            history = .loaded(response.events)
        } catch {
            if let held = current.value, !held.isEmpty {
                history = .stale(held, "showing earlier history")
            } else {
                history = .failed(operatorErrorMessage(error))
            }
        }
    }

    // MARK: - Status

    @ViewBuilder
    private var statusSection: some View {
        Section("Status") {
            switch status {
            case .idle, .loading:
                loadingRow("Loading autodown status…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadStatus() } })
            case .stale(let state, let ageMessage):
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadStatus() }
                }
                statusBody(state)
            case .loaded(let state):
                statusBody(state)
            }
        }
    }

    @ViewBuilder
    private func statusBody(_ state: AutodownStatusResponse) -> some View {
        if waking, let wakeMessage {
            wakingBanner(wakeMessage)
        }

        // Summary line (design §B).
        Text(state.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)

        // Key status fields.
        LabeledContent("State") {
            Text(stateLabel(state.state)).foregroundColor(stateColor(state.state))
        }
        LabeledContent("Idle limit") { Text(idleMinutesLabel(state.idle_minutes)) }
        LabeledContent("Enabled") {
            Text(state.enabled == true ? "Yes" : "No")
                .foregroundColor(state.enabled == true ? Theme.Semantic.ok : Theme.Semantic.onSurfaceMuted)
        }
        if state.watchdog_blocked == true {
            // `intentional` is a STRING ("autodown") during a teardown,
            // not a Bool — an intentional block is expected, not a fault.
            LabeledContent("Watchdog block") {
                Text(state.watchdog_intentional == nil ? "active" : "intentional (\(state.watchdog_intentional!))")
                    .foregroundColor(state.watchdog_intentional == nil ? Theme.Semantic.bad : Theme.Semantic.onSurfaceMuted)
            }
        }
        if let blockedBy = state.blocked_by, !blockedBy.isEmpty {
            blockedByRow(blockedBy)
        }
        if state.force_armed == true {
            Label("Force-armed (cron guard overridden)",
                  systemImage: "exclamationmark.triangle.fill")
                .font(.caption)
                .foregroundColor(Theme.Semantic.warn)
        }
    }

    /// Shown while a wake is in progress and we're polling the API for the
    /// outcome (wake can take ~9 minutes — never block the UI).
    ///
    /// Spec §1.3 counted this as "a sibling of StaleBanner written separately",
    /// and it now IS the same recipe — one `HSBanner` in Theme.swift, which is
    /// also what StaleBanner renders through. What it deliberately does NOT
    /// reuse is StaleBanner's *copy*: that one prints "Offline — <age>", and a
    /// fleet that is coming up is not a fleet that is unreachable. Printing
    /// "Offline — waking the fleet…" on a live API returning `state: waking`
    /// would be exactly the word/state disagreement spec §9 bans, so the
    /// geometry collapsed and the meaning stayed: spinner-led, neutral, and it
    /// says "Waking the fleet…".
    private func wakingBanner(_ message: String) -> some View {
        HSBanner(leading: .spinner,
                 tone: .neutral,
                 title: "Waking the fleet…",
                 reason: message)
    }

    /// A `down` state is NORMAL when autodown is armed and idle — the fleet is
    /// intentionally down, not faulting. Say so plainly instead of implying a
    /// fault (the offline feature's core honesty rule).
    private func stateLabel(_ state: String?) -> String {
        state == "down" ? "down (intentional)" : (state ?? "unknown")
    }

    private func stateColor(_ state: String?) -> Color {
        switch state {
        case "up": return Theme.Semantic.ok
        case "down": return Theme.Semantic.neutral
        case "waking": return Theme.Semantic.warn
        default: return Theme.Semantic.onSurfaceMuted
        }
    }

    private func idleMinutesLabel(_ minutes: Int?) -> String {
        guard let minutes else { return "—" }
        return "\(minutes) min"
    }

    private func blockedByRow(_ text: String) -> some View {
        Label(text, systemImage: "hand.raised.fill")
            .font(.caption)
            .foregroundColor(Theme.Semantic.warn)
    }

    // MARK: - Controls (all confirm-gated)

    /// `value` is the loaded status: the controls are only shown once the
    /// screen actually KNOWS the enabled state. Before a status value has
    /// arrived (loading / idle / failed-with-no-fallback) `value?.enabled` is
    /// nil, which would drop us into the ENABLE branch and show a misleading
    /// "Enable Autodown" set on an already-armed cluster — the operator's
    /// most-used surface, and every visit used to flash the wrong controls at
    /// the start of a load.
    @ViewBuilder
    private func controlSection(client: HSCCClient, value: AutodownStatusResponse) -> some View {
        let enabled = value.enabled == true
        Section("Controls") {
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
                .accessibilityLabel("Disable autodown")

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
                .accessibilityLabel("Wake the serving fleet now")

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
                .accessibilityLabel("Cancel autodown teardown")
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
                .accessibilityLabel("Enable autodown with \(idleMinutes) minute idle limit")
            }
        }
    }

    // MARK: - Cron notes

    /// Only appears when the server actually reports cron jobs — an empty
    /// "no cron jobs" section would be noise on the operator's most-used
    /// screen (this was the behaviour inside the old card and it is kept).
    @ViewBuilder
    private var cronSection: some View {
        if let state = value {
            let cpuOnly = state.active_cron_cpu_only ?? []
            let model = state.active_cron_model ?? []
            if !cpuOnly.isEmpty || !model.isEmpty {
                Section("Active cron jobs") {
                    if !model.isEmpty {
                        Label("Model-requiring jobs block autodown: \(model.joined(separator: ", "))",
                              systemImage: "exclamationmark.triangle.fill")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.warn)
                    }
                    if !cpuOnly.isEmpty {
                        Label("CPU-only jobs run through idle: \(cpuOnly.joined(separator: ", "))",
                              systemImage: "cpu")
                            .font(.caption)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            }
        }
    }

    private var value: AutodownStatusResponse? { status.value }

    // MARK: - Self-heal history
    //
    // Was its own screen (`SelfHealHistoryView`, retired by this §8 merge). It
    // keeps its own `LoadState` and its own row dialect: loading and failed are
    // INLINE rows rather than a centred `HSLoading`/`HSError`, because the
    // sections above it may well have content and the retry has to stay
    // reachable (spec §5's per-section rule).

    /// No `client` parameter (unlike `controlSection`): the retry closures
    /// re-enter `loadHistory()`, which guards on the client itself.
    @ViewBuilder
    private var historySection: some View {
        Section {
            switch history {
            case .idle, .loading:
                loadingRow("Scanning daemon log…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadHistory() } })
            case .stale(let events, let ageMessage):
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadHistory() }
                }
                historyBody(events)
            case .loaded(let events):
                historyBody(events)
            }
        } header: {
            Text("Self-heal history")
        } footer: {
            Text("What the daemon's automation decided while you were away — watchdog restarts, blocks, autodown transitions and recoveries. Read-only.")
        }
    }

    @ViewBuilder
    private func historyBody(_ events: [HistoryEvent]) -> some View {
        if events.isEmpty {
            HSEmpty("No automation history",
                    message: "No watchdog restarts, blocks, autodown or recovery events were found in the recent window.",
                    systemImage: "clock.arrow.circlepath")
        } else {
            summaryStrip(events)
            // Server returns oldest-first (it scans backward and reverses); for
            // a "what happened most recently" read show newest on top. The row
            // id is the offset, because a collapsed run CAN repeat the same
            // timestamp/kind/detail and a List row id must be unique (the old
            // LazyVStack used `event.id` and tolerated duplicate ids only
            // because it wasn't a List).
            let newestFirst = Array(events.reversed())
            ForEach(Array(newestFirst.enumerated()), id: \.offset) { _, event in
                HistoryRow(event: event)
            }
            footerText(events.count)
        }
    }

    /// A compact count strip: how many of each kind appear in the loaded
    /// timeline. Reveals the shape of a night's automation at a glance.
    private func summaryStrip(_ events: [HistoryEvent]) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.sm.rawValue) {
            HStack(spacing: Theme.Spacing.md.rawValue) {
                summaryBlock("Restarts", count(events, .restart), Theme.Semantic.warn, "arrow.triangle.2.circlepath")
                summaryBlock("Blocks", count(events, .block), Theme.Semantic.bad, "hand.raised.fill")
                summaryBlock("Autodown", count(events, .autodown), Theme.Semantic.warn, "powersleep")
                summaryBlock("Recoveries", count(events, .recovery), Theme.Semantic.ok, "arrow.clockwise.heart")
            }
            Text("Distinct automated decisions in the recent window — run-collapsed.")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func count(_ events: [HistoryEvent], _ kind: HistoryEventKind) -> Int {
        events.lazy.filter { $0.kind == kind }.count
    }

    private func summaryBlock(_ title: String, _ n: Int, _ color: Color, _ icon: String) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
            HStack(spacing: Theme.Spacing.xxs.rawValue) {
                Image(systemName: icon).font(.caption).foregroundColor(color)
                Text(title).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            Text("\(n)")
                .font(.hsccMono(20, weight: .bold))
                .foregroundColor(Theme.Semantic.onSurface)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private func footerText(_ n: Int) -> some View {
        Text("\(n) distinct automated \(n == 1 ? "decision" : "decisions") in the recent window. Read-only.")
            .font(.caption2)
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// After a mutation that changes config, refresh the status so the screen
    /// reflects the new reality. The history is NOT re-read here: a mutation
    /// cannot have produced an automation event yet (the daemon logs the
    /// consequence on its next cycle), and scanning daemon.log for it mid-tap
    /// is a slower read than the operator is waiting on.
    private func reloadAfterMutation() async {
        await loadStatus()
    }

    /// Enter the waking state and start tracking the wake — BOTH the in-app
    /// poll AND the Live Activity (the operator can close the app and still see
    /// the wake on the Dynamic Island / Lock Screen).
    private func beginWaking(_ result: AutodownWakeResponse) {
        waking = true
        wakeMessage = result.message
        // Start the Live Activity + its own polling; it ends when the state
        // settles (up = success, down = failure, both with an explicit message).
        if let client {
            liveActivity.beginWake(client: client)
        }
        // Poll from a background task so the UI stays responsive.
        Task {
            while waking {
                try? await Task.sleep(nanoseconds: 5_000_000_000) // 5s
                await loadStatus()
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

    private func errorLabel(_ message: String, retry: (() -> Void)? = nil) -> some View { HSErrorLabel(message: message, retry: retry) }
}

/// One timeline row: an event kind chip + outcome label over a mono timestamp
/// and the redacted detail line, with a duration caption for collapsed runs.
///
/// Moved here with the §8 merge (was `SelfHealHistoryView`'s private row). The
/// List supplies the separator now: the old screen hand-rolled a card fill, a
/// hairline stroke and a `Divider()` per row — spec §1.3's "Logs and
/// SelfHealHistory share a card+stroke recipe verbatim" duplicate — so the row
/// is just content.
private struct HistoryRow: View {
    let event: HistoryEvent

    private var color: Color { event.resolvedOutcome.tint }
    private var kindLabel: String { event.kind?.label ?? "Event" }
    var body: some View {
        HStack(alignment: .top, spacing: Theme.Spacing.md.rawValue) {
            // Leading marker on the timeline spine.
            Circle()
                .fill(color)
                .frame(width: 10, height: 10)
                .padding(.top, 6)
                .accessibilityHidden(true)

            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                HStack(spacing: Theme.Spacing.sm.rawValue) {
                    HSStatusChip(kindLabel, systemImage: kindIcon, color: color)
                    Text(outcomeText)
                        .font(.caption.weight(.semibold))
                        .foregroundColor(color)
                    Spacer(minLength: 0)
                }

                if let detail = event.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.footnote.monospaced())
                        .foregroundColor(Theme.Semantic.onSurface)
                        .textSelection(.enabled)
                }

                HSMetaLine([timestampText, durationText].filter { !$0.isEmpty })
            }
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }

    private var outcomeText: String {
        switch event.resolvedOutcome {
        case .success: return "Success"
        case .failed:  return "Failed"
        case .cleared: return "Cleared"
        case .attempt: return "Attempted"
        case .wake:    return "Wake"
        case .started: return "Started"
        case .recon:   return "Reconciled"
        case .info:    return "Info"
        }
    }

    private var kindIcon: String {
        switch event.kind {
        case .restart:  return "arrow.triangle.2.circlepath"
        case .block:    return "hand.raised.fill"
        case .autodown: return "powersleep"
        case .recovery: return "arrow.clockwise.heart"
        case .escalate: return "exclamationmark.triangle"
        case nil:       return "clock.arrow.circlepath"
        }
    }

    /// Compact local timestamp from the ISO-8601 string the server serves.
    /// Parses tolerantly and falls back to the raw string — never blanks.
    private var timestampText: String {
        guard let ts = event.timestamp, !ts.isEmpty else { return "no timestamp" }
        // "2026-08-27T07:12:31" -> "Aug 27, 07:12"
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: ts)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: ts)
        }
        guard let date else { return ts }
        let out = DateFormatter()
        out.dateFormat = "MMM d HH:mm"
        return out.string(from: date)
    }

    /// Human duration for a collapsed run ("for 26 min") — empty for a single
    /// event or when the run dimensions are missing.
    private var durationText: String {
        guard let last = event.lastTs, let first = event.timestamp else { return "" }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var a = f.date(from: first)
        var b = f.date(from: last)
        if a == nil { f.formatOptions = [.withInternetDateTime]; a = f.date(from: first) }
        if b == nil { f.formatOptions = [.withInternetDateTime]; b = f.date(from: last) }
        guard let a, let b else { return "" }
        let seconds = Int(b.timeIntervalSince(a))
        guard seconds > 0 else { return "" }
        if seconds < 60 { return "\(seconds)s" }
        let minutes = seconds / 60
        if minutes < 60 { return "for \(minutes) min" }
        let hours = minutes / 60
        return "for \(hours) h \(minutes % 60) min"
    }
}

/// Row accent colour per outcome (from the app's semantic roles). Lives in the
/// view layer — the model stays UI-free (Foundation only) so it can decode in
/// the headless fixture harness.
extension HistoryOutcome {
    var tint: Color {
        switch self {
        case .success, .cleared: return Theme.Semantic.ok
        case .failed:            return Theme.Semantic.bad
        case .recon, .wake, .attempt, .started: return Theme.Semantic.warn
        case .info:              return Theme.Semantic.neutral
        }
    }
}
