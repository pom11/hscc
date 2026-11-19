import Foundation

// ---------------------------------------------------------------------------
// What the `systemLarge` / `systemExtraLarge` monitor surfaces add to the
// table `systemMedium` already draws.
//
// THE SPLIT, and why it is this way and not the other way. Three files touch
// this fleet's numbers and each owns exactly one thing:
//
//   * `ClusterMonitorViewModel.swift` (t_61ed3f5e) owns the CELLS — every
//     string `hscc cluster monitor` prints, host table and serving table, and
//     the goldens in `model_decode_check` hold it there.
//   * `CLIMonitorTableReading.swift` (t_2d1c12dc) owns the HOST TABLE as drawn:
//     its nine columns, their titles, their alignment, the row cap and the
//     `+n`. `CLIMonitorHostTableView.swift` is the only thing that draws them,
//     at every size — large and extra-large pass a taller `rowLimit`, they do
//     not grow a second host table.
//   * THIS FILE owns only what those two do not have: the serving table's
//     column SELECTION (main's reading layer has no serving table at all) and
//     the extra-large DETAIL FIELDS, which the CLI has no column for.
//
// It exists separately from the view files for one reason: a layout rule buried
// in a view cannot be executed by a gate, and a view file imports WidgetKit.
//
// TWO RULES THIS FILE KEEPS.
//
//   1. It formats NOTHING that the CLI also formats. The serving rows it draws
//      ARE main's `CLIMonitorServingRow` / `CLIMonitorServingTotal`, reached
//      through `ClusterMonitorViewModel.servingRows` rather than re-projected
//      here — a column is a SELECTION of the CLI's row, never a copy of it, so
//      the widget's table and the terminal's table cannot round differently.
//      The harness asserts that: column i's header and cell equal the CLI's
//      `wideColumns[i]` / `cells(wide: true)[i]`.
//   2. The fields ONLY this file prints (VRAM, GPU clock, net, fan, load) are
//      NOT the CLI's — `host_table()` has nine columns and none of them is
//      VRAM. Their units are declared below as THIS FILE's rule and are
//      asserted by fixture, not by golden. Nothing in here may claim CLI
//      authority for them, and nothing in the CLI parity goldens covers them.
//
// The unknown is a dash (`FleetFormat.dash`), never `0` and never blank: the
// route sends `""` for a metric a node could not read, and the live capture in
// `v1_cluster_monitor_live.json` proves it — every GB10 host there has an
// EMPTY `gpu_mem_used_mb`, `gpu_fan_pct` and `gpu_power_limit_w`, so VRAM and
// fan are dashes on real data and the dash is the honest rendering.
//
// SURFACES: `.large` and `.extraLarge` only. Every other surface answers nil
// ("no table for you") rather than getting a silently shrunk table; a surface
// that later wants one adds its own plan by name, so shrinking large to make
// room for medium cannot happen by accident. Whether a table may be ON SCREEN
// at all is not this file's decision either — that is `entry.cliHostRows`, and
// `entry.tables` is built on it (one definition per entry).
// ---------------------------------------------------------------------------

// MARK: - The XL-only per-host detail

/// One field of the extra-large detail line: a label, a value, and whether the
/// value has a threshold at all.
struct ClusterMonitorField: Equatable {
    let label: String
    let value: String
    /// nil = this field has NO threshold anywhere in the app, so it must never
    /// be painted with a status colour. `.unknown` is different: it means the
    /// route did not tell us the number, and the value below is a dash.
    let level: ClusterMetricLevel?
}

/// The extra-large block's per-host line: the fields `host_table()` does not
/// have columns for, which is exactly what XL's extra width buys.
///
/// Order is the order the block prints: VRAM, GPU clock, net, uptime, fan,
/// load. `fields` is the single owner of that order — the view iterates it and
/// composes nothing.
struct ClusterMonitorHostDetail: Equatable, Identifiable {
    /// The wire host, for identity only. NEVER drawn — §7 forbids an address
    /// on a surface; `label` is what appears.
    let id: String
    let label: String
    let vram: String
    let clock: String
    let net: String
    let uptime: String
    let fan: String
    let load: String
    let vramLevel: ClusterMetricLevel

