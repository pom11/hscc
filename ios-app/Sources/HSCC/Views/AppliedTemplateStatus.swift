import SwiftUI

/// The applied-template read — ONE render of `GET /v1/template/status`.
///
/// Spec §5: "Templates `appliedCard` and FleetControl `appliedSection` (two
/// renders of `/v1/template/status`) collapse to one `AppliedTemplateCard`."
///
/// The two renders had drifted into two different screens with two different
/// levels of honesty about the same payload:
///
///   * Templates showed the name in mono plus a summary line built from the
///     STRUCTURED `applied` fields ("applied 3d ago · families: a, b · 4
///     units"), and deliberately did NOT print `speak` — the live server's
///     `speak` for this endpoint is a Python dict repr (debug quoting, the
///     orchestrator node's address), not prose.
///   * Fleet Control printed `speak` first as an italic subheadline — i.e. the
///     dict repr, rendered as if it were a sentence — and only then the
///     structured `LabeledContent` rows.
///
/// So the same status read as clean fields in one destination and as a
/// machine dump in the other. This is the structured render; `speak` is used
/// ONLY as the last resort when there is nothing structured to say at all.
///
/// Rendered as rows for a `Section` of a `List` (the §5 list recipe) — both
/// call sites put it inside their own `Section` and own their own header,
/// refresh action and load call.
struct AppliedTemplateStatus: View {
    let status: LoadState<TemplateStatusResponse>
    /// Re-fetch. Owned by the caller: each screen refreshes through its own
    /// `Offline.load` and its own cache key.
    let retry: () -> Void

    @ViewBuilder
    var body: some View {
        switch status {
        case .idle, .loading:
            HStack(spacing: Theme.Spacing.sm.rawValue) {
                ProgressView()
                Text("Loading…")
                    .font(.subheadline)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        case .failed(let message):
            HSErrorLabel(message: message, retry: retry)
        case .stale(let state, let ageMessage):
            StaleBanner(age: ageMessage, reason: "Can't reach the cluster right now.", retry: retry)
            rows(for: state)
        case .loaded(let state):
            rows(for: state)
        }
    }

    @ViewBuilder
    private func rows(for state: TemplateStatusResponse) -> some View {
        if let applied = state.applied, let name = applied.template, !name.isEmpty {
            Text(name)
                .font(.hsccMono(20, weight: .bold))
                .foregroundColor(Theme.Semantic.onSurface)
            if let at = applied.applied_at, !at.isEmpty {
                LabeledContent("Applied") { Text(relative(at)) }
            }
            if let node = applied.orchestrator_node, !node.isEmpty {
                LabeledContent("Orchestrator") { Text(node) }
            }
            if let fams = applied.families, !fams.isEmpty {
                LabeledContent("Families") { Text(fams.joined(separator: ", ")) }
            }
            if let units = applied.units {
                LabeledContent("Units") { Text(DisplayJSON.render(units)) }
            }
        } else {
            HSEmpty("No template applied",
                    message: state.speak.isEmpty
                        ? "Apply one from the Templates library."
                        : state.speak,
                    systemImage: "checkmark.rectangle.stack")
        }
        // Server-supplied note (Fleet Control had it; Templates did not). Kept
        // in the shared render so the merge drops nothing.
        if let note = state.note, !note.isEmpty {
            Label(note, systemImage: "info.circle")
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// "3d ago" for the applied timestamp; the raw string when it won't parse.
    private func relative(_ iso: String) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil {
            formatter.formatOptions = [.withInternetDateTime]
            date = formatter.date(from: iso)
        }
        guard let date else { return iso }
        return Offline.agePhrase(Date().timeIntervalSince(date))
    }
}
