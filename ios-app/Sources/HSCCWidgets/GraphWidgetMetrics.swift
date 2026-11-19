import WidgetKit

// ---------------------------------------------------------------------------
// The graph widget's ONE WidgetKit dependency: which Home Screen size draws
// which of the three layouts the card specifies.
//
// This file exists so `Sources/Shared/ClusterGraphReading.swift` can stay free
// of WidgetKit. `WidgetFamily` is a WidgetKit type, so the family → density map
// cannot live in Shared and still be compiled by a macOS `swiftc` harness — and
// it is written as a POSITIVE LIST (`case .systemSmall: return .sparkline`, …,
// `default: return nil`) so a harness on macOS can execute the true side
// (`cluster_graph_check.sh` asserts each of the four families maps to the density
// the card names) while the false side — "no accessory family, nothing else is
// supported" — is pinned by a structural grep on this file's own list rather than
// by a branch that can never be run.
//
// The accessory families are deliberately absent. The card asks for
// small/medium/large/extraLarge only, `widget_view_dispatch` gates this surface as
// `home-screen-only` (which asserts the INVERSE: no `.accessory*` in
// `supportedFamilies` and no `shortGateLabel` in the widget file), and a Lock
// Screen complication of a chart would be either a picture with no numbers or a
// number with no chart. Adding an accessory family later means switching that gate
// call to `accessory` mode and giving the entry a label rule, not just adding a
// case here.
// ---------------------------------------------------------------------------

extension WidgetFamily {
    /// The layout this size draws, or nil for a size this widget does not offer.
    ///
    /// The mapping is the card's, in the card's words: small is a sparkline with
    /// current/min/max; medium is one node's temps plus GPU% with their current
    /// values; large and extra-large are small multiples, one panel per node.
    var clusterGraphDensity: ClusterGraphDensity? {
        switch self {
        case .systemSmall: return .sparkline
        case .systemMedium: return .nodeOverlay
        case .systemLarge, .systemExtraLarge: return .smallMultiples
        default: return nil
        }
    }

    /// Every size this widget declares, in the order the gallery shows them.
    ///
    /// A positive list on purpose: `supportedFamilies` and the dispatch switch can
    /// drift apart silently when the switch has a `default:` (measured on
    /// t_b3ae3809, where a declared `systemLarge` would have drawn the medium
    /// layout and an undeclared one was invisible), so this list is what
    /// `cluster_graph_check.sh` iterates and requires an arm for, by name.
    static let clusterGraphFamilies: [WidgetFamily] = [
        .systemSmall, .systemMedium, .systemLarge, .systemExtraLarge
    ]

    /// Whether this size is offered at all — the same list, read the other way.
    var supportsClusterGraph: Bool { Self.clusterGraphFamilies.contains(self) }

    /// The width to reduce to when the view has measured NOTHING yet.
    ///
    /// A widget is laid out before its `GeometryReader` reports, and a reduction
    /// asked for 0 columns would flatten a real 2000-point series into two points
    /// — a chart that lies in exactly the way this widget's parent layer exists to
    /// prevent. So the unmeasured case gets a conservative lower bound per size,
    /// never zero, and the view replaces it with the measured width the moment one
    /// exists. This is a floor for a transient, not the width the chart draws at.
    var clusterGraphUnmeasuredPixelWidth: Int {
        switch self {
        case .systemSmall: return 140
        case .systemMedium: return 300
        case .systemLarge: return 320
        case .systemExtraLarge: return 360
        default: return 140
        }
    }
}

extension ClusterGraphDensity {
    /// The sizes that draw this density — the same mapping, asked from the other
    /// side, so the harness can assert "small multiples are drawn by large AND
    /// extra-large" without restating the family names a second time.
    func families(in families: [WidgetFamily] = WidgetFamily.clusterGraphFamilies) -> [WidgetFamily] {
        families.filter { $0.clusterGraphDensity == self }
    }

    // `panelLimit` deliberately lives with `ClusterGraphDensity` in
    // `Sources/Shared/ClusterGraphReading.swift`: it is a rule about how much of
    // the fleet a chart may show, and the macOS harness has to be able to assert
    // it. Only the layout numbers belong on the WidgetKit side.

    /// The mini-charts inside one panel, per unit group. `large` is the tighter
    /// canvas, so it draws each mini shorter rather than dropping a unit group —
    /// dropping a group would drop real metrics from the panel the card listed.
    var miniHeight: CGFloat {
        switch self {
        case .sparkline, .nodeOverlay: return 0
        case .smallMultiples: return 46
        }
    }
}
