import SwiftUI

/// Approvals inbox + board hygiene — ONE destination (spec §3.1 drawer row
/// "Approvals"; §8 migration map: "BoardHygieneView → merged into Approvals
/// (same endpoint)"; §5: "Approvals (absorbs BoardHygiene — same
/// `/v1/kanban/blocked` endpoint, two row dialects today): one `List`,
/// Allow/Recover rows already named per-card, count in the title, empty state
/// → `HSEmpty`. Recover gets the same per-card label Allow has.").
///
/// When a worker hits something destructive (a forced push, a tear-down, a
/// credential edit) it does NOT push ahead — it stops and blocks, waiting for a
/// human to say "go" or "no". That decision lives in the shared kanban board
/// as a blocked card. This view turns `/v1/kanban/blocked` into a focused
/// approvals inbox: "worker wants to force-push X — allow?" with a one-tap
/// **Allow** path — and, since the §8 merge, it is also the whole blocked-card
/// picture: the cards that are NOT waiting on a human (auto-resume `dependency`
/// / `transient` blocks) sit in their own section with the same confirm-gated
/// Recover the old BoardHygiene screen offered, and the stale-card read
/// (`/v1/kanban/stale`, BoardHygiene's other half) is the trailing section.
///
/// What counts as a pending approval (see `BlockedCard.isPendingApproval`):
/// a blocked card whose `block_kind` needs a human decision — `needs_input`,
/// `capability`, or an unclassified circuit-breaker block (operator must judge
/// it either way). `dependency` and `transient` blocks are excluded: they
/// auto-resume and no human is in the loop — but the operator may still
/// recover one early, so they stay visible here rather than vanishing into a
/// retired screen.
///
/// Decision semantics (honest — one mutation exists in the backend):
///   * **Allow** / **Recover** = recover the card (`POST /v1/kanban/blocked/
///     {id}/recover`, confirm-gated) so the worker re-runs. This is the ONLY
///     backend mutation for a blocked card; the two buttons are the same
///     mutation phrased for the two reader intents (approve vs. unstick).
///   * **Don't allow** = leave it blocked (take no action). There is no fake
///     "deny" endpoint — inventing one would diverge from the daemon. The card
///     stays visibly in the inbox until the operator decides to recover it or
///     it is handled some other way.
///
/// LIST RECIPE (spec §5): three `Section`s of ONE plain `List` (Pending
/// approvals · Other blocked cards · Stale cards) plus the union of both
/// reads' errors. Each block keeps its own `LoadState` and its loading/failed/
/// stale-fallback states render as INLINE rows — never a full-pane spinner —
/// because a sibling section may already have content and the retry control
/// has to stay reachable (spec §5's per-section rule; the `StaleBanner` lives
/// inside the section whose data is last-known, the AutodownView recipe). The
/// old screen got that rule wrong twice over: two `List`s behind a segmented
/// picker, each replacing itself with a centred spinner on refresh.
struct ApprovalsView: View {
    let client: HSCCClient?

    /// Blocked cards — ONE read feeds both blocked sections (the pending
    /// filter is a pure partition of the response, so there is no second
    /// network read to keep in sync). Cached read: degrades to last-known.
    @State private var blocked = LoadState<KanbanBlockedResponse>.idle
    /// Stale cards — BoardHygiene's other half, and its own `LoadState`
    /// (spec §5's per-section rule: a dead `/v1/kanban/stale` must not blank
    /// the inbox above it). Cached like the blocked read: `get(path:queryItems:)`
    /// persists a non-paging query read under the plain path, so
    /// `Offline.load` has a real last-known value to fall back to (exactly
    /// what the pre-merge hygiene screen relied on).
    @State private var stale = LoadState<KanbanStaleResponse>.idle

