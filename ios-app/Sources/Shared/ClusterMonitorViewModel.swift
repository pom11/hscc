import Foundation

// ---------------------------------------------------------------------------
// The `hscc cluster monitor` view-model — every column the CLI prints, ONE
// implementation, formatted exactly as the CLI formats it.
//
// The CLI's human view lives in `hscc_daemon/cluster_live.py`: `host_table()`
// prints one row per host (Host, Jobs, CPU%, RAM%, Swap%, GPU%, CPU Temp,
// GPU Temp, GPU Power) and `serving_table()` prints one row per (backend,
// model) plus a `total (n)` row per model. GET /v1/cluster/monitor returns
// exactly the two objects those two functions consume — `json` (the sparkrun
// frame, `handle_cluster_monitor` at hscc-api/routes_cluster.py:496) and
// `serving` (one `ServingPoller.poll()` sample, routes_cluster.py:509) — so a
// widget that shows the same numbers has to reproduce that formatting. Doing
// it per surface is how one card ends up printing `87°C` where the CLI prints
// `87.2 C`, and another prints `71.0 C` for a value the CLI rounds to `71 C`.
//
// So the formatting lives here and nowhere else. Every rule below names the
// CLI function it mirrors, and `model_decode_check` runs THIS code against
// goldens generated from THAT code on the same fixture bytes
// (`scripts/cluster_monitor_cli_parity.py`).
//
// THREE THINGS THIS FILE DOES NOT DO.
//
//   1. It does not use `°`. The CLI's unit is " C" — a space and a `C`
//      (`_cell(..., "{:.1f}", " C")`) — and that is what the parity contract
//      holds. `FleetFormat.celsius` in `ClusterNodeTally.swift` renders `°C`
//      for the existing glance surfaces: that is the app's own style, it is
//      NOT the CLI's, and nothing here should be "fixed" to agree with it.
//   2. It does not invent rounding. The CLI formats with Python's
//      `"{:.Nf}".format(float(v))`; this file uses `String(format: "%.Nf", d)`.
//      Both are correctly-rounded (ties-to-even) conversions of the same
//      binary double, measured identical on 48 values including the ties
//      (`1.5`→`2`, `2.5`→`2`, `3.5`→`4`, `-1.5`→`-2`, `99.95`→`100`) and the
//      binary-representation traps (`37.885`→`37.9`, `3.15`→`3.1`,
//      `0.15981224854716136`→`0.2`). Do not "improve" this with a decimal
//      type: that is exactly where a 0.1-unit drift would come from.
//   3. It is Foundation-only, on purpose: no SwiftUI, no WidgetKit, no
//      ActivityKit, no `Color`. That is what lets `model_decode_check.sh`
//      compile and RUN the same bytes the shipped surfaces run. Colour comes
//      back as `ClusterMetricLevel`, which the views map onto `Theme`; a raw
//      colour in this file would break that harness and redden
//      `check_theme.sh` in the same commit.
// ---------------------------------------------------------------------------

// MARK: - The CLI's cell primitives (`cluster_live.py`)

/// The primitives `cluster_live.py` uses to turn a payload value into a table
/// cell. Nothing else in the app should re-derive them.
enum CLIMonitorFormat {
    /// `_cell` and `fmt_tokens` render an unknown value as this.
    static let dash = "-"

    /// `cluster_live.WIDE_MIN`: below this terminal width `serving_table`
    /// drops the `Routes` and `Prompt t/s` columns.
    static let wideMin = 110

    /// `_cell(v, fmt, suffix)` for a value that arrives as a STRING — which is
    /// how every sparkrun metric arrives.
    ///
    /// `nil` and the EMPTY STRING both become `-`: the frame sends `""` for a
    /// metric a node could not read and the CLI treats that as unreadable, not
    /// as zero (§6 rule 1). A value that neither is parses as a number falls
    /// into `_cell`'s `except` branch, which prints the data verbatim
    /// (`esc(v)` — rich markup escaping, invisible in plain text), so this
    /// returns it unchanged too.
    ///
    /// `places` is the `{:.Nf}` precision of the call site: the host table
    /// uses 0 (Jobs, GPU%), 1 (CPU%, RAM%, CPU Temp) and 2 (GPU Power); the
    /// serving table 1 (KV%, Gen t/s) and 0 (Prompt t/s).
    static func cell(_ raw: String?, places: Int, suffix: String = "") -> String {
        guard let raw, !raw.isEmpty else { return dash }
        guard let value = Double(raw) else { return raw }
        return String(format: "%.\(places)f", value) + suffix
    }