    var fields: [ClusterMonitorField] {
        [ClusterMonitorField(label: "VRAM", value: vram, level: vramLevel),
         ClusterMonitorField(label: "GPU clock", value: clock, level: nil),
         ClusterMonitorField(label: "Net", value: net, level: nil),
         ClusterMonitorField(label: "Up", value: uptime, level: nil),
         ClusterMonitorField(label: "Fan", value: fan, level: nil),
         ClusterMonitorField(label: "Load", value: load, level: nil)]
    }

    /// The same line for VoiceOver, which cannot read a run-coloured `Text`
    /// into words worth hearing. Built from the already-formatted strings, so
    /// it is the same prose the eyes get.
    var accessibilityLine: String {
        ([label] + fields.map { "\($0.label) \($0.value)" }).joined(separator: ", ")
    }

    init(host: MonitorNode) {
        let sample = host.sample
        id = host.host
        label = host.displayLabel

        // VRAM used/total. The route reports MB as a string; GB at one decimal
        // is this file's unit (1024-based, so a 65536 MB part reads 64.0G — the
        // number a GPU spec sheet prints). Both sides are required: `57344/-`
        // is not a fraction anybody can read, so either side missing is a dash.
        if let used = sample?.gpuMemUsedMB, let total = sample?.gpuMemTotalMB, total > 0 {
            vram = String(format: "%.1f/%.1fG", used / 1024, total / 1024)
        } else {
            vram = FleetFormat.dash
        }
        // The used PERCENTAGE is what the threshold reads — the route sends it
        // directly (`gpu_mem_used_pct`) rather than this file dividing, which is
        // the same reason the CLI's Swap column is computed from its own parts.
        vramLevel = ClusterMetricLevel.level(sample?.gpuMemUsedPct, warn: 85, critical: 95)

        clock = sample?.gpuClockMHz.map { String(format: "%.0f MHz", $0) } ?? FleetFormat.dash

        // Net recv/send per second, SI steps (1000), one decimal above bytes.
        // The route counts BYTES PER SECOND off a counter, so 1024-based units
        // would print `999 B/s` next to `1.0 KB/s` for a one-byte difference
        // and read as two different quantities.
        net = "↓" + ClusterMonitorHostDetail.rate(sample?.netRecvBytesS)
            + " ↑" + ClusterMonitorHostDetail.rate(sample?.netSendBytesS)

        // `MonitorSample.uptimeText` is the app's ONE uptime rule ("2d 11h"),
        // already used by the daemon-host block; re-deriving it here would put
        // a second rounding on the same seconds.
        uptime = sample?.uptimeText ?? FleetFormat.dash

        fan = sample?.gpuFanPct.map { String(format: "%.0f%%", $0) } ?? FleetFormat.dash

        // Load 1/5/15 at one decimal, in the kernel's order. A missing member
        // keeps its slot as a dash: `1.4 — 1.6` says which one is unreadable,
        // while dropping it would make the 15-minute number look like the 5.
        let loads = [sample?.cpuLoad1m, sample?.cpuLoad5m, sample?.cpuLoad15m]
        load = loads.allSatisfy { $0 == nil }
            ? FleetFormat.dash
            : loads.map { $0.map { String(format: "%.1f", $0) } ?? FleetFormat.dash }
                  .joined(separator: " ")
    }

    /// The bytes/s → human rate rule described above. `nil` is "not told",
    /// which is a dash, not `0 B/s` (§6 rule 1).
    static func rate(_ bytesPerSecond: Double?) -> String {
        guard let value = bytesPerSecond, value >= 0 else { return FleetFormat.dash }
        if value < 1_000 { return String(format: "%.0f B/s", value) }
        if value < 1_000_000 { return String(format: "%.1f KB/s", value / 1_000) }
        if value < 1_000_000_000 { return String(format: "%.1f MB/s", value / 1_000_000) }
        return String(format: "%.1f GB/s", value / 1_000_000_000)
    }
}

// MARK: - Columns

// The HOST table's columns are NOT here. They are main's:
// `CLIMonitorTableReading.drawnColumns` (Shared/CLIMonitorTableReading.swift)
// pairs each of `host_table()`'s nine titles with its alignment and its
// verdict, and `CLIMonitorHostTableView` draws them at every size — large and
// extra-large pass a taller `rowLimit` rather than growing a second host table
// with a second set of header words. An earlier revision of this file carried
// its own nine-column enum with abbreviated `.large` header words ("CPU°", "W");
// it duplicated titles the reading layer already owns and drifted from them, so
// it was deleted rather than reconciled.

