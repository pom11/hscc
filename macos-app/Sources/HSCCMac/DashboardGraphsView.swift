import SwiftUI

// ---------------------------------------------------------------------------
// The dropdown's "graphs" section (t_be4e1a48): per-node temperature and
// utilisation history, so the menu bar app is not only numbers.
//
// The data is the snapshot's ONE canonical history read — `GET
// /v1/cluster/history` through the shared client (DECISION-t_447cc7fc; the
// retired `/v1/metrics/history` is never read here). Series building, tier
// semantics and every min/max come from the shared chart layer
// (`ios-app/Sources/Shared/ClusterHistorySeries.swift`, t_b6512c2e); this
// file re-derives no series and computes no peak of its own, and a number the
// route did not send renders `—`, never 0 — the standing rule of the file
// that feeds it.
//
// The line marks themselves are the SHARED Swift Charts views the iOS graph
// card (t_f4551339) owns. Embedding them is this section's follow-up slice:
// they arrive by path at the dropdown's content width (360 − 2 ×
// Theme.Spacing.lg) with a low axis-label density, never as a macOS copy.
// Until that file exists the rows draw what the shared layer already
// guarantees per metric: the current value and the window's TRUE peak (the
// extremes over every point, so thinning cannot move them).
// ---------------------------------------------------------------------------

extension DashboardView {
    /// Per-node history, drawn right after the cluster-health section.
    var graphsSection: some View {
        VStack(alignment: .leading, spacing: Theme.Spacing.xs.rawValue) {
            sectionTitle("Graphs")
            if let history = dashboard.snapshot.history {
                // The resolution shown is the ROUTE's own answer about what
                // its points mean ("2s" at 15m, "1m" at 1h…), so a sparse
                // long-window series can never be mistaken for live data.
                Text("\(history.window) · resolution \(history.resolution) · \(history.rows.count) node\(history.rows.count == 1 ? "" : "s")")
                    .font(.caption2.monospacedDigit())
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
                if history.rows.isEmpty {
                    // An empty answer keeps the payload's own reason. A 15m
                    // whose `store.tier0_resident` is false means the 2 s ring
                    // lives in the daemon process — a gap with a reason, not
                    // a quiet fleet.
                    Text(history.reason.flatMap { $0.isEmpty ? nil : $0 } ?? "—")
                        .font(.caption)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    Text("current · peak")
                        .font(.caption2)
                        .foregroundColor(Theme.Semantic.onSurfaceMuted)
                    ForEach(history.rows.prefix(6)) { row in
                        graphRow(row)
                    }
                }
            } else {
                Text("—")
                    .font(.caption)
                    .foregroundColor(Theme.Semantic.onSurfaceMuted)
            }
        }
    }

    private func graphRow(_ row: DashboardGraphRow) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(row.host)
                .font(.caption)
                .foregroundColor(Theme.Semantic.onSurface)
                .lineLimit(1)
            HStack(spacing: Theme.Spacing.md.rawValue) {
                graphValue(row.cpuTempC)
                graphValue(row.gpuTempC)
                graphValue(row.gpuUtilPct)
                graphValue(row.cpuUtilPct)
            }
        }
    }

    private func graphValue(_ series: ClusterHistorySeries?) -> some View {
        Text(graphValueText(series))
            .font(.caption2.monospacedDigit())
            .foregroundColor(Theme.Semantic.onSurface)
    }

    /// One metric as `current·peak` with the metric's own unit, both numbers
    /// read off the shared series' guaranteed values (the last readable point;
    /// `extremes.max`, computed over EVERY point before any thinning). `—`
    /// whenever the node never reported the metric — the recorder's null is
    /// not a zero.
    private func graphValueText(_ series: ClusterHistorySeries?) -> String {
        guard let series, let current = series.readable.last?.value else { return "—" }
        let currentText = String(format: "%.0f", current) + series.metric.unit
        guard let peak = series.extremes?.max, peak != current else { return currentText }
        return currentText + "·" + String(format: "%.0f", peak) + series.metric.unit
    }
}