    /// `_cell(v, fmt, suffix)` for a value that is already a number on the
    /// wire (the serving rows: `kv`, `gen_tps`, `prompt_tps`). `nil` → `-`.
    static func cell(_ value: Double?, places: Int, suffix: String = "") -> String {
        guard let value else { return dash }
        return String(format: "%.\(places)f", value) + suffix
    }

    /// `_swap_pct(sample)` — the one host column the CLI computes rather than
    /// prints, and the only host cell that carries its own unit:
    /// `f"{100 * used / total:.0f}%"`.
    ///
    /// The `or 0` in the CLI is load-bearing twice: an absent/empty `total` is
    /// zero total, and a zero total prints `-` (there is no swap to divide
    /// by); an absent/empty `used` is zero USED, so a host with swap and no
    /// swap in use prints `0%`, not `-`. An unparseable value on either side
    /// falls into the `except` and prints `-`.
    static func swapPct(usedRaw: String?, totalRaw: String?) -> String {
        guard let total = number(totalRaw), total != 0 else { return dash }
        guard let used = number(usedRaw) else { return dash }
        return String(format: "%.0f%%", 100 * used / total)
    }

    /// `float(s.get(key) or 0)` — absent/empty is zero, unparseable is nil
    /// (the caller prints `-`).
    private static func number(_ raw: String?) -> Double? {
        guard let raw, !raw.isEmpty else { return 0 }
        return Double(raw)
    }

    /// `fmt_tokens(n)` — compact token totals: `999`, `202k`, `18.0M`, `1.23G`.
    /// The `k` band is whole, `M` takes one decimal, `G` two — exactly as
    /// written. The thresholds are `< 1e3`/`< 1e6`/`< 1e9`, never `<=`.
    static func tokens(_ value: Double?) -> String {
        guard let value else { return dash }
        if value < 1e3 { return String(format: "%.0f", value) }
        if value < 1e6 { return String(format: "%.0fk", value / 1e3) }
        if value < 1e9 { return String(format: "%.1fM", value / 1e6) }
        return String(format: "%.2fG", value / 1e9)
    }

    /// `_alias(name)` — `orchestrator-model` → `orchestrator`. Every proxy
    /// alias ends in `-model`, and the CLI strips that ONE suffix (not every
    /// occurrence) to keep the `Routes` column narrow.
    static func alias(_ name: String) -> String {
        name.hasSuffix("-model") ? String(name.dropLast("-model".count)) : name
    }

    /// The `Routes` cell: every alias routed to this backend except the model
    /// itself, each `-model`-stripped, comma-joined — `-` when that list is
    /// empty, which includes a backend with no routes at all.
    static func aliasList(_ routes: [String]?, model: String?) -> String {
        let kept = (routes ?? []).filter { $0 != model }.map(alias)
        return kept.isEmpty ? dash : kept.joined(separator: ", ")
    }

    /// `r["model"].rsplit("/", 1)[-1]` — `local-inference-lab/Qwen3.8-Flash`
    /// prints as `Qwen3.8-Flash`, because the org prefix eats the column.
    /// Distinct from `ServingRow.shortModel`, which answers a different
    /// question (nil for a `down` row, for counting); here `-` must stay `-`.
    static func shortModel(_ model: String) -> String {
        String(model.split(separator: "/").last.map(String.init) ?? model)
    }

    /// The CLI's only threshold: `_load_style(running, waiting)` — a queue is
    /// `warn`, a busy engine is `ok`, an idle one is `dim`. Nothing else in
    /// the monitor CLI is thresholded, so every other level in this file is
    /// this file's own rule and says so.
    static func loadLevel(running: Int, waiting: Int) -> ClusterMetricLevel {
        if waiting != 0 { return .warn }
        return running != 0 ? .ok : .dim
    }

