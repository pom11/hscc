import Foundation

// ---------------------------------------------------------------------------
// What the `systemLarge` / `systemExtraLarge` monitor surfaces draw, as data.
//
// `ClusterMonitorViewModel` (t_61ed3f5e) owns the CELLS: it reproduces, to the
// character, what `hscc cluster monitor` puts in each one, and the goldens in
// `model_decode_check` hold it there. What that file deliberately does NOT own
// is the LAYOUT — which columns fit at a widget's width, how many rows fit at
// its height, and which fields the extra-large size spends its room on. That
// is this file, and it is why this file exists separately: a layout rule buried
// in a view cannot be executed by a gate, and a view file imports WidgetKit.
//
// TWO RULES THIS FILE KEEPS.
//
//   1. It formats NOTHING that the CLI also formats. Every host and serving
//      cell comes out of `CLIMonitorHostRow` / `CLIMonitorServingRow` — the
//      same instance the parity goldens are compared against. A column here is
//      a SELECTION of the CLI's row, never a re-derivation, so the widget's
//      table and the terminal's table cannot round differently. The harness
//      asserts exactly that: column i's header and cell equal the CLI's
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
// room for medium cannot happen by accident.
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

/// The host table's nine columns, in the CLI's order. `cell` reads the
/// CLI-formatted row, so the value is the CLI's by construction.
enum ClusterMonitorHostColumn: String, CaseIterable, Equatable {
    case host, jobs, cpu, ram, swap, gpu, cpuTemp, gpuTemp, gpuPower

    /// The CLI's own header words, and the abbreviation `.large` uses because
    /// nine words will not fit 320 pt. Only the LABEL shrinks — the cell text
    /// is identical in both sizes, which is what the parity goldens hold.
    func header(compact: Bool) -> String {
        switch self {
        case .host: return "Host"
        case .jobs: return "Jobs"
        case .cpu: return compact ? "CPU" : "CPU%"
        case .ram: return compact ? "RAM" : "RAM%"
        case .swap: return "Swap"
        case .gpu: return compact ? "GPU" : "GPU%"
        case .cpuTemp: return compact ? "CPU°" : "CPU Temp"
        case .gpuTemp: return compact ? "GPU°" : "GPU Temp"
        case .gpuPower: return compact ? "W" : "GPU Power"
        }
    }

    func cell(in row: CLIMonitorHostRow) -> String {
        switch self {
        // NOT `row.host`: the CLI's own cell there is the wire address, and a
        // Home Screen tile that prints a node's address is the §7 violation the
        // view-model exists to prevent (`displayLabel` is the hostname, or the
        // `.NNN` tail). This is the one column where the widget's table and the
        // terminal's table differ on purpose, and `monitor_tables.swift` pins
        // both halves of that difference by name.
        case .host: return row.displayLabel
        case .jobs: return row.jobs
        case .cpu: return row.cpuPct
        case .ram: return row.ramPct
        case .swap: return row.swapPct
        case .gpu: return row.gpuPct
        case .cpuTemp: return row.cpuTemp
        case .gpuTemp: return row.gpuTemp
        case .gpuPower: return row.gpuPower
        }
    }

    /// The column's threshold verdict, or nil when the column has no threshold
    /// anywhere: `host_table()` colours nothing, so these bands are
    /// `ClusterMetricLevel`'s own (see that enum) and only the four columns the
    /// app already bands get painted.
    func level(in row: CLIMonitorHostRow) -> ClusterMetricLevel? {
        switch self {
        case .cpu: return row.cpuLevel
        case .ram: return row.memLevel
        case .gpu: return .level(row.gpuUtilPct, warn: 85, critical: 95)
        case .cpuTemp: return row.cpuTempLevel
        case .gpuTemp: return row.gpuTempLevel
        case .gpuPower: return row.gpuPowerLevel
        case .host, .jobs, .swap: return nil
        }
    }
}

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

/// Everything a table surface needs to decide, laid out: which columns, in
/// which order, how many rows, and whether the XL detail block is drawn.
struct ClusterMonitorTablePlan: Equatable {
    let surface: FleetSurface
    let hostColumns: [ClusterMonitorHostColumn]
    let servingColumns: [ClusterMonitorServingColumn]
    /// Header words: the CLI's own (`.extraLarge`) or the abbreviated set
    /// (`.large`). Cells are unaffected either way.
    let compactHeaders: Bool
    let hostRowLimit: Int
    let servingRowLimit: Int
    let showsHostDetail: Bool
}

enum ClusterMonitorTableLayout {
    /// The plan for a surface, or nil when that surface draws no table.
    static func plan(for surface: FleetSurface) -> ClusterMonitorTablePlan? {
        switch surface {
        case .large:
            // ~320 pt wide, 2 rows tall. Nine host columns fit at 9-10 pt with
            // monospaced digits; the serving table takes the CLI's narrow row
            // MINUS `Gen t/s`, which is aggregate-only on this fleet (see the
            // PR note: every live row sends null, so a column of dashes is not
            // information, and the fleet total already sits in the header
            // detail line). `Routes` and `Prompt t/s` are the CLI's own
            // wide-only columns.
            return ClusterMonitorTablePlan(
                surface: .large,
                hostColumns: ClusterMonitorHostColumn.allCases,
                servingColumns: [.node, .port, .model, .run, .wait, .kvPct, .tokensIn, .tokensOut],
                compactHeaders: true,
                hostRowLimit: 8,
                servingRowLimit: 8,
                showsHostDetail: false)
        case .extraLarge:
            // ~660 pt wide: room for the CLI's own header words, room for the
            // per-host detail block, and room for the serving table to be the
            // right-hand column instead of a second stack.
            return ClusterMonitorTablePlan(
                surface: .extraLarge,
                hostColumns: ClusterMonitorHostColumn.allCases,
                servingColumns: [.node, .port, .model, .run, .wait, .kvPct, .tokensIn, .tokensOut],
                compactHeaders: false,
                hostRowLimit: 8,
                servingRowLimit: 12,
                showsHostDetail: true)
        case .small, .medium, .inline, .circular, .rectangular:
            return nil
        }
    }

