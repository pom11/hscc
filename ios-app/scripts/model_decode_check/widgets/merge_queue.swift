import Foundation

/// Merge-queue widget decode + CLI-parity checks (card MQ5).
func checkMergeQueueWidget(_ c: DecodeCheck) {
    // ---- The merge-queue widget (card MQ5) ----
    //
    // `v1_merge_queue.json` is the route's own envelope shape with placeholder
    // project/card text (the queue is operator-private). `v1_merge_queue.cli.json`
    // is not transcribed: `ios-app/scripts/merge_queue_cli_parity.py` builds it by
    // feeding this fixture's rows into the REAL `merge_queue_table`, so these
    // checks prove the widget's cells are the CLI's cells, not a reading of them.
    c.check(MergeQueueLite.self, "v1_merge_queue.json", "MergeQueueLite (GET /v1/merge-queue)")
    c.assertions("v1_merge_queue.json", "the merge-queue projection keeps the CLI's rows, order and cells") {
        struct CLIGolden: Decodable {
            struct Table: Decodable {
                let header: [String]
                let rows: [[String]]
            }
            let tables: [String: Table]
        }
        let payload = try JSONDecoder().decode(MergeQueueLite.self,
                                               from: try c.load("v1_merge_queue.json"))
        let golden = try JSONDecoder().decode(CLIGolden.self,
                                              from: try c.load("v1_merge_queue.cli.json"))
        let feed = MergeQueueFeed.from(payload)

        c.expect("v1_merge_queue.json", "7 rows decode, 3 queued, route count preserved",
                 feed.rows.count == 7 && feed.queued == 3 && feed.count == 7,
                 "rows=\(feed.rows.count) queued=\(feed.queued) count=\(feed.count)")
        c.expect("v1_merge_queue.json",
                 "order is the route's: alpha 101,102,103,99 then beta 201,198 then gamma 301",
                 feed.rows.map(\.pr) == [101, 102, 103, 99, 201, 198, 301],
                 "prs=\(feed.rows.map { String(describing: $0.pr) })")
        c.expect("v1_merge_queue.json", "ages use the CLI vocabulary (1m / 1h02m / 45s / 2h00m / 1d2h / 1d0h)",
                 feed.rows.map(\.ageLabel) == ["1m", "1h02m", "45s", "2h00m",
                                               "1d2h", "1d0h", "1m"],
                 "ages=\(feed.rows.map(\.ageLabel))")

        let groups = feed.groups
        c.expect("v1_merge_queue.json", "three project tables, in registry order",
                 groups.map(\.project) == ["alpha", "beta", "gamma"],
                 "projects=\(groups.map(\.project))")
        var cellMismatch = ""
        for group in groups {
            guard let table = golden.tables[group.project] else {
                cellMismatch = "no CLI table for \(group.project)"
                break
            }
            if table.header != MergeQueueTableReading.columns {
                cellMismatch = "\(group.project) header CLI \(table.header) vs widget \(MergeQueueTableReading.columns)"
                break
            }
            let cells = group.rows.map(\.cells)
            if cells != table.rows {
                cellMismatch = "\(group.project) cells widget \(cells) vs CLI \(table.rows)"
                break
            }
        }
        c.expect("v1_merge_queue.json",
                 "every project's columns and cells are the CLI table's own strings",
                 cellMismatch.isEmpty, cellMismatch)

        let stateCells = feed.rows.map { $0.cells[4] }
        c.expect("v1_merge_queue.json",
                 "state cells are the route's raw states, in order",
                 stateCells == ["queued", "queued", "gating", "merged",
                                "queued", "resolve", "failed"],
                 "states=\(stateCells)")

        c.expect("v1_merge_queue.json", "the live claim is gating #103; the next FIFO row is #101",
                 feed.current?.pr == 103 && feed.current?.state == "gating"
                    && feed.nextQueued?.pr == 101,
                 "current=\(String(describing: feed.current?.pr)) next=\(String(describing: feed.nextQueued?.pr))")
        c.expect("v1_merge_queue.json", "last merged is the smallest-age merged row (#99, 2h ago)",
                 feed.lastMerged?.pr == 99 && feed.lastMerged?.ageLabel == "2h00m",
                 "lastMerged=\(String(describing: feed.lastMerged?.pr)) age=\(String(describing: feed.lastMerged?.ageLabel))")

        // M1's pipeline: the stage vocabulary and an off queue are read off
        // the route, not guessed. `gamma` is disabled in the fixture, but the
        // fleet is mixed, so the live rows still draw.
        c.expect("v1_merge_queue.json",
                 "project enabled flags decode; a mixed fleet is not 'off'",
                 payload.projects?["alpha"]?.enabled == true
                    && payload.projects?["gamma"]?.enabled == false
                    && feed.offProject == nil,
                 "alpha=\(String(describing: payload.projects?["alpha"]?.enabled)) gamma=\(String(describing: payload.projects?["gamma"]?.enabled)) off=\(String(describing: feed.offProject))")
        c.expect("v1_merge_queue.json", "gating maps to the gates stage; merged completes the track",
                 MergeQueueReading.stageName("gating") == "gates"
                    && MergeQueueReading.stageProgress("gating") == 1
                    && MergeQueueReading.stageProgress("merged") == 4
                    && MergeQueueReading.stageLabels == ["rebase", "gates", "push", "merge"],
                 "names=\(MergeQueueReading.stageLabels)")
        let offFeed = MergeQueueFeed(rows: [], count: 0, queued: 0,
                                     generatedAt: nil, offProject: "gamma")
        c.expect("v1_merge_queue.json",
                 "an all-disabled fleet says off for the project, not an empty queue",
                 offFeed.isOff && offFeed.offProject == "gamma"
                    && !MergeQueueFeed.empty.isOff,
                 "off=\(offFeed.isOff) project=\(String(describing: offFeed.offProject))")
        c.expect("v1_merge_queue.json", "M1's waiting count is the queued rows behind the head",
                 feed.waitingCount == 3,
                 "waiting=\(feed.waitingCount) queued=\(feed.queued) current=\(String(describing: feed.current?.pr))")

        c.expect("v1_merge_queue.json",
                 "small/medium/large clips are 3 and 8, and hidden rows are counted",
                 MergeQueueTableReading.visibleRows(feed.rows, limit: 3).count == 3
                    && MergeQueueTableReading.hiddenRowCount(feed.rows, limit: 3) == 4
                    && MergeQueueTableReading.visibleRows(feed.rows, limit: 8).count == 7
                    && MergeQueueTableReading.hiddenRowCount(feed.rows, limit: 8) == 0,
                 "visible3/hidden3 = \(MergeQueueTableReading.visibleRows(feed.rows, limit: 3).count)/\(MergeQueueTableReading.hiddenRowCount(feed.rows, limit: 3))")
        c.expect("v1_merge_queue.json",
                 "the inline copy names the queued count and the live stage",
                 feed.inlineLine == "3 queued · gating #103"
                    && MergeQueueFeed.empty.inlineLine == "merge queue empty"
                    && MergeQueueFeed.empty.currentLine == "nothing queued",
                 "inline=\(feed.inlineLine) empty=\(MergeQueueFeed.empty.inlineLine)")

        // The CLI flattens newlines in Last result; a retry row's detail is not
        // printed beside its result, which is the CLI's own rule (its fallback is
        // reachable only when the result is empty).
        let newlineRow = MergeQueueRow(lite: MergeQueueRowLite(
            project: "alpha", position: nil, pr: 7, card: nil, branch: nil,
            state: "resolve", ageS: nil, lastResult: "conflict\nshared.txt",
            detail: "ignored while a result exists", isLast: true))
        let newlineCells = MergeQueueTableReading.cells(for: newlineRow)
        c.expect("v1_merge_queue.json",
                 "Last result flattens newlines and missing fields render '-' like the CLI",
                 newlineCells == ["-", "#7", "-", "-", "resolve", "-",
                                  "conflict shared.txt"],
                 "cells=\(newlineCells)")
    }

    // ---- A failed route is offline, never a fresh empty queue ----
    //
    // `GET /v1/merge-queue` answers `{ok: false, rows: []}` when the daemon
    // cannot produce the queue. Before this field was read, that decoded as a
    // successful empty queue and the widget said "merge queue empty · as of
    // now" while the backend was down.
    c.check(MergeQueueLite.self, "v1_merge_queue_not_ok.json",
            "MergeQueueLite (GET /v1/merge-queue, ok:false)")
    c.assertions("v1_merge_queue_not_ok.json",
                 "ok:false surfaces as unavailable, not an empty queue") {
        let payload = try JSONDecoder().decode(MergeQueueLite.self,
                                               from: try c.load("v1_merge_queue_not_ok.json"))
        let feed = MergeQueueFeed.from(payload)
        c.expect("v1_merge_queue_not_ok.json",
                 "ok:false is unavailable and its copy says offline",
                 feed.ok == false && feed.isUnavailable
                    && !feed.isOff
                    && feed.inlineLine == "merge queue offline"
                    && feed.currentLine == "merge queue offline",
                 "ok=\(String(describing: feed.ok)) unavailable=\(feed.isUnavailable) inline=\(feed.inlineLine) current=\(feed.currentLine)")
        c.expect("v1_merge_queue_not_ok.json",
                 "a missing ok (old snapshot) still decodes as healthy",
                 MergeQueueFeed(rows: [], count: 0, queued: 0,
                                generatedAt: nil).isUnavailable == false,
                 "missing ok must not be treated as a failure")
    }
}