    /// Python's `sorted()` orders strings by code point. Swift's `<` uses
    /// canonical equivalence, which disagrees for some non-ASCII names, so the
    /// totals sort compares UTF-8 bytes — the order `sorted(totals.items())`
    /// actually produces.
    static func codePointBefore(_ lhs: String, _ rhs: String) -> Bool {
        lhs.utf8.lexicographicallyPrecedes(rhs.utf8)
    }
}

// MARK: - Threshold colour

/// How hot / how loaded a value is, for the views to paint.
///
/// The monitor CLI colours exactly ONE thing — the serving `Run` column, via
/// `_load_style`, mirrored as `CLIMonitorFormat.loadLevel` — and prints every
/// temperature and power reading in the default style. The numeric thresholds
/// below are therefore THIS FILE's rule, not the CLI's, and a surface painting
/// them must not claim CLI authority for them:
///
///   * CPU temp:  warn ≥ 80, critical ≥ 90 (°C)
///   * GPU temp:  warn ≥ 80, critical ≥ 90 (°C)
///   * GPU power: ≥ 80% / ≥ 95% of `gpu_power_limit_w`; no limit read = no verdict
///   * RAM used:  warn ≥ 85, critical ≥ 95 (%)
///   * CPU used:  warn ≥ 85, critical ≥ 95 (%)
///   * GPU used:  warn ≥ 85, critical ≥ 95 (%) — added by t_1685f884 for the
///     Live Activity's glance metric, on the same util band as the two above:
///     one "how busy is this part" rule, not three.
///
/// Unreadable (`nil`) is `.unknown`, never `.dim`: `.dim` is the CLI's word
/// for "measured, and idle", and painting an absent metric like an idle one is
/// §6 rule 1.
enum ClusterMetricLevel: String, Codable, CaseIterable {
    case unknown, dim, ok, warn, critical
}

extension ClusterMetricLevel {
    /// Threshold on a plain value; `nil` means "not told".
    static func level(_ value: Double?, warn warnFrom: Double, critical critFrom: Double) -> ClusterMetricLevel {
        guard let value else { return .unknown }
        if value >= critFrom { return .critical }
        if value >= warnFrom { return .warn }
        return .ok
    }
}

// MARK: - Host row

/// One row of `host_table()`. `cells` is the row exactly as the CLI hands it
/// to `Table.add_row`: nine already-formatted strings, `-` where the CLI
/// prints `-`.
struct CLIMonitorHostRow: Equatable, Identifiable {
    /// The host table's columns, in the CLI's order (Host is the only
    /// left-justified one; the other eight are `justify="right"`).
    static let columns = ["Host", "Jobs", "CPU%", "RAM%", "Swap%", "GPU%",
                          "CPU Temp", "GPU Temp", "GPU Power"]

    let host: String
    let jobs: String
    let cpuPct: String
    let ramPct: String
    let swapPct: String
    let gpuPct: String
    let cpuTemp: String
    let gpuTemp: String
    let gpuPower: String
    /// The host's `error`, which the CLI prints in the CPU% column of a host
    /// that never sampled. Nil on a row that sampled — including a host that
    /// reported an error AND a sample, which the CLI prints as a normal row.
    let error: String?
    /// The label a view should show instead of `host` (the `.NNN` tail or the
    /// reported hostname, §7). `host` stays the wire value because the CLI's
    /// column is the wire value; a surface that draws an address is showing
    /// the wrong thing, so it draws this instead.
    let displayLabel: String

    // The numbers behind the strings, for a surface that draws a gauge or a
    // colour rather than the CLI's text. Parsed from the same reads, never
    // re-derived from the formatted strings.
    let cpuUsagePct: Double?
    let memUsedPct: Double?
    let gpuUtilPct: Double?
    let cpuTempC: Double?
    let gpuTempC: Double?
    let gpuPowerW: Double?
    let gpuPowerLimitW: Double?

    var id: String { host }

    /// The row as `Table.add_row(...)` receives it.
    var cells: [String] {
        [host, jobs, cpuPct, ramPct, swapPct, gpuPct, cpuTemp, gpuTemp, gpuPower]
    }

