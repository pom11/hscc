import Foundation

// ===========================================================================
// monitor_parity.swift — the cluster-monitor CLI parity check (t_61ed3f5e).
//
// Compiled by model_decode_check.sh alongside main.swift and the REAL model +
// view-model sources. Only declarations live in this file: Swift forbids
// top-level statements in a non-main file, so main.swift holds the calls.
//
// WHAT THE GOLDENS ARE
//
// `v1_cluster_monitor_live.cli.json` / `.cli.txt` are not transcribed by hand.
// `scripts/cluster_monitor_cli_parity.py` feeds a fixture's bytes into
// `hscc_daemon/cluster_live.py`'s real `host_table()` / `serving_table()` /
// `render()` and records the strings the CLI put in each cell, plus the whole
// plain-text view at two widths. Everything here compares THIS run's
// `ClusterMonitorViewModel` against that. Change the CLI's formatting, run the
// generator with --write, and these checks go red until the view-model moves
// with it — that coupling is the point of pairing them.
//
// THREE LEVELS, because each catches a different mistake:
//   1. the column LABELS — a renamed column is a different view, not the same
//      view reformatted;
//   2. every CELL string, per row, in both widths — rounding, units, the `-`
//      for unreadable, the 30-character error truncation, the `k`/`M`/`G`
//      token bands, and the `total (n)` rows;
//   3. the same cells against the CLI's plain-text render, so a golden that
//      drifted away from what the CLI actually prints cannot pass level 2
//      on its own.
// ===========================================================================

/// The generator's output shape. Local to the harness on purpose: nothing in
/// the app reads it, and declaring it in `SharedModels.swift` would put a
/// mirror of the CLI's own file where someone might start maintaining it.
struct CLIGolden: Decodable {
    let title_wide: String
    let host_header: [String]
    let host_rows: [[String]]
    let serving_header_wide: [String]
    let serving_rows_wide: [[String]]
    let serving_header_narrow: [String]
    let serving_rows_narrow: [[String]]
}

/// One parity case: a fixture's wire bytes, the golden generated from them,
/// and the CLI's plain-text render of them at the wide width.
func monitorParity(_ c: DecodeCheck, fixture: String, golden goldenName: String,
                   text textName: String) {
    let label = "cluster monitor CLI parity"
    do {
        let response = try JSONDecoder().decode(MonitorResponse.self, from: try c.load(fixture))
        let cli = try JSONDecoder().decode(CLIGolden.self, from: try c.load(goldenName))
        let wideText = String(data: try c.load(textName), encoding: .utf8) ?? ""
        guard let view = ClusterMonitorViewModel.project(response) else {
            c.expect(fixture, label, false, "the view-model projected nothing from a payload that decoded")
            return
        }

        // The host count, read off the CLI's own title line rather than
        // asserted from the same array the view-model counted.
        c.expect(fixture, "\(label): hostCount is the number in the CLI's own title line",
                 cli.title_wide.contains("\(view.hostCount) host(s)")
                 && view.hostCount == view.hostRows.count,
                 "view says \(view.hostCount) host(s), CLI title says \(cli.title_wide)")

        // 1. Labels.
        c.expect(fixture, "\(label): host column labels are the CLI's",
                 cli.host_header == CLIMonitorHostRow.columns,
                 "CLI \(cli.host_header) vs view-model \(CLIMonitorHostRow.columns)")
        c.expect(fixture, "\(label): serving column labels are the CLI's, wide and narrow",
                 cli.serving_header_wide == CLIMonitorServingRow.wideColumns
                 && cli.serving_header_narrow == CLIMonitorServingRow.narrowColumns,
                 "CLI wide \(cli.serving_header_wide) narrow \(cli.serving_header_narrow)")

        // 2. Cells, per row.
        let hostRows = view.hostRows.map(\.cells)
        c.expect(fixture, "\(label): every host row's cells equal the CLI's",
                 hostRows.count == cli.host_rows.count
                 && zip(hostRows, cli.host_rows).allSatisfy(==),
                 monitorDiff(hostRows, cli.host_rows, CLIMonitorHostRow.columns))
        let wide = view.servingTable(forWidth: 140)
        let narrow = view.servingTable(forWidth: 90)
        c.expect(fixture, "\(label): every serving row's wide cells equal the CLI's",
                 wide.count == cli.serving_rows_wide.count
                 && zip(wide, cli.serving_rows_wide).allSatisfy(==),
                 monitorDiff(wide, cli.serving_rows_wide, CLIMonitorServingRow.wideColumns))
        c.expect(fixture, "\(label): every serving row's narrow cells equal the CLI's",
                 narrow.count == cli.serving_rows_narrow.count
                 && zip(narrow, cli.serving_rows_narrow).allSatisfy(==),
                 monitorDiff(narrow, cli.serving_rows_narrow, CLIMonitorServingRow.narrowColumns))
        // The width rule has to be a real switch rather than two lists that
        // happen to agree today: 110 is the CLI's WIDE_MIN, and the shapes do
        // differ (the narrow row drops `Routes` and `Prompt t/s`).
        c.expect(fixture, "\(label): WIDE_MIN is the CLI's 110 and the two shapes differ",
                 CLIMonitorFormat.wideMin == 110
                 && wide.first?.count == 11 && narrow.first?.count == 9,
                 "wideMin \(CLIMonitorFormat.wideMin), wide row \(wide.first?.count ?? -1) cells, narrow row \(narrow.first?.count ?? -1)")

        // 3. The same cells against the CLI's rendered text. Every cell of a
        // row has to be REACHABLE in what the CLI printed, which is what stops a
        // golden that drifted away from the real render from passing level 2 on
        // its own. Reachable means: the cell appears verbatim, or the text shows
        // an elision of it (`Qwen3-30B-A3B[GPT…` — `serving_table` caps Model and
        // Routes at `max_width=26` with `overflow="ellipsis"`, so even the WIDE
        // render truncates a long model id; measured, this fixture's
        // `org/Qwen3-30B-A3B[GPTQ]` row is what proved it). A fabricated cell
        // matches neither: it is in the text nowhere, and no elision of the
        // CLI's own string happens to be a prefix of the invention.
        var stray: [String] = []
        for row in hostRows + wide {
            let cells = row.filter { !$0.isEmpty }
            if cells.isEmpty { continue }
            if !cells.allSatisfy({ monitorShown($0, in: wideText) }) {
                stray.append(row.joined(separator: " | "))
            }
        }
        c.expect(fixture, "\(label): every row also appears in the CLI's text output",
                 stray.isEmpty, "no CLI line holds: \(stray.joined(separator: " ;; "))")
    } catch {
        c.expect(fixture, label, false, "\(error)")
    }
}

