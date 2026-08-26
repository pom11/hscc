import SwiftUI

/// The signature element — the node topology strip.
///
/// A compact persistent row showing the cluster's four serving nodes as their
/// two tensor-parallel (TP) pairs. It encodes something TRUE and specific
/// about this cluster: it is two TP pairs, and only the HEAD of each pair
/// serves HTTP — the exact fact that makes a naive health check report a
/// healthy fleet as broken (it probes the wrong node). The paired dots make
/// the topology legible at a glance.
///
///     [ .244 ●──● .246 ]  orchestrator      [ .247 ●──● .248 ]  worker
///
/// This is the ONE place the design spends visual boldness — every other
/// surface stays quiet. No gradients, no numbered eyebrows, no idle animation.
struct NodeTopologyView: View {
    /// The two serving pairs, each carrying its two nodes + a role label.
    let pairs: [TopologyPair]

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // The pairs laid out side by side.
            HStack(spacing: 20) {
                ForEach(pairs) { pair in
                    pairBlock(pair)
                }
                Spacer(minLength: 0)
            }
            // The one-line caption below the strip names the fact that matters.
            Text("Two TP pairs — only each pair's head serves HTTP.")
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
        .padding(.horizontal)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Theme.Semantic.surfaceRaised)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
    }

    /// One TP pair: two nodes joined by a link, with a role label beneath.
    private func pairBlock(_ pair: TopologyPair) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                nodeDot(pair.nodes[0])
                pairLink
                nodeDot(pair.nodes[1])
            }
            Text(pair.role)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    /// A single node: a coloured dot with its ip label beside it.
    private func nodeDot(_ node: TopologyNode) -> some View {
        HStack(spacing: 4) {
            Circle()
                .fill(node.state.color)
                .frame(width: 8, height: 8)
            Text(node.label)
                .font(.hsccMono(12, weight: .semibold))
                .foregroundColor(Theme.Semantic.onSurface)
        }
        .accessibilityLabel("\\(node.label), \\(node.state.rawValue)")
    }

    /// The bond between the pair's two nodes.
    private var pairLink: some View {
        Rectangle()
            .fill(Theme.Palette.mist.opacity(0.5))
            .frame(height: 2)
    }

    private var accessibilitySummary: String {
        pairs.map { pair in
            "\\(pair.role): \\(pair.nodes[0].label) paired with \\(pair.nodes[1].label)"
        }.joined(separator: ", ")
    }
}

/// One serving pair in the topology strip — two nodes + a role label.
struct TopologyPair: Identifiable {
    /// A stable identity for the pair (the joining node labels).
    var id: String { "\(nodes[0].label)-\(nodes[1].label)" }
    let nodes: [TopologyNode]       // exactly 2
    let role: String                // e.g. "orchestrator" / "worker"
}

/// One node in the topology strip: its displayed label (the short ip tail) and
/// its live state.
struct TopologyNode: Identifiable {
    let label: String               // e.g. ".244"
    let state: NodeState

    var id: String { label }

    /// The live serving state of a node, driving its dot colour.
    enum NodeState: String {
        /// Serving / healthy.
        case up
        /// Busy / waking / in transition.
        case busy
        /// Warning / degraded.
        case warn
        /// Down / faulted.
        case down
        /// No live signal yet.
        case unknown

        var color: Color {
            switch self {
            case .up: return Theme.Semantic.ok
            case .busy: return Theme.Semantic.warn
            case .warn: return Theme.Semantic.warn
            case .down: return Theme.Semantic.bad
            case .unknown: return Theme.Semantic.neutral
            }
        }
    }
}
