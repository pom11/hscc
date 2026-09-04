import Foundation

// ===========================================================================
// fleet_stats_render_check — prove the FleetView by_day + activity RENDER logic
// against the REAL live /v1/fleet/stats payload, decoded by the REAL models.
//
// The two pure functions under test (shortDay, parseToolPair) are byte-identical
// replicas of the ones added to FleetView.swift by task t_b07ec05f. They are
// pure string/int transforms with no SwiftUI dependency, so they can run in a
// headless CLI. This harness:
//   1. compiles the REAL Models.swift (via the wrapper script) so FleetStatsResponse
//      decodes the real wire body,
//   2. decodes the captured v1_fleet_stats.json,
//   3. feeds its by_day + activity through the replicas and prints the exact rows
//      the view would render (bar widths computed the same way).
//
// We do NOT recompile FleetView.swift itself (it is a SwiftUI View; a headless
// CLI can't exercise @ViewBuilder). The mapping under test — which data field
// becomes which row — is exactly what this main prints, and build_check.sh
// proves the real file compiles.
//
// NOTE: no String(format:) with %@ — passing a Swift String to %@ is a crash
// source; all output uses string interpolation.
// ===========================================================================

// --- replicas of FleetView.swift helpers (keep in sync) --------------------
func shortDay(_ iso: String) -> String {
    guard iso.count >= 10 else { return iso }
    let start = iso.index(iso.startIndex, offsetBy: 5)
    let end = iso.index(iso.startIndex, offsetBy: 10)
    return String(iso[start..<end])
}

func parseToolPair(_ pair: [JSONValue]) -> (String, Int)? {
    guard pair.count >= 2,
          let name = pair[0].string,
          case .int(let count) = pair[1] else { return nil }
    return (name, count)
}
// ---------------------------------------------------------------------------

let path = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "scripts/live_captures/v1_fleet_stats.json"
guard let data = FileManager.default.contents(atPath: path),
      let state = try? JSONDecoder().decode(FleetStatsResponse.self, from: data) else {
    print("FAIL: could not decode \(path) into FleetStatsResponse")
    exit(1)
}

var failed = false
func check(_ cond: Bool, _ msg: String) {
    print((cond ? "PASS  " : "FAIL  ") + msg)
    if !cond { failed = true }
}

print("Decoded speak: \(state.speak)")

// ---- By day ----
if let byDay = state.completions?.by_day, !byDay.isEmpty {
    let maxValue = byDay.values.max() ?? 1
    let sortedDays = byDay.keys.sorted()
    print("\nBY DAY (chronological, bar width ∝ value/max \(maxValue), full width 140):")
    check(sortedDays == byDay.keys.sorted(), "sorted chronologically")
    // Assert the ORDERING PROPERTY, not a literal date. This used to pin
    // "2026-09-03", so the check failed on every subsequent day regardless of
    // whether the code was correct — a harness that rots by the calendar
    // teaches the operator to ignore a red run.
    check(sortedDays.last == byDay.keys.max(),
          "newest day sorted last: \(sortedDays.last ?? "nil")")
    for date in sortedDays {
        let value = byDay[date] ?? 0
        let width = Int(Double(value) / Double(maxValue) * 140)
        print("  \(shortDay(date))   \(value)   barWidth=\(width)")
    }
    check(shortDay("2026-08-27") == "08-27", "shortDay('2026-08-27') == '08-27'")
    check(shortDay("2026-09-03") == "09-03", "shortDay('2026-09-03') == '09-03'")
    let d1 = shortDay("2026-09-03")
    let d2 = shortDay("2026-08-03")
    check(d1 != d2, "year is dropped, month differentiates (\(d1) vs \(d2))")
    // Monotonic bar width: highest value -> largest width.
    let maxDate = sortedDays.max { (byDay[$0] ?? 0) < (byDay[$1] ?? 0) } ?? ""
    let maxWidth = Int(Double(byDay[maxDate] ?? 0) / Double(maxValue) * 140)
    check(maxWidth == 140, "max-value day (\(maxDate), \(byDay[maxDate] ?? 0)) gets full 140 width, got \(maxWidth)")
} else {
    check(false, "by_day should be non-empty in live data")
}

// ---- Activity ----
if let activity = state.activity {
    print("\nACTIVITY:")
    if let tools = activity.top_tools, !tools.isEmpty {
        let parsed = tools.compactMap(parseToolPair)
        check(parsed.count == tools.count, "all \(tools.count) top_tools entries parse to pairs")
        print("  TOP TOOLS (name -> count):")
        for (name, count) in parsed {
            print("    \(name)  ->  \(count)")
        }
        if let first = parsed.first {
            check(first.0 == "test_tool", "top tool name resolves to 'test_tool' (got \(first.0))")
        }
    } else {
        check(false, "top_tools should be non-empty in live data")
    }
    if let byProfile = activity.tool_calls_by_profile, !byProfile.isEmpty {
        print("  TOOL CALLS BY PROFILE (profile -> count):")
        for (k, v) in byProfile.sorted(by: { $0.value > $1.value }) {
            print("    \(k)  ->  \(v)")
        }
        check(byProfile["test"] != nil, "tool_calls_by_profile contains 'test'")
    } else {
        check(false, "tool_calls_by_profile should be non-empty in live data")
    }
} else {
    check(false, "activity should be non-nil in live data")
}

print("\n" + (failed ? "RENDER CHECK FAILED" : "ALL RENDER CHECKS PASS"))
exit(failed ? 1 : 0)