/// Whether `cell` is reachable in the CLI's own text: verbatim, or elided by
/// rich where a column caps its width (`serving_table` gives Model and Routes
/// `max_width=26, overflow="ellipsis"`, so a long model id prints as
/// `Qwen3-30B-A3B[GPT…`). A cell nobody fabricated matches one arm or the other;
/// an invented string matches neither, which is the point of the level.
func monitorShown(_ cell: String, in text: String) -> Bool {
    if text.contains(cell) { return true }
    var prefix = ""
    for ch in cell {
        prefix.append(ch)
        if prefix.count >= 3, text.contains(prefix + "…") { return true }
    }
    return false
}

/// A failure message that names the ROW and the COLUMN, so a red run says
/// which cell moved instead of "the tables differ".
func monitorDiff(_ got: [[String]], _ want: [[String]], _ columns: [String]) -> String {
    if got.count != want.count {
        return "row count: view-model \(got.count), CLI \(want.count)"
    }
    for (i, pair) in zip(got, want).enumerated() {
        let (g, w) = pair
        if g.count != w.count {
            return "row \(i) cell count: view-model \(g.count), CLI \(w.count)"
        }
        for (j, cell) in w.enumerated() where g[j] != cell {
            let column = columns.indices.contains(j) ? columns[j] : "column \(j)"
            return "row \(i) \(column): view-model \"\(g[j])\", CLI \"\(cell)\""
        }
    }
    return "rows differ"
}

/// The `= nil` trap this file's own subject is exposed to: a wire key declared
/// WITH a default compiles, is never decoded, and its column prints `-`
/// forever while every gate stays green. So the host table's inputs are
/// asserted against the live capture, and the set of keys the model does NOT
/// carry is MEASURED rather than asserted from a comment.
func monitorSampleDecodeShape(_ c: DecodeCheck) {
    let fixture = "v1_cluster_monitor_live.json"
    c.assertions(fixture, "every host-table field decodes") {
        let data = try c.load(fixture)
        let response = try JSONDecoder().decode(MonitorResponse.self, from: data)
        let hosts = response.json?.hosts ?? []
        c.expect(fixture, "the live capture has hosts to measure against",
                 hosts.count == 4, "got \(hosts.count)")
        let sampled = hosts.compactMap { $0.sample }
        // Every column `host_table` reads, on every host that sampled. A
        // shorter `sampled` list is its own failure: the check would otherwise
        // pass on a fixture that stopped carrying samples.
        let read: [(String, (MonitorSample) -> String?)] = [
            ("sparkrun_jobs", { $0.sparkrun_jobs }), ("cpu_usage_pct", { $0.cpu_usage_pct }),
            ("mem_used_pct", { $0.mem_used_pct }), ("swap_used_mb", { $0.swap_used_mb }),
            ("swap_total_mb", { $0.swap_total_mb }), ("gpu_util_pct", { $0.gpu_util_pct }),
            ("cpu_temp_c", { $0.cpu_temp_c }), ("gpu_temp_c", { $0.gpu_temp_c }),
            ("gpu_power_w", { $0.gpu_power_w }),
        ]
        for (key, get) in read {
            c.expect(fixture, "host column \(key) decodes on every sampled host",
                     sampled.count == hosts.count && sampled.allSatisfy { get($0) != nil },
                     "\(sampled.count)/\(hosts.count) hosts carried a sample; \(key) nil somewhere")
        }

        // The synthesized encoder writes only DECLARED properties, so the keys
        // a decoded sample re-encodes without are exactly the keys no surface
        // can ever read. Naming that set here means §7's one deliberate
        // omission (a sparkrun container name) cannot quietly grow, and a
        // newly-undeclared wire key cannot quietly shrink a column to `-`.
        let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        let rawHosts = (raw?["json"] as? [String: Any])?["hosts"] as? [[String: Any]] ?? []
        guard let wire = rawHosts.first?["sample"] as? [String: Any], let first = sampled.first else {
            c.expect(fixture, "the capture carries a sample to measure", false, "no sample object")
            return
        }
        let encoded = try JSONEncoder().encode(first)
        let carried = Set((try JSONSerialization.jsonObject(with: encoded) as? [String: Any] ?? [:]).keys)
        let missing = Set(wire.keys).subtracting(carried)
        c.expect(fixture, "the ONLY sample key the model does not decode is sparkrun_job_names (§7)",
                 missing == ["sparkrun_job_names"],
                 "not decoded: \(missing.sorted()); decoded \(carried.count) of \(wire.count) wire keys")
    }
}