    /// Threshold colour for the thermal and load columns (see
    /// `ClusterMetricLevel` for whose thresholds these are).
    var cpuTempLevel: ClusterMetricLevel { .level(cpuTempC, warn: 80, critical: 90) }
    var gpuTempLevel: ClusterMetricLevel { .level(gpuTempC, warn: 80, critical: 90) }
    var memLevel: ClusterMetricLevel { .level(memUsedPct, warn: 85, critical: 95) }
    var cpuLevel: ClusterMetricLevel { .level(cpuUsagePct, warn: 85, critical: 95) }
    /// GPU utilization, on the same util band as CPU and RAM above (t_1685f884:
    /// the Live Activity's glance metric needed a verdict for it, and a second
    /// band invented in a surface is how one card ends up calling 75% hot and
    /// another calling it fine).
    var gpuUtilLevel: ClusterMetricLevel { .level(gpuUtilPct, warn: 85, critical: 95) }

    /// GPU power only has a verdict against the part's own limit; the CLI
    /// prints the wattage and nothing else, so no limit read is no verdict.
    var gpuPowerLevel: ClusterMetricLevel {
        guard let w = gpuPowerW, let limit = gpuPowerLimitW, limit > 0 else { return .unknown }
        return .level(100 * w / limit, warn: 80, critical: 95)
    }

    /// Mirror of `host_table`'s loop body, including its error branch:
    /// `if h.get("error") and not s:` prints the reason in the CPU% column and
    /// a `-` in the other six numeric columns (Jobs included).
    init(node: MonitorNode) {
        let sample = node.sample
        host = node.host
        displayLabel = node.displayLabel
        if let reason = node.cliErrorText, node.cliHasNoSample {
            error = reason
            jobs = CLIMonitorFormat.dash
            cpuPct = reason
            ramPct = CLIMonitorFormat.dash
            swapPct = CLIMonitorFormat.dash
            gpuPct = CLIMonitorFormat.dash
            cpuTemp = CLIMonitorFormat.dash
            gpuTemp = CLIMonitorFormat.dash
            gpuPower = CLIMonitorFormat.dash
            cpuUsagePct = nil; memUsedPct = nil; gpuUtilPct = nil
            cpuTempC = nil; gpuTempC = nil; gpuPowerW = nil; gpuPowerLimitW = nil
            return
        }
        error = nil
        jobs = CLIMonitorFormat.cell(sample?.sparkrun_jobs, places: 0)
        cpuPct = CLIMonitorFormat.cell(sample?.cpu_usage_pct, places: 1)
        ramPct = CLIMonitorFormat.cell(sample?.mem_used_pct, places: 1)
        swapPct = CLIMonitorFormat.swapPct(usedRaw: sample?.swap_used_mb,
                                          totalRaw: sample?.swap_total_mb)
        gpuPct = CLIMonitorFormat.cell(sample?.gpu_util_pct, places: 0)
        cpuTemp = CLIMonitorFormat.cell(sample?.cpu_temp_c, places: 1, suffix: " C")
        gpuTemp = CLIMonitorFormat.cell(sample?.gpu_temp_c, places: 0, suffix: " C")
        gpuPower = CLIMonitorFormat.cell(sample?.gpu_power_w, places: 2, suffix: " W")
        cpuUsagePct = sample?.cpuUsagePct
        memUsedPct = sample?.memUsedPct
        gpuUtilPct = sample?.gpuUtilPct
        cpuTempC = sample?.cpuTempC
        gpuTempC = sample?.gpuTempC
        gpuPowerW = sample?.gpuPowerW
        gpuPowerLimitW = sample?.gpuPowerLimitW
    }
}

/// The two truth tests `host_table` applies to the host object, both of which
/// are Python truthiness and so are NOT the same as `!= nil`.
extension MonitorNode {
    /// `h.get("error")` — present AND non-empty. An empty-string error is
    /// falsy in Python and prints a normal row. (`isUp` on this type reads
    /// `error == nil`, which is the app's rule for "did this host answer" and
    /// is deliberately not touched here.)
    var cliErrorText: String? {
        guard let error, !error.isEmpty else { return nil }
        return error
    }

