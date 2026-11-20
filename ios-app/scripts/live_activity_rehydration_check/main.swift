import Foundation

// ===========================================================================
// live_activity_rehydration_check — the ONE Live Activity's decision table.
//
// Everything under test is SLICED out of
// `Sources/Shared/ClusterHealthActivity.swift` by the .sh that compiles this
// file: the reading rules (what every A2 surface may print), the orphan rule
// (what the launch sweep ends) and the projection (how the sanctioned reads
// become the pushed signals). Nothing here re-declares a subject type, so a
// green run proves the shipped rules, not a transcription of them.
// ===========================================================================

var failures = 0
func check(_ name: String, _ cond: @autoclosure () -> Bool, _ detail: String = "") {
    if cond() {
        print("  ok: \(name)")
    } else {
        failures += 1
        print("FAIL: \(name) \(detail)")
    }
}

func checkText(_ name: String, _ got: String, _ want: String) {
    check(name, got == want, "got \"\(got)\", want \"\(want)\"")
}

func checkLevel(_ name: String, _ got: ClusterMetricLevel?, _ want: ClusterMetricLevel?) {
    check(name, got == want, "got \(String(describing: got)), want \(String(describing: want))")
}

let now = Date()

// ---------------------------------------------------------------------------
// Orphan decision — the launch sweep.
// ---------------------------------------------------------------------------
print("\nCluster watch — orphan decision (real ClusterHealthOrphanRule)\n")

let quiet2m = now.addingTimeInterval(-2 * 60)
let quiet8h = now.addingTimeInterval(-ClusterHealthOrphanRule.maxSilence)
let quiet8h01 = now.addingTimeInterval(-(ClusterHealthOrphanRule.maxSilence + 0.001))
let quiet12h = now.addingTimeInterval(-12 * 60 * 60)

let healthy = ClusterHealthSignals(servingState: "serving", nodesUp: 4, nodesDown: 0,
                                   nodesTotal: 4, failureLabel: nil, downNodes: [])
check("4/4 up + 2m -> KEEP",
      !ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet2m, now: now))
check("4/4 up + exactly 8h -> KEEP (<=)",
      !ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet8h, now: now))
check("4/4 up + 8h0.001s -> end",
      ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet8h01, now: now))
check("4/4 up + 12h -> end",
      ClusterHealthOrphanRule.isOrphan(healthy, updatedAt: quiet12h, now: now))

let empty = ClusterHealthSignals(servingState: "unknown", nodesUp: nil, nodesDown: nil,
                                 nodesTotal: nil, failureLabel: nil, downNodes: [])
check("an empty bubble is an orphan even when fresh",
      ClusterHealthOrphanRule.isOrphan(empty, updatedAt: quiet2m, now: now))
let blind = ClusterHealthSignals(servingState: "unreachable", nodesUp: nil, nodesDown: nil,
                                 nodesTotal: nil, failureLabel: "No connection", downNodes: [])
check("a blind but non-empty bubble is kept",
      !ClusterHealthOrphanRule.isOrphan(blind, updatedAt: quiet2m, now: now))
let fleetDown = ClusterHealthSignals(servingState: "down", nodesUp: 0, nodesDown: 4,
                                     nodesTotal: 4, failureLabel: nil, downNodes: [".1"])
check("a genuinely down fleet is kept — a zero is an answer",
      !ClusterHealthOrphanRule.isOrphan(fleetDown, updatedAt: quiet2m, now: now))

// ---------------------------------------------------------------------------
// Reading rules — every word and colour a surface draws.
// ---------------------------------------------------------------------------
print("\nCluster watch — what a surface may print (real ClusterHealthReading)\n")

checkText("state: serving", ClusterHealthReading.stateLabel("serving"), "Serving")
checkText("state: waking", ClusterHealthReading.stateLabel("waking"), "Waking")
checkText("state: down", ClusterHealthReading.stateLabel("down"), "Down")
checkText("state: unreachable", ClusterHealthReading.stateLabel("unreachable"), "Can't reach")
checkText("state: unknown", ClusterHealthReading.stateLabel("quantum"), "Unknown")
check("the state table still agrees with the shared enum",
      ClusterState(rawValue: "serving") != nil
      && ClusterState(rawValue: "quantum") == nil
      && ClusterHealthReading.stateLabel("quantum") == "Unknown")

