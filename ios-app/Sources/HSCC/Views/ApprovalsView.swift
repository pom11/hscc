import SwiftUI

/// Approvals inbox (t_9a5cfc3b) — the phone's decision surface for blocked
/// kanban cards that are genuinely waiting on a human.
///
/// When a worker hits something destructive (a forced push, a tear-down, a
/// credential edit) it does NOT push ahead — it stops and blocks, waiting for a
/// human to say "go" or "no". That decision lives in the shared kanban board
/// as a blocked card. This view turns `/v1/kanban/blocked` into a focused
/// approvals inbox: "worker wants to force-push X — allow?" with a one-tap
/// **Allow** path.
///
/// What counts as a pending approval (see `BlockedCard.isPendingApproval`):
/// a blocked card whose `block_kind` needs a human decision — `needs_input`,
/// `capability`, or an unclassified circuit-breaker block (operator must judge
/// it either way). `dependency` and `transient` blocks are excluded: they
/// auto-resume and no human is in the loop.
///
/// Decision semantics (honest — one mutation exists in the backend):
///   * **Allow** = recover the card (`POST /v1/kanban/blocked/{id}/recover`,
///     confirm-gated) so the worker re-runs. This is the ONLY backend mutation
///     for a blocked card.
///   * **Don't allow** = leave it blocked (take no action). There is no fake
///     "deny" endpoint — inventing one would diverge from the daemon. The card
///     stays visibly in the inbox until the operator decides to recover it or
///     it is handled some other way.
///
/// Only the inbox's own header load reads live data; each Allow is a
/// confirm-gated mutation through the shared `MutationButton`.
struct ApprovalsView: View {
    let client: HSCCClient?

    @State private var approvals = LoadState<KanbanBlockedResponse>.idle

    var body: some View {
        NavigationStack {
            Group {
                if let client {
                    content(client)
                } else {
                    notConfiguredView
                }
            }
            .navigationTitle("Approvals")
        }
    }

    private var notConfiguredView: some View {
        VStack(spacing: 12) {
            Image(systemName: "checkmark.seal")
                .font(.system(size: 44))
                .foregroundColor(.secondary)
            Text("Connect to your cluster")
                .font(.headline)
            Text("Set the host, port, and token in Settings to review approvals.")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
        .padding(.horizontal)
    }

    @ViewBuilder
    private func content(_ client: HSCCClient) -> some View {
        switch approvals {
        case .idle:
            ProgressView("Loading…").task { await loadApprovals(client) }
        case .loading:
            ProgressView("Loading…")
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't load approvals", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Try again") { Task { await loadApprovals(client) } }
            }
        case .stale(let response, let ageMessage):
            approvalsList(response, client: client, staleMessage: ageMessage)
        case .loaded(let response):
            approvalsList(response, client: client, staleMessage: nil)
        }
    }

    @ViewBuilder
    private func approvalsList(_ response: KanbanBlockedResponse,
                               client: HSCCClient,
                               staleMessage: String?) -> some View {
        let cards = response.tasks ?? []
        let pending = cards.filter(\.isPendingApproval)
        List {
            if let staleMessage {
                Section {
                    StaleBanner(age: staleMessage, reason: "Can't reach the cluster right now.") {
                        Task { await loadApprovals(client) }
                    }
                }
            }
            Section {
                Label(response.speak, systemImage: "text.bubble")
                    .font(.subheadline)
            }
            if pending.isEmpty {
                Section {
                    Label("No pending approvals.", systemImage: "checkmark.seal.fill")
                        .foregroundColor(Theme.Semantic.ok)
                }
            }
            ForEach(pending) { card in
                approvalRow(card, client: client)
            }
            if let errors = response.errors, !errors.isEmpty {
                Section("Errors") {
                    ForEach(errors, id: \.self) { error in
                        Text(error).font(.caption).foregroundColor(Theme.Semantic.bad)
                    }
                }
            }
        }
        .refreshable { await loadApprovals(client) }
    }

    @ViewBuilder
    private func approvalRow(_ card: BlockedCard, client: HSCCClient) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // The ask, phrased as what's being requested ("worker wants to X").
            Text(card.displayTitle)
                .font(.body.weight(.semibold))
            HStack(spacing: 6) {
                if let assignee = card.assignee, !assignee.isEmpty {
                    Text(assignee).font(.caption).foregroundColor(.secondary)
                }
                if let board = card.board, !board.isEmpty {
                    Text(board).font(.caption).foregroundColor(.secondary)
                }
                if let age = card.age_days {
                    Text("\(age)d").font(.caption).foregroundColor(.secondary)
                }
                Spacer()
                Label(approvalKindLabel(card.block_kind), systemImage: "hand.raised.fill")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.warn)
            }
            // A human-readable why (the reason the worker blocked), when the
            // server recorded one.
            if let why = card.why, !why.isEmpty, why != "kind=\(card.block_kind ?? "")" {
                Text(why).font(.caption).foregroundColor(.secondary)
            }
            // The actual comments carry the worker's request / context.
            if let comments = card.comments, !comments.isEmpty {
                ForEach(comments.prefix(2), id: \.self) { comment in
                    Text(comment)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .padding(8)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
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
                    await loadApprovals(client)
                    return result.message ?? "Allowed \(card.id)."
                }
            )
            .font(.caption)
            .buttonStyle(.borderless)
            .foregroundColor(Theme.Semantic.ok)
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

    private func loadApprovals(_ client: HSCCClient) async {
        approvals = await Offline.load(approvals,
                                       cacheKey: EndpointPath.kanbanBlocked,
                                       client: client) {
            try await client.kanbanBlocked()
        }
    }
}