    /// `not s` where `s = h.get("sample") or {}` — an absent sample AND an
    /// EMPTY sample object are both "no sample" in Python, so a host with
    /// `"error": "ssh: refused", "sample": {}` prints the error row rather
    /// than a row of dashes.
    var cliHasNoSample: Bool {
        guard let sample else { return true }
        return sample.readsAsNoSample
    }
}

extension MonitorSample {
    /// Whether the wire sent this host no sample at all.
    ///
    /// Derived from the struct's own stored properties rather than from a
    /// hand-written list of them: a list goes stale the moment a field is
    /// added, and a stale list reads as "this sample is empty" — a host that
    /// sampled would start printing the CLI's error row. `MonitorSample`'s
    /// stored properties are all `String?`, so "every one is nil" is exactly
    /// "the object had no keys", and a decoded `{}` is the only payload that
    /// satisfies it (sparkrun always stamps `timestamp`, which is declared).
    var readsAsNoSample: Bool {
        Mirror(reflecting: self).children.allSatisfy { child in
            if case Optional<Any>.none = child.value { return true }
            return false
        }
    }
}

// MARK: - Serving row

/// One row of `serving_table()` — one (backend, model) — or one of the
/// `total (n)` rows the CLI appends per model.
///
/// `cells(wide:)` is the row, and width is part of the ROW and not a filter on
/// it: the CLI builds both shapes through one `row()` helper that slices
/// `rest[:6]`, so for a `down` backend the reason lands in `Routes` when wide
/// and in `Run` when narrow. That is the CLI's behaviour, mirrored rather than
/// "fixed"; the fields below describe the WIDE row, and `cells(wide: false)`
/// drops `Routes` and `Prompt t/s` exactly as the CLI does.
struct CLIMonitorServingRow: Equatable, Identifiable {
    /// Wide: every column. Narrow (`width < WIDE_MIN`): the same minus
    /// `Routes` and `Prompt t/s`, in the CLI's order.
    static let wideColumns = ["Node", "Port", "Model", "Routes", "Run", "Wait",
                              "KV%", "In", "Out", "Gen t/s", "Prompt t/s"]
    static let narrowColumns = ["Node", "Port", "Model", "Run", "Wait",
                                "KV%", "In", "Out", "Gen t/s"]

    let node: String
    let port: String
    let model: String
    let routes: String
    let run: String
    let wait: String
    let kvPct: String
    let tokensIn: String
    let tokensOut: String
    let genTps: String
    let promptTps: String

    /// A `total (n)` row carries no node/port (the CLI prints "").
    let isTotalRow: Bool
    /// The backend reported an error, so the CLI printed `down` + the reason.
    let isDown: Bool
    /// The queue state, per the CLI's own `_load_style`. A `down` or total row
    /// has no per-backend style: `.unknown` and `.ok` respectively.
    let loadLevel: ClusterMetricLevel
    /// Short model name, for grouping and labels.
    let shortModel: String

    var id: String { isTotalRow ? "total:\(shortModel)" : "\(node):\(port):\(model)" }

    /// The row as `Table.add_row(...)` receives it, for the requested width.
    func cells(wide: Bool) -> [String] {
        wide ? [node, port, model, routes, run, wait, kvPct, tokensIn, tokensOut, genTps, promptTps]
             : [node, port, model, run, wait, kvPct, tokensIn, tokensOut, genTps]
    }

    /// The same, from a terminal width — the CLI's own `WIDE_MIN` rule.
    func cells(forWidth width: Int) -> [String] { cells(wide: width >= CLIMonitorFormat.wideMin) }