/// The serving table's eleven columns — the same eleven, in the same order, as
/// `CLIMonitorServingRow.wideColumns`. A plan selects a subset; nothing here
/// renames or reformats a cell.
enum ClusterMonitorServingColumn: String, CaseIterable, Equatable {
    case node, port, model, routes, run, wait, kvPct, tokensIn, tokensOut, genTps, promptTps

    /// The CLI's header word, verbatim. The harness asserts
    /// `header == CLIMonitorServingRow.wideColumns[index]`.
    var header: String {
        switch self {
        case .node: return "Node"
        case .port: return "Port"
        case .model: return "Model"
        case .routes: return "Routes"
        case .run: return "Run"
        case .wait: return "Wait"
        case .kvPct: return "KV%"
        case .tokensIn: return "In"
        case .tokensOut: return "Out"
        case .genTps: return "Gen t/s"
        case .promptTps: return "Prompt t/s"
        }
    }

    func cell(in row: CLIMonitorServingRow) -> String {
        row.cells(wide: true)[indexInWideRow]
    }

    /// The column's position in the CLI's wide row, so `cell` cannot silently
    /// read the wrong field.
    var indexInWideRow: Int {
        ClusterMonitorServingColumn.allCases.firstIndex(of: self) ?? 0
    }

    /// Only `Run` carries a colour rule, and it is the CLI's own `_load_style`
    /// (mirrored by the view-model) rather than a threshold invented here.
    func level(in row: CLIMonitorServingRow) -> ClusterMetricLevel? {
        self == .run ? row.loadLevel : nil
    }
}

// MARK: - The plan

/// Everything a large / extra-large surface needs to decide, laid out: which
/// serving columns, in which order, how many rows of each block, and whether
/// the XL detail block is drawn.
///
/// There is deliberately NO host-column field here. The host half of these two
/// surfaces is main's `CLIMonitorHostTableView`, which reads its own columns
/// from `CLIMonitorTableReading.drawnColumns`; all this plan tells it is how
/// many rows the surface has room for (`hostRowLimit`), which is the one
/// parameter `CLIMonitorHostTableView` already takes for taller surfaces.
struct ClusterMonitorTablePlan: Equatable {
    let surface: FleetSurface
    let servingColumns: [ClusterMonitorServingColumn]
    /// The SERVING table's compact rendering: the abbreviated caption and the
    /// smaller type, for the ~320 pt tile. It says nothing about the host
    /// table's header words, which are the CLI's at every size (the reading
    /// layer has no abbreviated set, and this card's former one was deleted).
    let compactHeaders: Bool
    /// Host rows this surface gives room to. Passed to `CLIMonitorHostTableView`
    /// as its `rowLimit`; the tile's own `+n` comes from there, not from here.
    let hostRowLimit: Int
    let servingRowLimit: Int
    let showsHostDetail: Bool
}

enum ClusterMonitorTableLayout {
    /// The plan for a surface, or nil when that surface draws no table.
    static func plan(for surface: FleetSurface) -> ClusterMonitorTablePlan? {
        switch surface {
        case .large:
            // ~320 pt wide, 2 rows tall. The host table is main's nine columns
            // at its own sizes; this is their row budget. The serving table
            // takes the CLI's narrow row MINUS `Gen t/s`, which is
            // aggregate-only on this fleet (see the PR note: every live row
            // sends null, so a column of dashes is not information, and the
            // fleet total already sits in the header detail line). `Routes` and
            // `Prompt t/s` are the CLI's own wide-only columns.
            return ClusterMonitorTablePlan(
                surface: .large,
                servingColumns: [.node, .port, .model, .run, .wait, .kvPct, .tokensIn, .tokensOut],
                compactHeaders: true,
                hostRowLimit: 8,
                servingRowLimit: 8,
                showsHostDetail: false)
        case .extraLarge:
            // ~660 pt wide: room for the per-host detail block, and room for
            // the serving table to be the right-hand column instead of a second
            // stack.
            return ClusterMonitorTablePlan(
                surface: .extraLarge,
                servingColumns: [.node, .port, .model, .run, .wait, .kvPct, .tokensIn, .tokensOut],
                compactHeaders: false,
                hostRowLimit: 8,
                servingRowLimit: 12,
                showsHostDetail: true)
        case .small, .medium, .inline, .circular, .rectangular:
            return nil
        }
    }