    var body: some View {
        Group {
            if let client {
                List {
                    pendingSection(client: client)
                    otherBlockedSection(client: client)
                    staleSection(client: client)
                    errorsSection
                }
                .listStyle(.plain)
                .refreshable { await loadAll(client) }
                .task {
                    if blocked.value == nil, !blocked.isLoading {
                        await loadAll(client)
                    }
                }
            } else {
                notConfiguredView
            }
        }
        // Spec §5: "count in the title". Derived from the SAME response the
        // list renders (its own partition), not from the shell's
        // separately-polled badge count — so the title can never disagree
        // with the rows below it. Nil count (loading/failed) = plain title.
        //
        // The count is every blocked card this screen can act on (pending +
        // the other-blocked section), NOT the stale cards: a stale card has
        // no action here, and the drawer badge (ApprovalPoller) keeps its
        // pending-only meaning — title and badge then answer two honest
        // questions ("what can I decide here" vs. "what is waiting on you").
        .navigationTitle(blockedCount.map { "Approvals (\($0))" } ?? "Approvals")
    }

    /// Every blocked card in the currently-loaded (or last-known) read —
    /// both blocked sections. This is what "(N)" in the title counts.
    private var blockedCount: Int? {
        (blocked.value?.tasks ?? []).count
    }

    /// Pending approvals in the currently-loaded (or last-known) read.
    private var pendingCount: Int? {
        (blocked.value?.tasks ?? []).filter(\.isPendingApproval).count
    }

    private var notConfiguredView: some View {
        HSConnectGate(systemImage: "checkmark.seal", verb: "to review approvals")
    }

    // MARK: - Load

    /// Both reads fire together on first load and on pull-to-refresh, but each
    /// writes its own `LoadState` and neither awaits the other's success: a
    /// slow or dead `/v1/kanban/stale` must not delay or blank the approvals
    /// inbox the operator came for (spec §5, per-section LoadState).
    private func loadAll(_ client: HSCCClient) async {
        async let b: Void = loadBlocked(client)
        async let s: Void = loadStale(client)
        _ = await (b, s)
    }

    private func loadBlocked(_ client: HSCCClient) async {
        // Don't replace content with a spinner on a refresh.
        if blocked.value == nil {
            blocked = .loading
        }
        blocked = await Offline.load(blocked,
                                     cacheKey: EndpointPath.kanbanBlocked,
                                     client: client) {
            try await client.kanbanBlocked()
        }
    }

    private func loadStale(_ client: HSCCClient) async {
        if stale.value == nil {
            stale = .loading
        }
        stale = await Offline.load(stale,
                                   cacheKey: EndpointPath.kanbanStale,
                                   client: client) {
            try await client.kanbanStale(olderThan: 0)
        }
    }

    // MARK: - Pending approvals