    /// Mirror of `serving_table`'s per-row body.
    init(row: ServingRow) {
        isTotalRow = false
        node = row.node ?? ""
        port = row.port ?? ""
        shortModel = CLIMonitorFormat.shortModel(row.model ?? "-")
        let down = row.error != nil
        isDown = down
        routes = CLIMonitorFormat.aliasList(row.routes, model: row.model)
        if down {
            // The dead-backend row: `down` in Model, the reason truncated to
            // 30 characters in the next slot, and six empty numeric cells.
            model = "down"
            let reason = String((row.error ?? "").prefix(30))
            run = reason; wait = ""; kvPct = ""; tokensIn = ""; tokensOut = ""
            genTps = ""; promptTps = ""
            loadLevel = .unknown
            return
        }
        model = shortModel
        // `running`/`waiting` are ints on the wire (the poller writes
        // `int(m.get("running", 0))`), so nil is unreachable — and
        // `model_decode_check` asserts the fixture proves it rather than this
        // line guessing what the CLI would have printed.
        let running = row.running ?? 0
        let waiting = row.waiting ?? 0
        run = String(running)
        wait = String(waiting)
        // KV is a 0…1 fraction on the wire and a percentage in the table.
        kvPct = CLIMonitorFormat.cell(row.kv.map { $0 * 100 }, places: 1)
        tokensIn = CLIMonitorFormat.tokens(row.prompt_total)
        tokensOut = CLIMonitorFormat.tokens(row.gen_total)
        genTps = CLIMonitorFormat.cell(row.gen_tps, places: 1)
        promptTps = CLIMonitorFormat.cell(row.prompt_tps, places: 0)
        loadLevel = CLIMonitorFormat.loadLevel(running: running, waiting: waiting)
    }

    /// The `total (n)` row `serving_table` appends per model, built from the
    /// same `totals` accumulator the CLI reduces.
    init(total: CLIMonitorServingTotal) {
        isTotalRow = true
        isDown = false
        shortModel = total.model
        node = "total (\(total.backends))"
        port = ""
        model = total.model
        routes = ""
        run = String(total.running)
        wait = String(total.waiting)
        kvPct = ""
        tokensIn = CLIMonitorFormat.tokens(total.promptTotal)
        tokensOut = CLIMonitorFormat.tokens(total.genTotal)
        genTps = total.genTps.map { String(format: "%.1f", $0) } ?? CLIMonitorFormat.dash
        // The CLI passes "" here, not "-": summing a rate across rows that
        // each measured a different interval is meaningless, and a `-` would
        // read as "measured zero".
        promptTps = ""
        loadLevel = .ok
    }
}

/// One model's totals — the CLI's `totals` dict reduced to what its row
/// prints. `backends` is the `n` in `total (n)`: live ROWS counted, so a
/// `down` backend is not in it.
struct CLIMonitorServingTotal: Equatable, Identifiable {
    let model: String
    let running: Int
    let waiting: Int
    /// Summed `Gen t/s` over the rows that reported one; nil when none did —
    /// the CLI prints `-`, never `0.0`.
    let genTps: Double?
    let backends: Int
    let promptTotal: Double?
    let genTotal: Double?

    var id: String { model }
}

extension CLIMonitorServingTotal {
    /// `serving_table`'s accumulator: `[run, wait, gen, n, prompt_total,
    /// gen_total]` keyed by SHORT model name, over the rows that answered —
    /// an errored row is `continue`d before it, so it adds nothing. A rate or
    /// a total that is nil on every row stays nil; a partial sum of the rows
    /// that reported one is what the CLI prints.
    static func summing(rows: [ServingRow]) -> [CLIMonitorServingTotal] {
        var acc: [String: (run: Int, wait: Int, gen: Double?, n: Int, pin: Double?, pout: Double?)] = [:]
        for row in rows where row.error == nil {
            let short = CLIMonitorFormat.shortModel(row.model ?? "-")
            var a = acc[short] ?? (0, 0, nil, 0, nil, nil)
            a.run += row.running ?? 0
            a.wait += row.waiting ?? 0
            if let gen = row.gen_tps { a.gen = (a.gen ?? 0) + gen }
            a.n += 1
            if let pin = row.prompt_total { a.pin = (a.pin ?? 0) + pin }
            if let pout = row.gen_total { a.pout = (a.pout ?? 0) + pout }
            acc[short] = a
        }
        return acc.keys.sorted { CLIMonitorFormat.codePointBefore($0, $1) }.map { key in
            let a = acc[key]!
            return CLIMonitorServingTotal(model: key, running: a.run, waiting: a.wait,
                                          genTps: a.gen, backends: a.n,
                                          promptTotal: a.pin, genTotal: a.pout)
        }
    }
}

// MARK: - The serving-block note line

