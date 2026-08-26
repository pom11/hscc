import SwiftUI

/// Template shape renderer — reuses the node-topology grammar to show what a
/// template WOULD look like before applying it.
///
/// The signature `NodeTopologyView` shows the two live TP pairs with real IP
/// labels. A template is a DESIRED layout with a variable node count and no
/// pinned IPs, so this view reuses the SAME visual grammar (paired dots, role
/// labels beneath, one-line caption) while adapting to the template's own
/// shape: the node count laid out left to right as an orchestrator block
/// followed by the worker families. Same palette, same dots, same caption
/// pattern — deliberately not a second style.
struct TemplateTopologyView: View {
    let template: ClusterTemplate

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 20) {
                ForEach(blocks) { block in
                    blockView(block)
                }
                Spacer(minLength: 0)
            }
            Text(caption)
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

    // MARK: - Shape derivation

    /// The number of nodes this template lays out (parsed from `group`, e.g.
    /// "4node" → 4). Falls back to the live cluster count when unparseable.
    private var nodeCount: Int {
        guard let group = template.group else { return 4 }
        let digits = group.filter(\.isNumber)
        guard digits.count > 0, let n = Int(digits) else { return 4 }
        return n
    }

    /// The worker families in this template, in list order.
    private var families: [String] {
        template.families ?? []
    }

    /// The layout blocks: orchestrator first, then one block per family. The
    /// orchestrator always occupies one node; the remaining nodes form the
    /// serving block(s).
    private var blocks: [Block] {
        var out: [Block] = []
        let serving = max(nodeCount - 1, 0)
        if serving <= 0 {
            // Orchestrator-only (e.g. 1node-orchestrator-only).
            out.append(Block(label: "orchestrator", nodeCount: nodeCount))
        } else {
            out.append(Block(label: "orchestrator", nodeCount: 1))
            if families.isEmpty {
                out.append(Block(label: "serving", nodeCount: serving))
            } else {
                // Split the serving nodes across families, remainder to the
                // last family. This is an APPROXIMATE shape from the list data
                // (the list doesn't pin per-family counts); the preview's
                // change details give the exact split.
                let base = serving / families.count
                var remaining = serving
                for (i, family) in families.enumerated() {
                    let n = i == families.count - 1 ? remaining : base
                    remaining -= base
                    out.append(Block(label: family, nodeCount: n))
                }
            }
        }
        return out
    }

    private var caption: String {
        "\(nodeCount) node\(nodeCount == 1 ? "" : "s") — \(familiesContent)"
    }

    private var familiesContent: String {
        if families.isEmpty {
            return "no worker family"
        }
        let joined = families.joined(separator: " + ")
        return "families: \(joined)"
    }

    private var accessibilitySummary: String {
        blocks.map { "\($0.label): \($0.nodeCount) node\($0.nodeCount == 1 ? "" : "s")" }
            .joined(separator: ", ")
    }

    // MARK: - Rendering (same grammar as NodeTopologyView)

    private func blockView(_ block: Block) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 5) {
                // nodeCount dots joined by links — the same paired-dot grammar
                // as the live topology strip, extended to any node count.
                ForEach(0..<max(block.nodeCount, 1), id: \.self) { i in
                    if i > 0 { nodeLink }
                    nodeDot
                }
            }
            Text(block.label)
                .font(.caption2)
                .foregroundColor(Theme.Semantic.onSurfaceMuted)
        }
    }

    private var nodeDot: some View {
        Circle()
            .fill(Theme.Semantic.ok)   // the DESIRED layout is up/serving
            .frame(width: 8, height: 8)
    }

    private var nodeLink: some View {
        Rectangle()
            .fill(Theme.Palette.mist.opacity(0.5))
            .frame(height: 2)
    }

    /// One rendering block: a label + how many nodes it lays out.
    private struct Block: Identifiable {
        let label: String
        let nodeCount: Int
        var id: String { label }
    }
}
