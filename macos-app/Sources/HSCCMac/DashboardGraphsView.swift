import SwiftUI

// ---------------------------------------------------------------------------
// The dropdown's "graphs" section (t_be4e1a48): per-node temperature and
// utilisation HISTORY, so the menu bar app is not only numbers.
//
// THE CHARTS ARE THE WIDGET'S. `ClusterGraphMarks` is the shared Swift Charts
// view the iOS Cluster Graph tile draws (t_f4551339), compiled into this target
// BY PATH — `macos-app/scripts/widget_parity_check.sh` refuses a build where the
// app target stops compiling it, because a dropdown that quietly drew numbers
// again would still pass both build gates. This file passes its own width and
// label count into those marks and draws no chart of its own.
//
// The width the marks get IS the width the series were reduced to
// (`DashboardGraphLayout.miniPixelWidth`, one integer, used by the projection in
// `DashboardMonitor` and by the axis here). That is the rule the marks' own file
// states: an axis may not state a time density its buckets do not have. A widget
// measures that width with a `GeometryReader`; a menu bar window has a pinned
// width, so here it is a constant derived from the same numbers that pin it.
//
// The data is the snapshot's ONE canonical history read — `GET /v1/cluster/history`
// through the shared client (DECISION-t_447cc7fc; the retired `/v1/metrics/history`
// is never read here). `ClusterGraphReading` owns series building, the pixel
// reduction, the true extremes, the unit groups, the metric labels and the
// empty-reason vocabulary, so this file re-derives no series, computes no peak,
// invents no unit and renders no zero for a value the route did not send.
// ---------------------------------------------------------------------------

/// The dropdown's chart geometry, in the one place it is decided.
///
/// `DashboardView` pins the panel at `.frame(width: 360)` and pads it by
/// `Theme.Spacing.lg` on both sides, so the content width is known without
/// measuring. Every number below is derived from that width; none of it is a
/// widget's size, and none of it is repeated at a call site.
enum DashboardGraphLayout {
    /// The dropdown's content width: 360 − 2 × the lg padding.
    static let contentWidth: Double = 360 - 2 * Double(Theme.Spacing.lg.rawValue)

    /// The two unit groups (temperature, percent) share one row, so each mini
    /// gets half the row. Half rather than stacked: four nodes × two stacked
    /// charts does not fit a menu bar window, and dropping a unit group would
    /// drop real metrics from the section the card asked for.
    static let gutter: Double = Double(Theme.Spacing.sm.rawValue)
    static let miniWidth: Double = ((contentWidth - gutter) / 2).rounded(.down)

    /// A chart shorter than this is a smudge; taller and the fleet does not fit.
    static let miniHeight: Double = 62

    /// Nodes drawn per refresh. The section says so when it caps, and the
    /// desktop's extra-large graph tile is the surface for a whole fleet (its
    /// panel count is uncapped by design).
    static let nodesShown = 4

    /// The ONE pixel width: what the series are reduced to AND what the axis
    /// labels are counted from. Same integer, so neither can claim the other's
    /// density.
    static let miniPixelWidth = ClusterGraphAxisRule.pixelColumns(measuredWidth: miniWidth)
    static let axisLabelCount = ClusterGraphAxisRule.labelCount(pixelWidth: miniPixelWidth)
}

extension DashboardView {
    /// Per-node history, drawn right after the cluster-health section.
    var graphsSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("Graphs")
            if let history = dashboard.snapshot.history {
                // The step shown is the ROUTE's own statement of what its points
                // mean, put into words by the shared caption (`2` → "2 s",
                // `300` → "5 m"), so a sparse long-window series can never be
                // mistaken for 2-second data. The window is the payload's own
                // string, not the one this app asked for.
                Text("\(history.window) · every \(ClusterGraphResolutionCaption.text(resolutionS: history.resolutionS)) · \(history.nodes.count) node\(history.nodes.count == 1 ? "" : "s")")
                    .font(.caption2.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                if history.nodes.isEmpty {
                    // An empty answer keeps a reason. `store.tier0_resident`
                    // false at 15m means the 2 s ring lives in the daemon
                    // process — a gap with a cause, not a quiet fleet.
                    Text(verbatim: history.emptyLine)
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ForEach(history.nodes.prefix(DashboardGraphLayout.nodesShown)) { node in
                        graphRow(node, window: history.axisWindow)
                    }
                    if history.nodes.count > DashboardGraphLayout.nodesShown {
                        // Capping the dropdown is a layout fact, so the section
                        // states it and names where the rest lives.
                        Text("+\(history.nodes.count - DashboardGraphLayout.nodesShown) more nodes in the Cluster Graph tile")
                            .font(.caption2)
                            .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    }
                }
            } else {
                // Nothing has been read yet this session: the dash, never a 0.
                Text("—")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
        // The marks read fonts, ink and accent out of the environment, exactly as
        // the widget does, so the operator's stored style for this surface drives
        // the dropdown too — one appearance setting, two surfaces.
        .widgetStyle(WidgetStyle.resolved(for: .widgetClusterGraph))
    }

    private func graphRow(_ node: DashboardGraphNode, window: ClusterGraphWindow) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(verbatim: node.panel.displayLabel)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
            if node.panel.hasAnyValue {
                HStack(alignment: .top, spacing: DashboardGraphLayout.gutter) {
                    ForEach(node.minis) { mini in
                        graphMini(mini, window: window)
                    }
                }
            } else {
                // A node whose every metric came back null gets the dash in the
                // chart's place, not a flat line at zero and not an empty box
                // that reads as a bug.
                Text("— no samples in \(dashboard.snapshot.history?.window ?? "")")
                    .font(.caption2)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    .frame(height: DashboardGraphLayout.miniHeight, alignment: .topLeading)
            }
        }
    }

    private func graphMini(_ mini: ClusterGraphMini, window: ClusterGraphWindow) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ClusterGraphMarks(mini: mini,
                              pixelWidth: DashboardGraphLayout.miniPixelWidth,
                              axisLabelCount: DashboardGraphLayout.axisLabelCount,
                              window: window)
                // A fixed frame, so the drawn width is the width the reduction
                // was asked for rather than whatever the row handed out.
                .frame(width: DashboardGraphLayout.miniWidth,
                       height: DashboardGraphLayout.miniHeight)
            // The numbers the chart cannot say out loud: each line's current and
            // the window's TRUE peak, both formatted by the shared caption (its
            // decimal places are the CLI's own column precisions, so the dropdown
            // and `hscc cluster monitor` cannot print two numbers for one reading).
            Text(verbatim: nodeCaption(mini.lines))
                .font(.caption2.monospacedDigit())
                .foregroundColor(Theme.Semantic.onSurface)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(width: DashboardGraphLayout.miniWidth)
    }

    /// One metric as `label current·peak`, its unit coming from the metric and
    /// not from this file. The peak is the reduction's own `maximum` — the
    /// extreme over EVERY point before thinning — so a chart that dropped a
    /// column cannot move the number beside it.
    private func nodeCaption(_ lines: [ClusterGraphLine]) -> String {
        lines.map { line in
            "\(line.metric.label) \(ClusterGraphCaption.value(line.current, metric: line.metric))·\(ClusterGraphCaption.value(line.maximum, metric: line.metric))"
        }.joined(separator: "  ")
    }
}