/// The one line `render()` puts under the serving header when something is wrong
/// or empty — the ONE owner of that sentence for every surface in both apps.
///
/// `hscc_daemon/cluster_live.py`'s `render()` is the authority: it appends the
/// `warn` proxy line between the serving header and the table, and prints the dim
/// `no backends behind the proxy` in place of the table when there are no rows.
/// The macOS dropdown has drawn these words since t_f5fdc6b2; `ClusterMonitor-
/// Tables.proxyError` was projected and drawn by NO iOS view, so the phone met the
/// same failure and said nothing (t_b5d0ec18). A surface draws `text` verbatim and
/// decides only colour — a per-platform re-wording is exactly what this type
/// exists to make impossible, and both apps' gates hold it to the CLI's words.
///
/// Lives here rather than in a new file because `ClusterMonitorViewModel.swift` is
/// Foundation-only, is compiled by all five iOS targets AND by both macOS targets
/// (`macos-app/project.yml`), and is where the state it reads is projected: one
/// file, one owner, no second `- path:` list to forget.
enum CLIMonitorNote: Equatable {
    /// The CLI's dim note beside the serving header. Not a state, so it is not
    /// part of `servingBlock`'s decision: it is the header's own caption, owned
    /// here so no platform re-words it. Its exact bytes are pinned against the
    /// CLI's rendered text in `monitor_tables.swift`.
    static let tokensNote = "In/Out = tokens since each engine started"

    /// The proxy route map could not be refreshed. `showingLastKnownRoutes` is
    /// the CLI's ` (showing last known routes)` suffix, which it prints only when
    /// rows were drawn: the map is stale, but the routes on screen are real, and
    /// the sentence has to say which of the two the operator is looking at.
    case proxyUnreachable(error: String, showingLastKnownRoutes: Bool)
    /// The serving poll itself failed. `serving_error` has NO line in `render()`
    /// (the live view just keeps the last rows), so this sentence is the app's —
    /// and this is its only owner.
    case servingPollFailed(error: String)
    /// The route sent a serving section and it was empty: the CLI's dim line in
    /// place of the table. Deliberately NOT the sentence for a MISSING section
    /// (§6 rule 1 — nobody said anything, so the surface says nothing), which is
    /// why `servingBlock` needs `hasServingSection` rather than "no rows".
    case noBackends

    /// The CLI's precedence: the proxy failure first (it is why the table may be
    /// stale), then the poll failure, then the empty table. `backendRows` counts
    /// the CLI's `rows` — the backends, never the per-model `total (n)` rows,
    /// which the CLI appends itself and which would make an empty fleet look
    /// like it had routes.
    static func servingBlock(proxyError: String?, servingError: String?,
                             hasServingSection: Bool, backendRows: Int) -> CLIMonitorNote? {
        if let proxyError {
            return .proxyUnreachable(error: proxyError,
                                     showingLastKnownRoutes: backendRows > 0)
        }
        if let servingError { return .servingPollFailed(error: servingError) }
        return hasServingSection && backendRows == 0 ? .noBackends : nil
    }

    /// The line as printed. Composed only here.
    var text: String {
        switch self {
        case .proxyUnreachable(let error, let showingRoutes):
            return "proxy /v1/model/info unreachable: \(error)"
                + (showingRoutes ? " (showing last known routes)" : "")
        case .servingPollFailed(let error):
            return "serving poll failed: \(error)"
        case .noBackends:
            return "no backends behind the proxy"
        }
    }

    /// Whether the CLI styles this line `warn` — only the proxy one; the
    /// empty-table line is `dim`. A surface that paints everything as a warning
    /// makes the operator's calm readings stop being readable as calm.
    var isWarning: Bool {
        if case .proxyUnreachable = self { return true }
        return false
    }
}

// MARK: - The view-model

