import SwiftUI

/// Phase B3 — the kanban tab host.
///
/// Switches between the four project/kanban READ views: Standup, Cards,
/// Review, and QA. Every one of them is read-only — the confirm-gated
/// mutating actions land in B4. The segmented picker purely selects which
/// read view is shown; it never mutates anything.
struct KanbanView: View {
    enum Pane: String, CaseIterable, Identifiable {
        case standup, cards, review, qa
        var id: String { rawValue }
        var label: String {
            switch self {
            case .standup: return "Standup"
            case .cards: return "Cards"
            case .review: return "Review"
            case .qa: return "QA"
            }
        }
    }

    @State private var selected: Pane = .standup

    var body: some View {
        VStack(spacing: 0) {
            Picker("Pane", selection: $selected) {
                ForEach(Pane.allCases) { pane in
                    Text(pane.label).tag(pane)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal)
            .padding(.vertical, 8)

            switch selected {
            case .standup: StandupView()
            case .cards: CardsView()
            case .review: ReviewQueueView()
            case .qa: QAQueueView()
            }
        }
    }
}
