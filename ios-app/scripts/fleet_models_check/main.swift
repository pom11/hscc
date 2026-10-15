import Foundation

// ===========================================================================
// fleet_models_check/main.swift — exercise the REAL row derivation behind
// Fleet's read-only Models section (Sources/HSCC/FleetServing.swift, ruling
// t_1897550c / spec §5 as amended by PR #29).
//
// Why this harness exists: the Models section renders strings DERIVED from
// the `/v1/cluster/status` payload — the model headline trimmed from the
// recipe path, the up/no-container state, the mono caption, and the one
// combined VoiceOver label the card demands. Those are exactly the decisions
// that look fine in review and then (a) print a personal home directory as a
// headline, (b) hand `ForEach` duplicate ids — the feed repeats one recipe
// name per container, and `ClusterWorkload.id` is just the name — or (c) let
// the spoken label drift from what is on screen. The functions are compiled
// VERBATIM (never redeclared here) and asserted against the committed live
// capture plus synthetic edges.
//
// Compiled by scripts/fleet_models_check.sh together with the real model
// sources (Models.swift, SharedModels.swift, APIError.swift) — the same
// "compile the real thing, never mirror it" rule as serving_placement_check.sh.
// ===========================================================================

var failures = 0
var checks = 0

func expect(_ condition: Bool, _ what: String) {
    checks += 1
    if condition {
        print("OK   \(what)")
    } else {
        failures += 1
        print("FAIL \(what)")
    }
}

func workload(_ name: String, tp: String?, pp: String?, cid: String?) -> ClusterWorkload {
    ClusterWorkload(name: name, tp: tp, pp: pp, container_id: cid)
}

let fixDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "scripts/model_decode_check/fixtures"

// ---------------------------------------------------------------------------
// 1. The committed LIVE capture decodes with the real model and yields the
//    rows the section renders.
// ---------------------------------------------------------------------------
let captureURL = URL(fileURLWithPath: fixDir).appendingPathComponent("v1_cluster_status.json")
guard let data = try? Data(contentsOf: captureURL),
      let capture = try? JSONDecoder().decode(ClusterStatusResponse.self, from: data) else {
    print("FAIL decode \(captureURL.path) with the REAL ClusterStatusResponse")
    exit(1)
}
print("capture: \(captureURL.path) — \(capture.workloads.count) workloads")

let rows = FleetServing.rows(from: capture.workloads)
expect(rows.count == capture.workloads.count, "one row per workload (\(capture.workloads.count))")
expect(Set(rows.map(\.id)).count == rows.count,
       "row ids are UNIQUE for the live capture (the duplicate-id bug this gate exists for)")
// The capture is three replicas of ONE recipe (same name, three containers).
expect(Set(capture.workloads.map(\.name)).count == 1,
       "capture really does repeat one name three times (the ForEach hazard is real, not hypothetical)")

let expectedModel = "deepseek-v4-fp8-scitrera-hscc"
for row in rows {
    expect(row.model == expectedModel,
           "headline trims the recipe path to '\(expectedModel)' (got '\(row.model)')")
    expect(!row.model.contains("/") && !row.model.lowercased().contains(".yaml"),
           "headline '\(row.model)' carries no path and no file extension")
    expect(row.state == .up, "workload with a container id reads as .up")
    expect(row.chipText == "up", "chip text for .up is 'up' (never 'waking')")
    expect(row.caption.hasPrefix("tp=2 · pp=1 · "),
           "caption carries the same tp/pp the serving screen shows (\(row.caption))")
    expect(row.accessibilityLabel == "\(row.model), \(row.caption), up",
           "VoiceOver label = '<model>, <caption>, <state>' in reading order")
}
expect(rows[0].accessibilityLabel != "", "VoiceOver label is non-empty")
// (Feed ORDER is asserted against a synthetic feed in section 4 below, where
//  alphabetical order and feed order actually differ.)

// 12-char container heads (docker-style) rather than the raw 29-char id.
expect(rows[0].caption.contains("9a0467d69586"),
       "caption shows the 12-char container head (\(rows[0].caption))")
expect(!rows[0].caption.contains("_"),
       "caption shows the FIRST id segment (no '_<second segment>' noise)")

// Header count derives from the rows.
expect(FleetServing.header(workloadCount: rows.count) == "Models · \(rows.count) serving",
       "header is the mockup's 'Models · N serving' from the row count")
expect(FleetServing.header(workloadCount: 1) == "Models · 1 serving",
       "header is not pluralised wrong for one model")

// ---------------------------------------------------------------------------
// 2. modelDisplayName — every shape the engine's parser can emit.
//    (hscc-cluster/hscc.py takes the first token of the `Job:` line: normally
//    a recipe path, "?" never reaches `name`.)
// ---------------------------------------------------------------------------
expect(FleetServing.modelDisplayName("/home/devuser/.sparkrun-local/recipes/local-fixed/qwen3.8-max-fp8-hscc.yaml")
        == "qwen3.8-max-fp8-hscc",
       "path + .yaml -> stem (live shape)")