    /// The serving rows a plan draws, clipped to its limit. The host rows are
    /// NOT clipped here: `CLIMonitorTableReading.visibleRows` does that, and a
    /// second clip function for the same rows is how the two halves of a table
    /// start disagreeing about the `+n`.
    static func visibleServingRows(_ rows: [CLIMonitorServingRow],
                                   plan: ClusterMonitorTablePlan) -> [CLIMonitorServingRow] {
        Array(rows.prefix(plan.servingRowLimit))
    }

    /// One detail line per host, clipped like the tables: the XL block shows at
    /// most the plan's host rows, and the clip is a layout rule in this file —
    /// a view that clipped too would be a second owner of it.
    static func visibleDetails(_ details: [ClusterMonitorHostDetail],
                               plan: ClusterMonitorTablePlan) -> [ClusterMonitorHostDetail] {
        Array(details.prefix(plan.hostRowLimit))
    }

    /// `+N more` for a clipped block, or nil when nothing was clipped. `total`
    /// is a count of rows the ROUTE sent — never an estimate, never a zero.
    /// Only the SERVING table and the detail block use this: the host table's
    /// `+n` is `CLIMonitorHostTableView`'s own, computed from the same list it
    /// drew so the two cannot disagree.
    static func overflowLine(shown: Int, total: Int) -> String? {
        let hidden = total - shown
        guard hidden > 0 else { return nil }
        return "+\(hidden) more"
    }
}

// MARK: - The tables

/// The two things a large / extra-large monitor surface draws BESIDES main's
/// host table: the serving table and the extra-large detail block.
///
/// Projected from the SAME `GET /v1/cluster/monitor` response the widgets
/// already fetch, and it re-projects NOTHING: the serving rows are main's
/// `ClusterMonitorViewModel.servingRows` (`CLIMonitorServingRow` built from
/// `CLIMonitorServingTotal`), passed through rather than rebuilt, so there is
/// one serving projection in the app. `details` is the only new projection,
/// because no CLI column exists for those six fields.
///
/// `nil` from `project` means "this response has no host list" (the fleet-down
/// body, or a failed read), which is the same rule `FleetProjection` uses: a
/// surface with no host list draws its failure/stale text rather than an empty
/// table. An EMPTY host list is different and does project — the route sent it.
///
/// This struct is data, not a gate. Whether the surface may show numbers at all
/// is `entry.cliHostRows` (per entry, one definition), and `entry.tables` is
/// that predicate applied to this projection.
struct ClusterMonitorTables: Equatable {
    /// Backend rows then the per-model `total (n)` rows, in `serving_table`'s
    /// order — main's rows, read off main's view-model.
    let servingRows: [CLIMonitorServingRow]
    /// Whether the serving section exists at all — `false` means the route sent
    /// no section, which is silence, NOT "0 backends" (§6 rule 1). The view
    /// must not draw an empty serving table for it.
    ///
    /// This is `ClusterMonitorViewModel.toldServingSection`, which captured the
    /// wire key's presence at projection time. Reading `servingError == nil`
    /// instead — as an earlier revision of this file did — conflates "the poll
    /// failed" with "the route sent no serving key", the exact bug t_9e110d88
    /// caught in the small tile.
    let hasServingSection: Bool
    /// One line per host, in host-table order. Drawn only where the plan says
    /// so, but always projected: clipping it is a layout decision, not a read.
    let details: [ClusterMonitorHostDetail]

    static func project(_ response: MonitorResponse?) -> ClusterMonitorTables? {
        // The host-list rule is main's projection's rule, taken from the same
        // response rather than re-decided here.
        guard let model = ClusterMonitorViewModel.project(response),
              let hosts = response?.json?.hosts, !hosts.isEmpty else {
            return nil
        }
        return ClusterMonitorTables(servingRows: model.servingRows,
                                    hasServingSection: model.hasServingSection,
                                    details: hosts.map(ClusterMonitorHostDetail.init(host:)))
    }
}
