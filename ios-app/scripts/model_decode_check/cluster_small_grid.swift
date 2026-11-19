import Foundation

// ---------------------------------------------------------------------------
// The systemSmall tile's data half (t_9e110d88).
//
// `Sources/HSCCWidgets/ClusterSmallGrid.swift` is compiled into the widget
// extension AND into this harness, so these checks execute the code the Home
// Screen actually draws, against the goldens `cluster_monitor_cli_parity.py`
// derived from the CLI's own rendering functions. The SwiftUI layer is not in
// here on purpose: it would need WidgetKit, and a view that only lays out
// already-formatted strings has nothing numeric left to get wrong.
//
// The rule they enforce — a dense tile may not invent a number, a unit or a
// zero:
//   * every cell string equals the CLI's golden cell for that host and column
//     (so a power cell that quietly rounded 23.15 W to 23 W, or a temperature
//     that dropped its degree suffix, fails here);
//   * the run/wait totals equal the sum of the fixture's own serving rows, and
//     the model count equals the number of `total (n)` rows in the CLI's own
//     text render — neither number is the tile's arithmetic;
//   * a host that did not fit is counted, never silently dropped;
//   * a metric the API never sent stays the CLI's dash and never becomes a 0.
// ---------------------------------------------------------------------------

func clusterSmallGridChecks(_ c: DecodeCheck) {
    let label = "cluster monitor small tile"
    func ck(_ name: String, _ actual: String, _ expected: String, _ why: String) {
        c.expect(name, "\(label): \(why)", actual == expected,
                 actual == expected ? "" : "got '\(actual)' want '\(expected)'")
    }
    func golden(_ file: String) -> CLIGolden? {
        guard let data = try? c.load(file) else { return nil }
        return try? JSONDecoder().decode(CLIGolden.self, from: data)
    }
    func text(_ file: String) -> String {
        guard let data = try? c.load(file) else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    func projected(_ file: String) -> (MonitorResponse, ClusterMonitorViewModel?)? {
        guard let data = try? c.load(file),
              let resp = try? JSONDecoder().decode(MonitorResponse.self, from: data)
        else { return nil }
        return (resp, ClusterMonitorViewModel.project(resp))
    }
    /// The CLI's own cell for one host row and one grid column, or nil when the
    /// column is not part of the CLI's host table at all.
    func cliCell(_ cli: CLIGolden, _ row: Int, _ column: ClusterSmallGrid.Column) -> String? {
        guard let index = CLIMonitorHostRow.columns.firstIndex(of: column.cliColumn),
              row < cli.host_rows.count, index < cli.host_rows[row].count
        else { return nil }
        return cli.host_rows[row][index]
    }
    func allCells(_ tile: ClusterSmallGrid.Tile) -> [ClusterSmallGrid.Cell] {
        tile.rows.flatMap { $0.cells }
    }
    /// Every cell of a tile against the CLI golden. Returns how many of the
    /// VISITED cells were the CLI's dash, drawn and expected, so the caller can
    /// assert the tile never turned an unreadable metric into a number.
    func checkCells(_ tile: ClusterSmallGrid.Tile, _ cli: CLIGolden, _ prefix: String)
        -> (drawn: Int, expected: Int) {
        var drawn = 0, expected = 0
        for (row, host) in tile.rows.enumerated() {
            for cell in host.cells {
                let want = cliCell(cli, row, cell.column) ?? "-not in the CLI host table-"
                ck("\(prefix):r\(row):\(cell.column.cliColumn)", cell.text, want,
                   "the cell equals the CLI golden for that host and column")
                if cell.text == "-" { drawn += 1 }
                if want == "-" { expected += 1 }
            }
        }
        return (drawn, expected)
    }
    /// §7: the CLI's Host column is the node's address, and the app must not
    /// carry one. A dense grid is exactly where an address sneaks back in as
    /// "just a label", so the tile's own strings are scanned here.
    let dottedQuad = try! NSRegularExpression(pattern: #"\d{1,3}(?:\.\d{1,3}){3}"#)
    func showsAddress(_ s: String) -> Bool {
        dottedQuad.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
    }

    guard let pair = projected("v1_cluster_monitor_live.json"),
          let view = pair.1,
          let cli = golden("v1_cluster_monitor_live.cli.json") else {
        c.expect("gsmall:fixture", "\(label): the live fixture and its golden must load", false,
                 "v1_cluster_monitor_live.json / .cli.json did not load and decode")
        return
    }
    let resp = pair.0
    let hosts = resp.json?.hosts ?? []

    // The header row is the CLI's vocabulary, in the CLI's order, and every one
    // of the tile's four columns must exist in the CLI's host table. If the CLI
    // renames a column the tile's header follows it, or this goes red.
    let headers = ClusterSmallGrid.Column.allCases.map { $0.cliColumn }
    ck("gsmall:headers", headers.joined(separator: "|"),
       "GPU%|GPU Temp|CPU Temp|GPU Power",
       "the tile carries the four metrics the card asked for, accelerator first")
    ck("gsmall:headers:known",
       headers.filter { !cli.host_header.contains($0) }.joined(separator: "|"), "",
       "every column name is one the CLI prints — the ORDER is the tile's (GPU first), "
       + "the VOCABULARY is the CLI's")
    ck("gsmall:headers:four", "\(headers.count)", "4",
       "four columns, so the tile is a grid and not a single number")
    ck("gsmall:hostheader", ClusterSmallGrid.hostColumnLabel, cli.host_header.first ?? "",
       "the row-label header is the CLI's first host-table column")

    guard let tile = ClusterSmallGrid.tile(from: view) else {
        c.expect("gsmall:plain:tile", "\(label): a fixture with hosts must produce a tile",
                 false, "ClusterSmallGrid.tile returned nil")
        return
    }
    ck("gsmall:plain:rows", "\(tile.rows.count)",
       "\(min(hosts.count, ClusterSmallGrid.maxHosts))", "one row per host, up to the cap")
    ck("gsmall:plain:hidden", "\(tile.hiddenHosts)",
       "\(max(0, hosts.count - ClusterSmallGrid.maxHosts))",
       "hosts the tile could not fit are counted, never silently dropped")
    ck("gsmall:plain:labels", tile.rows.map { $0.label }.joined(separator: "|"),
       view.hostRows.prefix(tile.rows.count).map { $0.displayLabel }
           .joined(separator: "|"),
       "the row label is the view-model's display label — the one label the app may show")
    let drawnStrings = tile.rows.flatMap { [$0.label] + $0.cells.map { $0.text } }
    ck("gsmall:plain:noaddr", "\(drawnStrings.filter { showsAddress($0) }.count)", "0",
       "no label or cell on the tile is a node address (§7), though the CLI's Host column is")
    ck("gsmall:plain:addrswapped",
       (tile.rows.first.map { $0.label != (cli.host_rows.first?.first ?? "") } ?? false)
           ? "yes" : "no", "yes",
       "the tile really did swap the CLI's address for the display label")

    let plainDashes = checkCells(tile, cli, "gsmall:plain")
    ck("gsmall:plain:dashcells", "\(plainDashes.drawn)", "\(plainDashes.expected)",
       "a metric the CLI could not read is drawn as the CLI's dash, not as a 0")

    // Totals: the sum of the fixture's serving rows, i.e. the same sum the CLI's
    // `total (n)` rows add up to. The tile cannot report a queue it was not told.
    let servingRows = resp.serving ?? []
    let runSum = servingRows.reduce(0) { $0 + ($1.running ?? 0) }
    let waitSum = servingRows.reduce(0) { $0 + ($1.waiting ?? 0) }
    ck("gsmall:plain:totals",
       "\(tile.totals.running ?? -1)/\(tile.totals.waiting ?? -1)",
       "\(runSum)/\(waitSum)",
       "run/wait equal the sum of the fixture's serving rows")
    let totalRowLines = text("v1_cluster_monitor_live.cli.txt")
        .split(separator: "\n").filter { $0.contains("total (") }.count
    ck("gsmall:plain:models", "\(tile.totals.models ?? -1)", "\(totalRowLines)",
       "models is the number of `total (n)` rows in the CLI's own render")
    ck("gsmall:plain:level", "\(tile.totals.level)", waitSum > 0 ? "warn" : "ok",
       "the totals verdict is the CLI's row style (waiting -> warn), not the tile's")

    // A bar whose length disagrees with the number above it is the classic dense
    // tile lie: a bar appears only where the cell printed a real percentage, and
    // its length is that percentage, clamped.
    let gpuCells = allCells(tile).filter { $0.column == .gpuUtil }
    ck("gsmall:plain:bared", "\(gpuCells.filter { $0.fraction != nil }.count)",
       "\(gpuCells.filter { Double($0.text) != nil }.count)",
       "a bar only where the cell printed a real percentage")
    if let first = gpuCells.first, let fraction = first.fraction {
        ck("gsmall:plain:barvalue", String(format: "%.2f", fraction),
           String(format: "%.2f", min(max((Double(first.text) ?? 0) / 100, 0), 1)),
           "bar length is the printed percentage, clamped to 0...1")
    }
    // The whole tile in one sentence, for VoiceOver: it must name both hosts and
    // totals, so an accessibility user is not left with a single number.
    let summary = tile.accessibilitySummary
    ck("gsmall:plain:axtotals", summary.contains("6 running") ? "yes" : "no", "yes",
       "the one-line summary carries the serving totals the tile shows")
    ck("gsmall:plain:axhosts", summary.contains("node-1") ? "yes" : "no", "yes",
       "the one-line summary names the hosts, not just a count")

    // Four rows fit; seven must not quietly become four.
    let crowdedHosts = hosts.isEmpty ? []
        : Array(repeating: hosts[0], count: ClusterSmallGrid.maxHosts + 3)
    let crowded = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: crowdedHosts),
                                  serving: resp.serving, serving_error: nil,
                                  serving_proxy_error: nil, speak: "crowded")
    let crowdedTile = ClusterSmallGrid.tile(from: ClusterMonitorViewModel.project(crowded))
    ck("gsmall:crowded:rows", "\(crowdedTile?.rows.count ?? -1)",
       "\(ClusterSmallGrid.maxHosts)", "the tile caps at maxHosts rows")
    ck("gsmall:crowded:hidden", "\(crowdedTile?.hiddenHosts ?? -1)", "3",
       "the three hosts that did not fit are counted on the tile")
    ck("gsmall:crowded:ax",
       crowdedTile?.accessibilitySummary.contains("3 more") == true ? "yes" : "no", "yes",
       "the dropped hosts are spoken too, not only drawn")

    // What "nothing to grid" means, and what it does NOT mean.
    //   no host rows AND no serving rows -> nil: the family shows its gate pane
    //     (setup invite / Paused / the reason the read failed) rather than a table
    //     of dashes, which would read as "four hosts, all unreadable".
    //   no host rows but a serving table -> a tile with no rows and real totals.
    //     The serving half was told; hiding it would lose the only live numbers.
    let nothing = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: []),
                                  serving: [], serving_error: nil,
                                  serving_proxy_error: nil, speak: "nothing")
    ck("gsmall:nothing:tile",
       ClusterSmallGrid.tile(from: ClusterMonitorViewModel.project(nothing)) == nil
           ? "nil" : "tile",
       "nil", "a payload with neither host rows nor serving rows must not grid dashes")
    let hostsOnly = MonitorResponse(json: MonitorPayload(timestamp: nil, hosts: []),
                                    serving: servingRows, serving_error: nil,
                                    serving_proxy_error: nil, speak: "serving only")
    let hostsOnlyTile = ClusterSmallGrid.tile(from: ClusterMonitorViewModel.project(hostsOnly))
    ck("gsmall:servingonly:rows", "\(hostsOnlyTile?.rows.count ?? -1)", "0",
       "no host rows means no host rows — the tile invents none")
    ck("gsmall:servingonly:totals",
       hostsOnlyTile.map { "\($0.totals.running ?? -1)/\($0.totals.waiting ?? -1)" } ?? "no tile",
       "\(runSum)/\(waitSum)",
       "the serving totals it WAS told still appear when the host list is empty")

    // Told-empty versus not-told is the §6 rule-1 distinction, and a small tile is
    // where it is easiest to flatten: an empty serving table means nothing is
    // running (print 0), a missing serving section means nobody said (print `-`).
    let toldEmpty = MonitorResponse(json: MonitorPayload(timestamp: nil,
                                                         hosts: Array(hosts.prefix(2))),
                                    serving: [], serving_error: nil,
                                    serving_proxy_error: nil, speak: "told empty")
    let notTold = MonitorResponse(json: MonitorPayload(timestamp: nil,
                                                       hosts: Array(hosts.prefix(2))),
                                  serving: nil, serving_error: nil,
                                  serving_proxy_error: nil, speak: "not told")
    func totalsLine(_ response: MonitorResponse) -> String {
        guard let t = ClusterSmallGrid.tile(from: ClusterMonitorViewModel.project(response)) else {
            return "no tile"
        }
        return "\(t.totals.running ?? -1)/\(t.totals.waiting ?? -1)/\(t.totals.models ?? -1) "
            + "\(t.totals.level)"
    }
    ck("gsmall:toldempty", totalsLine(toldEmpty), "0/0/0 dim",
       "an empty serving table is TOLD: nothing is running, and that is printed as 0")
    ck("gsmall:not-told", totalsLine(notTold), "-1/-1/-1 unknown",
       "a missing serving section is NOT TOLD: the tile prints dashes, never a 0")

    // The edge fixture is where the unreadable numbers live. Same guarantee:
    // whatever the CLI could not read, the tile must not have invented.
    guard let edgePair = projected("v1_cluster_monitor_edge.json"),
          let edgeView = edgePair.1,
          let edgeTile = ClusterSmallGrid.tile(from: edgeView),
          let edgeCLI = golden("v1_cluster_monitor_edge.cli.json") else {
        c.expect("gsmall:edge:tile", "\(label): the edge fixture must produce a tile", false,
                 "v1_cluster_monitor_edge.json / .cli.json did not load, decode or project")
        return
    }
    let edgeDashes = checkCells(edgeTile, edgeCLI, "gsmall:edge")
    ck("gsmall:edge:dashcells", "\(edgeDashes.drawn)", "\(edgeDashes.expected)",
       "the edge fixture's unreadable cells stay the CLI's dash")
    ck("gsmall:edge:nofakezero",
       "\(allCells(edgeTile).filter { $0.level == .unknown && $0.text != "-" }.count)", "0",
       "a metric the API never sent never prints a number")
    ck("gsmall:edge:rows", "\(edgeTile.rows.count)",
       "\(min((edgePair.0.json?.hosts ?? []).count, ClusterSmallGrid.maxHosts))",
       "the edge fixture also gets one row per host, up to the cap")
}