    /// The inbox the operator came for. The blocked read's stale fallback
    /// banner lives INSIDE this section (AutodownView's recipe from the same
    /// §8 wave: a section that is last-known data carries its own inline
    /// `StaleBanner`, so "Offline" is never printed over a live sibling).
    @ViewBuilder
    private func pendingSection(client: HSCCClient) -> some View {
        Section {
            switch blocked {
            case .idle, .loading:
                loadingRow("Loading approvals…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadBlocked(client) } })
            case .stale(let response, let ageMessage):
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadAll(client) }
                }
                pendingBody(response, client: client)
            case .loaded(let response):
                pendingBody(response, client: client)
            }
        } header: {
            Text(pendingCount.map { "Pending approvals (\($0))" } ?? "Pending approvals")
        } footer: {
            Text("Waiting on your decision. Allow recovers the card so the worker proceeds.")
        }
    }

    @ViewBuilder
    private func pendingBody(_ response: KanbanBlockedResponse, client: HSCCClient) -> some View {
        let pending = (response.tasks ?? []).filter(\.isPendingApproval)
        Text(response.speak)
            .font(.footnote.italic())
            .foregroundColor(Theme.Semantic.onSurfaceMuted)
        if pending.isEmpty {
            // Spec §5: the "nothing to do" state was a `Section` with a green
            // `Label` — the same idea the other screens render with `HSEmpty`,
            // but a different dialect. One shared empty view now.
            HSEmpty("No pending approvals",
                    message: "Nothing is waiting on you right now.",
                    systemImage: "checkmark.seal.fill")
        }
        ForEach(pending) { card in
            approvalRow(card, client: client)
        }
    }

    // MARK: - Other blocked cards (BoardHygiene's blocked pane)

    @ViewBuilder
    private func otherBlockedSection(client: HSCCClient) -> some View {
        // Only a partition of the SAME response — never shown while that
        // response is still loading or failed (the pending section already
        // carries the spinner/retry for this read), and never shown empty
        // ("nothing else is blocked" is the absence of a section, not a row).
        if let response = blocked.value {
            let others = (response.tasks ?? []).filter { !$0.isPendingApproval }
            if !others.isEmpty {
                Section {
                    ForEach(others) { card in
                        blockedRow(card, client: client)
                    }
                } header: {
                    Text("Other blocked cards (\(others.count))")
                } footer: {
                    Text("Auto-resume blocks (waiting on another card, or transient) — no human decision required, but you can recover one early.")
                }
            }
        }
    }

    // MARK: - Stale cards (BoardHygiene's stale pane)

    @ViewBuilder
    private func staleSection(client: HSCCClient) -> some View {
        Section {
            switch stale {
            case .idle, .loading:
                loadingRow("Loading stale cards…")
            case .failed(let message):
                errorLabel(message, retry: { Task { await loadStale(client) } })
            case .stale(let response, let ageMessage):
                // Same inline-banner recipe as the pending section (AutodownView's
                // history section established it for a second, independent read):
                // the section that is last-known data says so itself, with its
                // own retry — no screen-wide "Offline" over live siblings.
                StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.") {
                    Task { await loadStale(client) }
                }
                Text(response.speak)
                    .font(.footnote.italic())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                staleBody(response)
            case .loaded(let response):
                Text(response.speak)
                    .font(.footnote.italic())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                staleBody(response)
            }
        } header: {
            Text(staleCount.map { "Stale cards (\($0))" } ?? "Stale cards")
        } footer: {
            Text("Non-terminal cards that have not moved (every board, any age). Read-only — recovering a blocked card happens above.")
        }
    }

    private var staleCount: Int? {
        (stale.value?.tasks ?? []).count
    }

    @ViewBuilder
    private func staleBody(_ response: KanbanStaleResponse) -> some View {
        let tasks = response.tasks ?? []
        if tasks.isEmpty {
            HSEmpty("No stale cards",
                    message: "Every card on every board has moved recently.",
                    systemImage: "clock.arrow.circlepath")
        }
        ForEach(tasks) { card in
            VStack(alignment: .leading, spacing: Theme.Spacing.xxs.rawValue) {
                Text(card.displayTitle)
                    .font(.body)
                HSMetaLine([card.board, card.status, card.assignee,
                            card.age_days.map { "\($0)d old" }])
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: - Errors

    /// Both reads report per-board errors; the screen shows the union once
    /// (two identical "Errors" sections would be the two-dialect split again).
    /// Rows are id'd by OFFSET: the two reads can report the identical error
    /// string and a List row id must be unique (the same trap the merged
    /// Autodown history hit with server-collapsed events).
    @ViewBuilder
    private var errorsSection: some View {
        let all = (blocked.value?.errors ?? []) + (stale.value?.errors ?? [])
        if !all.isEmpty {
            Section("Errors") {
                ForEach(Array(all.enumerated()), id: \.offset) { _, error in
                    Text(error).font(.caption).foregroundColor(Theme.Semantic.bad)
                }
            }
        }
    }

    // MARK: - Rows (the ONE dialect per card kind — §5's "two row dialects"
    // were between two SCREENS reading the same endpoint; within this screen
    // each kind of card has exactly one row, unchanged from where it lived)

    /// Pending-approval row — kept verbatim from the pre-merge inbox, so the
    /// operator's decision surface did not shift under them.
    @ViewBuilder
    private func approvalRow(_ card: BlockedCard, client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // The ask, phrased as what's being requested ("worker wants to X").
            Text(card.displayTitle)
                .font(.body.weight(.semibold))
            HStack(spacing: 6) {
                if let assignee = card.assignee, !assignee.isEmpty {
                    Text(assignee).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if let board = card.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                if let age = card.age_days {
                    Text("\(age)d").font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
                }
                Spacer()
                // The sixth of §6's bespoke badges ("Approvals comment
                // chips"): a warn-tinted `Label`, the only badge left in the
                // app that wasn't HSStatusChip. Same role (warn), now the same
                // chip — so it can't drift from the other five again.
                HSStatusChip(approvalKindLabel(card.block_kind),
                             systemImage: "hand.raised.fill",
                             color: Theme.Semantic.warn)
            }
            // A human-readable why (the reason the worker blocked), when the
            // server recorded one.
            if let why = card.why, !why.isEmpty, why != "kind=\(card.block_kind ?? "")" {
                Text(why).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            // The actual comments carry the worker's request / context.
            if let comments = card.comments, !comments.isEmpty {
                ForEach(comments.prefix(2), id: \.self) { comment in
                    Text(comment)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .padding(Theme.Spacing.sm.rawValue)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: Theme.Corner.badge.rawValue, style: .continuous)
                                .fill(Theme.Semantic.surfaceElevated)
                        )
                }
            }

            // Allow — confirm-gated, one card at a time. Names the card so the
            // operator knows exactly what will be re-run.
            MutationButton(
                title: "Allow",
                systemImage: "checkmark.seal",
                prompt: "Allow card \(card.id) (\"\(card.displayTitle)\") to re-run? This recovers the blocked card so the worker proceeds with \(card.displayTitle). Only allow a request you're sure is safe.",
                run: {
                    let result = try await client.recoverBlockedCard(card.id)
                    await loadBlocked(client)
                    return result.message ?? "Allowed \(card.id)."
                }
            )
            .font(.caption)
            .buttonStyle(.borderless)
            .foregroundColor(Theme.Semantic.ok)
            // A11Y: each row has its own Allow; VoiceOver reads the bare label
            // "Allow" identically for every row and the user can't tell which
            // card a given button acts on. Name the card so the two are linked.
            .accessibilityLabel("Allow \(card.displayTitle)")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Non-pending blocked row — BoardHygiene's row, kept verbatim including
    /// the per-card Recover label that landed in dca7f25c.
    @ViewBuilder
    private func blockedRow(_ card: BlockedCard, client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            Text(card.displayTitle)
                .font(.body)
            HSMetaLine([card.board, card.assignee,
                        card.age_days.map { "\($0)d" }])
            if let kind = card.block_kind, !kind.isEmpty {
                HSStatusChip(kind, systemImage: "hand.raised.fill", color: Theme.Semantic.warn)
            }
            if let why = card.why, !why.isEmpty {
                Text(why).font(.caption).foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
            // Recover — confirm-gated, one card at a time. Names the card so
            // the operator knows exactly what will be re-run.
            MutationButton(
                title: "Recover",
                systemImage: "arrow.counterclockwise",
                prompt: "Recover \(card.id) (\"\(card.displayTitle)\") to ready so it re-runs? Only recover a card you're sure is safe to re-run.",
                run: {
                    let result = try await client.recoverBlockedCard(card.id)
                    await loadBlocked(client)
                    return result.message ?? "Recovered \(card.id)."
                }
            )
            .font(.caption)
            .buttonStyle(.borderless)
            // Spec §5: "Recover gets the same per-card label Allow has."
            // Approvals' Allow already names its card; this row's Recover read
            // as a bare "Recover" for every row, so VoiceOver couldn't say
            // which blocked card it would re-run.
            .accessibilityLabel("Recover \(card.id) (\(card.displayTitle))")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func approvalKindLabel(_ kind: String?) -> String {
        switch kind {
        case "needs_input": return "Needs a decision"
        case "capability": return "Blocked on access"
        case nil: return "Unclassified"
        default: return "Blocked"
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
