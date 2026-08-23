import SwiftUI

/// Placeholder tabs that Phase B4 will fill with real feature views.
///
/// B4 — actions (confirm-gated dispatch/merge/stop).
/// B5 — Siri App Intents + spoken summaries.
///
/// (Phase B2 moved the cluster + fleet views into `ClusterView.swift` /
/// `FleetView.swift`, and Phase B3 moved kanban into `KanbanView.swift`, so
/// the only placeholder left here is the actions surface.)

// MARK: - Actions (B4) — not yet a tab; reserved for the actions card.

struct ActionsPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Label("Coming soon", systemImage: "hammer")
                        .font(.headline)
                    Text("Confirm-gated dispatch, merge, and stop actions arrive in Phase B4.")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
            }
            .navigationTitle("Actions")
        }
    }
}
