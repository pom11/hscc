import SwiftUI

/// Placeholder tabs that Phase B2 / B3 / B4 will fill with real feature views.
///
/// B2 — cluster + fleet views.
/// B3 — kanban views.
/// B4 — actions (confirm-gated dispatch/merge/stop).
/// B5 — Siri App Intents + spoken summaries.
///
/// These are intentionally minimal: skeleton + settings only (Phase B1). Each
/// shows the `speak`-style placeholder and a note about what lands there.

// MARK: - Cluster (B2)

struct ClusterPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    comingSoonLabel
                    Text("Cluster status, hosts, health, and monitor views arrive in Phase B2.")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
            }
            .navigationTitle("Cluster")
        }
    }

    private var comingSoonLabel: some View {
        Label("Coming soon", systemImage: "hammer")
            .font(.headline)
    }
}

// MARK: - Kanban (B3)

struct KanbanPlaceholderView: View {
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Label("Coming soon", systemImage: "hammer")
                        .font(.headline)
                    Text("Kanban cards, standup, and review queues arrive in Phase B3.")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
            }
            .navigationTitle("Kanban")
        }
    }
}

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