    /// The rows a plan draws, clipped to its limit. The clipped count is what
    /// the `overflowLine` reports, so a truncated table says so instead of
    /// reading as "the fleet is four hosts".
    static func visibleHostRows(_ rows: [CLIMonitorHostRow],
                                plan: ClusterMonitorTablePlan) -> [CLIMonitorHostRow] {
        Array(rows.prefix(plan.hostRowLimit))
    }

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
    static func overflowLine(shown: Int, total: Int) -> String? {
        let hidden = total - shown
        guard hidden > 0 else { return nil }
        return "+\(hidden) more"
    }
}

// MARK: - The tables

/// The host table, the serving table and the XL detail, projected from ONE
/// `GET /v1/cluster/monitor` response — no second read, and no number that the
/// route did not send.
///
/// `nil` from `project` means "this response has no host list" (the fleet-down
/// body, or a failed read), which is the same rule `FleetProjection` uses: a
/// surface with no host list draws its failure/stale text rather than an empty
/// table. An EMPTY host list is different and does project — the route sent it,
/// and `hostCount` says 0.
struct ClusterMonitorTables: Equatable {
    let hostCount: Int
    let hostRows: [CLIMonitorHostRow]
    /// Backend rows, errored ones included — the CLI keeps a `down` row rather
    /// than dropping it. Stored separately from the totals for the same reason
    /// `ClusterMonitorViewModel` keeps them separate: the totals come from the
    /// accumulator, not from the printed rows.
    let backendRows: [CLIMonitorServingRow]
    /// Per-model totals, in the CLI's order (sorted by short model name).
    let totalRows: [CLIMonitorServingTotal]
    /// One line per host, in host-table order. Drawn only where the plan says
    /// so, but always projected: clipping it is a layout decision, not a read.
    let details: [ClusterMonitorHostDetail]
    let proxyError: String?
    let servingError: String?
    /// Whether the route sent a `serving` section at all, captured at projection
    /// time from the wire key's presence — the same capture
    /// `ClusterMonitorViewModel.toldServingSection` makes, for the same reason,
    /// and this copy has to make it too: `let rows = response.serving ?? []` in
    /// `project` has already flattened "empty list" and "no key" into one empty
    /// array by the time anybody asks. A clean poll of a route that sent no
    /// `serving` key has NO error and still has nothing to show, so the error is
    /// a different fact and cannot answer this question (t_9e110d88 caught the
    /// conflation in the small tile; the tables block had it independently).
    let toldServingSection: Bool

    /// Backend rows then the per-model `total (n)` rows, in `serving_table`'s
    /// order — the rows the table draws, built the same way
    /// `ClusterMonitorViewModel.servingRows` builds them.
    var servingRows: [CLIMonitorServingRow] {
        backendRows + totalRows.map(CLIMonitorServingRow.init(total:))
    }

    /// Whether the serving section exists at all — `false` means the route sent
    /// no section, which is silence, NOT "0 backends" (§6 rule 1). The view must
    /// not draw an empty serving table for it.
    ///
    /// Reads the wire key's presence captured above, never `servingError`: a
    /// clean poll of a route that sent no `serving` key has no error and nothing
    /// to show. `monitor_tables.swift` pins that case and pins that this answer
    /// equals `ClusterMonitorViewModel`'s for the same payload — the two copies
    /// exist because one file is layout and one is cells, not to disagree.
    var hasServingSection: Bool { toldServingSection }

    /// The CLI's one note line under the serving header, or nil when the CLI
    /// prints none — the same `CLIMonitorNote` the cell view-model and the macOS
    /// dropdown answer from, so the two apps report one sentence for one failure.
    /// `MonitorServingColumn` draws it OUTSIDE the `hasServingSection` test: a
    /// proxy failure is worth printing even when the section itself is absent,
    /// which is precisely the case this file used to leave silent.
    var note: CLIMonitorNote? {
        CLIMonitorNote.servingBlock(proxyError: proxyError, servingError: servingError,
                                    hasServingSection: hasServingSection,
                                    backendRows: backendRows.count)
    }

    static func project(_ response: MonitorResponse?, now: Date = Date()) -> ClusterMonitorTables? {
        guard let response, let hosts = response.json?.hosts, !hosts.isEmpty else {
            return nil
        }
        let rows = response.serving ?? []
        // The parked set comes from the shared index, so this cells-twin of
        // `ClusterMonitorViewModel` renders a backend on a parked machine the
        // same way that view-model does — the two copies exist to split layout
        // from cells, not to disagree about what a parked machine is.
        let parked = FleetProjection.parkedIndex(of: response)
        return ClusterMonitorTables(
            hostCount: hosts.count,
            hostRows: hosts.map(CLIMonitorHostRow.init(node:)),
            backendRows: rows.map { CLIMonitorServingRow(row: $0, parked: parked, now: now) },
            totalRows: CLIMonitorServingTotal.summing(rows: rows),
            details: hosts.map(ClusterMonitorHostDetail.init(host:)),
            proxyError: response.serving_proxy_error,
            servingError: response.serving_error,
            toldServingSection: response.serving != nil)
    }
}