expect(FleetServing.modelDisplayName("qwen3-30b") == "qwen3-30b",
       "bare name passes through untouched")
expect(FleetServing.modelDisplayName("nested/dir/embed-8b.yml") == "embed-8b",
       ".yml trims too")
expect(FleetServing.modelDisplayName("Recipes/Whisper.LARGE.YAML") == "Whisper.LARGE",
       "extension match is case-insensitive; the stem's own dots survive")
expect(FleetServing.modelDisplayName("whisper-large.pt") == "whisper-large.pt",
       "a non-yaml extension is NOT trimmed (a name is rewritten only for the recipe extension)")
expect(FleetServing.modelDisplayName(".yaml") == ".yaml",
       "a stem-less '.yaml' still shows something (never an empty headline)")
expect(FleetServing.modelDisplayName("") == "",
       "empty name passes through (no crash); the row is then honestly empty")
expect(!FleetServing.modelDisplayName("/home/devuser/.sparkrun-local/recipes/local-fixed/qwen3.8-max-fp8-hscc.yaml")
        .contains("devuser"),
       "the live-shaped path leaves NO home directory on screen")

// ---------------------------------------------------------------------------
// 3. State + caption edges — what the payload can and cannot prove.
// ---------------------------------------------------------------------------
expect(FleetServing.state(of: workload("m", tp: "1", pp: "1", cid: "?")) == .noContainer,
       "container '?' (the engine's placeholder) is NOT 'up'")
expect(FleetServing.state(of: workload("m", tp: "1", pp: "1", cid: nil)) == .noContainer,
       "absent container id is 'no container', never guessed 'up'")
expect(FleetServing.state(of: workload("m", tp: "1", pp: "1", cid: "")) == .noContainer,
       "empty container id is 'no container'")
expect(FleetServing.state(of: workload("m", tp: "1", pp: "1", cid: "abc123")) == .up,
       "any real container id is 'up'")

let noPar = workload("bare-model", tp: nil, pp: nil, cid: "?")
expect(FleetServing.caption(for: noPar) == "parallelism unknown",
       "unknown tp/pp reads as words, not '--' or '??' (\(FleetServing.caption(for: noPar)))")
expect(FleetServing.caption(for: workload("bare-model", tp: "2", pp: nil, cid: nil)) == "tp=2 · pp=?",
       "one known parallelism field still shows")
expect(FleetServing.caption(for: workload("bare-model", tp: "1", pp: "1", cid: "?")) == "tp=1 · pp=1",
       "no trailing separator when the container is missing")
expect(FleetServing.caption(for: workload("bare-model", tp: "1", pp: "1", cid: "0123456789abcdef_0123456789ab"))
        == "tp=1 · pp=1 · 0123456789ab",
       "caption = 'tp · pp · <12-char container head>'")

let unlabelled = FleetServing.Row(workload: noPar, id: "x")
expect(unlabelled.accessibilityLabel == "bare-model, parallelism unknown, no container",
       "combined VoiceOver label covers the unknown+noContainer case (\(unlabelled.accessibilityLabel))")

// ---------------------------------------------------------------------------
// 4. rows(from:) uniqueness under adversarial inputs — id duplication is the
//    class of bug where rows silently vanish or double on refresh.
// ---------------------------------------------------------------------------
let dupPair = [workload("same", tp: "1", pp: "1", cid: "samecid"),
               workload("same", tp: "1", pp: "1", cid: "samecid")]
let dupRows = FleetServing.rows(from: dupPair)
expect(Set(dupRows.map(\.id)).count == 2,
       "two IDENTICAL (name, container) pairs still get unique ids (\(dupRows.map(\.id)))")
expect(dupRows.map(\.model) == ["same", "same"], "dedup does not rename or drop rows")

let nilCids = [workload("a", tp: nil, pp: nil, cid: nil),
               workload("a", tp: nil, pp: nil, cid: nil),
               workload("a", tp: nil, pp: nil, cid: nil)]
expect(Set(FleetServing.rows(from: nilCids).map(\.id)).count == 3,
       "three identical name+nil-container workloads get three unique ids")
expect(FleetServing.rows(from: []).isEmpty, "empty feed yields no rows (HSEmpty path)")

// Order must follow the feed exactly (a re-sort would break the operator's
// muscle memory against the serving screen).
let ordered = [workload("z", tp: nil, pp: nil, cid: "1"),
               workload("a", tp: nil, pp: nil, cid: "2"),
               workload("m", tp: nil, pp: nil, cid: "3")]
expect(FleetServing.rows(from: ordered).map(\.model) == ["z", "a", "m"],
       "rows keep feed order")

print("")
print("\(checks - failures)/\(checks) assertions passed")
exit(failures == 0 ? 0 : 1)