checkText("tally: up of total", ClusterHealthReading.tallyText(up: 3, total: 4), "3/4 up")
checkText("tally: no total", ClusterHealthReading.tallyText(up: 3, total: nil), "3 up")
checkText("tally: not told", ClusterHealthReading.tallyText(up: nil, total: 4), "— up")
checkText("down: nil is not zero", ClusterHealthReading.downText(down: nil), "— down")
checkText("down: zero is zero", ClusterHealthReading.downText(down: 0), "0 down")
checkText("headline: serving + tally", ClusterHealthReading.headline(healthy),
          "Serving · 4/4 up")
checkText("headline: blind read leads with its label",
          ClusterHealthReading.headline(blind), "No connection")
checkText("headline: empty is an em dash", ClusterHealthReading.headline(empty), "—")
check("tone: healthy is ok", ClusterHealthReading.tone(healthy) == .ok)
check("tone: a node down while serving is warn",
      ClusterHealthReading.tone(ClusterHealthSignals(
        servingState: "serving", nodesUp: 3, nodesDown: 1, nodesTotal: 4,
        failureLabel: nil, downNodes: [".4"])) == .warn)
check("tone: down is bad", ClusterHealthReading.tone(fleetDown) == .bad)
check("tone: an unreachable read with no numbers is bad",
      ClusterHealthReading.tone(blind) == .bad)

// A2: header, temperatures, compact strings, footer.
print("\nCluster watch — the A2 layouts (2026-11-20)\n")

checkText("header names the model", ClusterHealthReading.watchHeader(model: "llama-70b"),
          "CLUSTER · llama-70b")