/// `hscc cluster monitor`'s whole human view, as data: the host table, the
/// serving table at a given terminal width, and the per-model totals the CLI
/// appends. A widget draws `hostRows` and `servingRows`; it owns layout and
/// colour only — never a number, a unit or a rounding rule.
///
/// Projected from the same `MonitorResponse` the widgets already fetch, and
/// faithful to the CLI when the fleet is DOWN too: a degraded route
/// (`{"speak": …}`, no `json`) projects to zero host rows, which is the CLI's
/// `0 host(s)` line, not a table of empty rows.
struct ClusterMonitorViewModel: Equatable {
    /// The title line's count: `hscc cluster monitor — N host(s) — …`.
    let hostCount: Int
    /// Host table rows, in the order the route listed them.
    let hostRows: [CLIMonitorHostRow]
    /// Every backend row the route reported, errored ones included (the CLI
    /// keeps a `down` row rather than dropping it).
    let backendRows: [CLIMonitorServingRow]
    /// Per-model totals, in the CLI's order: sorted by short model name.
    let totalRows: [CLIMonitorServingTotal]
    /// The proxy route map could not be refreshed (`serving_proxy_error`); the
    /// CLI prints a `warn` line and keeps the last known routes.
    let proxyError: String?
    /// The route's serving poll itself failed (`serving_error`). The section
    /// is then absent — silence, and NOT "no backends".
    let servingError: String?
    /// Whether the route sent a `serving` section at all, captured at projection
    /// time. It has to be captured: `rows = response.serving ?? []` below already
    /// flattened "empty list" and "no key" into the same empty array, and the two
    /// are different sentences (§6 rule 1). An errored poll always implies no
    /// section; a nil error does NOT imply a section was sent.
    let toldServingSection: Bool

    /// nil only for a nil response (the read failed). A degraded response
    /// projects to an empty view rather than nil, because the CLI prints its
    /// headers and `0 host(s)` for that case rather than nothing.
    static func project(_ response: MonitorResponse?) -> ClusterMonitorViewModel? {
        guard let response else { return nil }
        let hosts = response.json?.hosts ?? []
        let rows = response.serving ?? []
        return ClusterMonitorViewModel(
            hostCount: hosts.count,
            hostRows: hosts.map(CLIMonitorHostRow.init(node:)),
            backendRows: rows.map(CLIMonitorServingRow.init(row:)),
            totalRows: CLIMonitorServingTotal.summing(rows: rows),
            proxyError: response.serving_proxy_error,
            servingError: response.serving_error,
            toldServingSection: response.serving != nil)
    }

    /// Whether the serving section exists at all. `false` means the server
    /// never sent it: the CLI prints `no backends behind the proxy` for an
    /// EMPTY list and shows nothing for a missing one, so a surface must not
    /// read "0 models" out of a nil section (§6 rule 1).
    ///
    /// This reads the wire key's presence rather than `servingError`, which is a
    /// different fact: a clean poll of a route that sent no `serving` key at all
    /// has no error and still has nothing to show (t_9e110d88 caught the
    /// conflation — the small tile rendered that case as "0 running, 0 waiting").
    var hasServingSection: Bool { toldServingSection }

    /// The CLI's one note line under the serving header, or nil when the CLI
    /// prints none. The words and their precedence are `CLIMonitorNote`'s; a
    /// surface draws `text` and chooses only colour. The dropdown's `Tile.note`
    /// is this value's `text`, so phone and desktop cannot say two different
    /// things about the same failure.
    var note: CLIMonitorNote? {
        CLIMonitorNote.servingBlock(proxyError: proxyError, servingError: servingError,
                                    hasServingSection: hasServingSection,
                                    backendRows: backendRows.count)
    }

    /// The host table as cell arrays (`host_table` has no width rule — it is
    /// always these nine columns).
    var hostTable: [[String]] { hostRows.map(\.cells) }

    /// Backend rows followed by the per-model total rows, in the order
    /// `serving_table` adds them.
    var servingRows: [CLIMonitorServingRow] {
        backendRows + totalRows.map(CLIMonitorServingRow.init(total:))
    }

    /// The serving table as cell arrays for a terminal of `width`.
    func servingTable(forWidth width: Int) -> [[String]] {
        servingRows.map { $0.cells(forWidth: width) }
    }

    /// The serving table's header row for a terminal of `width`.
    func servingColumns(forWidth width: Int) -> [String] {
        width >= CLIMonitorFormat.wideMin ? CLIMonitorServingRow.wideColumns
                                          : CLIMonitorServingRow.narrowColumns
    }
}