checkText("header without a model", ClusterHealthReading.watchHeader(model: nil), "CLUSTER")
check("as-of is an HH:mm clock", ClusterHealthReading.asOfText(now).range(
        of: #"^\d{2}:\d{2}$"#, options: .regularExpression) != nil)
checkLevel("temp under 70 -> ok", ClusterHealthReading.watchTempLevel(69.9), .ok)
checkLevel("temp 70 -> warn", ClusterHealthReading.watchTempLevel(70), .warn)
checkLevel("temp 74.9 -> warn", ClusterHealthReading.watchTempLevel(74.9), .warn)
checkLevel("temp 75 -> critical", ClusterHealthReading.watchTempLevel(75), .critical)
checkLevel("temp not told -> unknown", ClusterHealthReading.watchTempLevel(nil), .unknown)

let columns = ClusterHealthReading.nodeColumns([
    ClusterHealthHostLine(label: ".1", gpuPct: "34", gpuTemp: "72 C", gpuPower: "10.00 W",
                          gpuTempLevel: .warn, gpuPowerLevel: .ok, isDown: false,
                          gpuTempC: 72, gpuUtilPct: 34),
    ClusterHealthHostLine(label: ".2", gpuPct: "85", gpuTemp: "76 C", gpuPower: "20.00 W",
                          gpuTempLevel: .warn, gpuPowerLevel: .warn, isDown: false,
                          gpuTempC: 76, gpuUtilPct: 85),
    ClusterHealthHostLine(label: ".3", gpuPct: "-", gpuTemp: "-", gpuPower: "-",
                          gpuTempLevel: .unknown, gpuPowerLevel: .unknown, isDown: true),
    ClusterHealthHostLine(label: ".4", gpuPct: "12", gpuTemp: "64 C", gpuPower: "5.00 W",
                          gpuTempLevel: .ok, gpuPowerLevel: .ok, isDown: false,
                          gpuTempC: 64, gpuUtilPct: 12),
])
check("four node columns, in route order", columns.map(\.shortName) == ["S1", "S2", "S3", "S4"])
checkText("column temp is rounded degrees", columns[0].tempText, "72°")
checkText("column detail is S1 · util%", columns[0].detailText, "S1 · 34%")
check("a down node keeps its column but has no reading", columns[2].isDown
      && columns[2].tempText == "—" && columns[2].utilText == "—")
check("the row clips at four", ClusterHealthReading.nodeColumns(
        Array(repeating: columns[0].lineForGate, count: 6)).count == 4)

let watch = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                            monitor: monitor([upHost("1"), upHost("2")]),
                                            monitorLabel: nil, showNodeTally: true,
                                            daemonRunning: true)
checkText("compact leading is the tally", ClusterHealthReading.watchTally(watch), "2/2")
checkText("compact trailing is hottest-temp · tok/s",
          ClusterHealthReading.watchTrailing(watch), "— · — t/s")
checkText("minimal falls back to the tally when no temperature was told",
          ClusterHealthReading.watchMinimal(watch), "2/2")
checkText("footer is tok/s · daemon · autodown",
          ClusterHealthReading.watchFooter(watch),
          "— tok/s · daemon OK · autodown —")

// ---------------------------------------------------------------------------
// Projection — how the sanctioned reads become the pushed signals.
// ---------------------------------------------------------------------------
print("\nCluster watch — the projection the driver pushes (sliced)\n")

func autodown(_ state: String?, enabled: Bool? = nil) -> AutodownStatusResponse? {
    var body = "{\"speak\":\"ok\""
    if let state { body += ",\"state\":\"\(state)\"" }
    if let enabled { body += ",\"enabled\":\(enabled)" }
    body += "}"
    return try? JSONDecoder().decode(AutodownStatusResponse.self, from: Data(body.utf8))
}

/// nil hosts = the down-fleet shape §4.4 measured: the route answers, `json`
/// is simply not in the body, and a required key here would decode-fail a fleet
/// the server just told us is off.
func monitor(_ hosts: [String]?) -> MonitorResponse? {
    let body = hosts.map { hosts in
        "{\"speak\":\"ok\",\"json\":{\"hosts\":[\(hosts.joined(separator: ","))]}}"
    } ?? "{\"speak\":\"cluster monitor unavailable\"}"
    return try? JSONDecoder().decode(MonitorResponse.self, from: Data(body.utf8))
}

func upHost(_ tail: String) -> String { "{\"host\":\"192.0.2.\(tail)\",\"error\":null}" }
func badHost(_ tail: String) -> String { "{\"host\":\"192.0.2.\(tail)\",\"error\":\"ssh timeout\"}" }

check("autodown up -> serving",
      ClusterHealthProjection.servingState(from: autodown("up")) == "serving")
check("autodown waking -> waking",
      ClusterHealthProjection.servingState(from: autodown("waking")) == "waking")
check("autodown down -> down",
      ClusterHealthProjection.servingState(from: autodown("down")) == "down")
check("a state the app does not model -> unknown",
      ClusterHealthProjection.servingState(from: autodown("sideways")) == "unknown")
check("a state read that answered nothing -> unreachable",
      ClusterHealthProjection.servingState(from: nil) == "unreachable")

let fourHosts = [upHost("1"), upHost("2"), badHost("3"), badHost("4")]
let half = ClusterNodeTally.project(monitor(fourHosts))
check("4 listed / 2 errored -> 2 up, 2 down",
      half.nodesUp == 2 && half.nodesDown == 2 && half.nodesTotal == 4)
check("the failed nodes are named in route order", half.downLabels == [".3", ".4"])
check("a down-node label is never an address (§7)",
      !half.downLabels.contains { $0.contains("192.0.2") })

let live = ClusterHealthProjection.signals(autodown: autodown("up", enabled: false),
                                           autodownLabel: nil,
                                           monitor: monitor(fourHosts), monitorLabel: nil,
                                           showNodeTally: true, daemonRunning: false)
check("one push carries the state, tally, daemon and autodown",
      live.servingState == "serving" && live.nodesUp == 2 && live.nodesTotal == 4
      && live.daemonRunning == false && live.autodownEnabled == false)
check("and the colour serving with a node down earns — amber",
      ClusterHealthReading.tone(live) == .warn)
checkText("its footer names the daemon and autodown state",
          ClusterHealthReading.watchFooter(live),
          "— tok/s · daemon down · autodown off")

let stateBlind = ClusterHealthProjection.signals(autodown: nil, autodownLabel: "No connection",
                                                 monitor: monitor([upHost("1")]), monitorLabel: nil,
                                                 showNodeTally: true)
check("a failed state read gives the headline and its own label",
      stateBlind.servingState == "unreachable" && stateBlind.failureLabel == "No connection")
check("while the read that DID answer keeps its numbers",
      stateBlind.nodesUp == 1 && stateBlind.nodesTotal == 1)

let nodeBlind = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                                monitor: nil, monitorLabel: "Timed out",
                                                showNodeTally: true)
check("a failed node read nils the counts — it never zeroes them",
      nodeBlind.nodesUp == nil && nodeBlind.nodesDown == nil && nodeBlind.nodesTotal == nil)
checkText("and the headline is the gap, never 0/0 up",
          ClusterHealthReading.headline(nodeBlind), "Timed out")

let tallyOff = ClusterHealthProjection.signals(autodown: autodown("up"), autodownLabel: nil,
                                               monitor: nil, monitorLabel: "Timed out",
                                               showNodeTally: false)
check("tally switched off: no counts, and not zeros",
      tallyOff.nodesUp == nil && tallyOff.downNodes.isEmpty)
check("tally off: a read the surface is not drawing cannot set its headline",
      tallyOff.failureLabel == nil)
checkText("tally off still renders the state it was told",
          ClusterHealthReading.headline(tallyOff), "Serving")

// -- the stale data clock (audit W2) -----------------------------------------
print("\nCluster watch — a stale snapshot keeps its real time\n")

let sevenMinutesAgo = ClusterHealthReading.snapshotDate(now: now, ageMinutes: 7)
check("a 7-minute-old snapshot is dated 7 minutes back, not now",
      abs(sevenMinutesAgo.timeIntervalSince(now) + 7 * 60) < 0.5)
check("a negative age cannot move a snapshot into the future",
      ClusterHealthReading.snapshotDate(now: now, ageMinutes: -5) == now)
let oldest = ClusterHealthReading.dataDate([now, sevenMinutesAgo, nil])
check("the push uses the OLDEST read it draws", oldest == sevenMinutesAgo)
check("no date at all is nil, never now", ClusterHealthReading.dataDate([nil, nil]) == nil)

// -- payload compatibility ---------------------------------------------------
let roundTrip = ClusterHealthProjection.signals(
    autodown: autodown("up", enabled: true), autodownLabel: nil,
    monitor: monitor([upHost("1"), upHost("2")]), monitorLabel: nil,
    showNodeTally: true, daemonRunning: true)
let encoded = try? JSONEncoder().encode(roundTrip)
let decoded = encoded.flatMap { try? JSONDecoder().decode(ClusterHealthSignals.self, from: $0) }
check("a pushed payload survives encode -> decode intact", decoded == roundTrip)
check("and the new daemon/autodown keys survive",
      decoded?.daemonRunning == true && decoded?.autodownEnabled == true)

let oldPayload = ("{\"servingState\":\"serving\",\"nodesUp\":3,\"nodesDown\":0,"
    + "\"nodesTotal\":3,\"downNodes\":[]}").data(using: .utf8)!
let oldState = try? JSONDecoder().decode(ClusterHealthSignals.self, from: oldPayload)
check("an OLD payload without the new keys still decodes",
      oldState != nil && oldState?.daemonRunning == nil
      && oldState?.autodownEnabled == nil && oldState?.hostLines == nil)

print("")
if failures == 0 {
    print("LIVE ACTIVITY CHECK PASSED — Cluster watch's orphan, reading, A2 layout, projection and stale-clock rules all execute")
} else {
    print("LIVE ACTIVITY CHECK FAILED — \(failures) assertion(s)")
}
exit(failures == 0 ? 0 : 1)

// A tiny helper the clipping assertion uses; kept out of the shipped type so
// the test can build six identical rows without a second initializer.
extension ClusterHealthNodeColumn {
    fileprivate var lineForGate: ClusterHealthHostLine {
        ClusterHealthHostLine(label: shortName, gpuPct: "0", gpuTemp: tempText,
                              gpuPower: "0 W", gpuTempLevel: tempLevel,
                              gpuPowerLevel: .ok, isDown: isDown,
                              gpuTempC: tempC, gpuUtilPct: utilPct)
    }
}
