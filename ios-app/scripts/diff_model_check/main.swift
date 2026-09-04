import Foundation

// ===========================================================================
// diff_model_check/main.swift — DECODE a committed diff fixture against the REAL
// model sources, and prove the view-facing helpers (statusBadge, renderedText,
// typed predicates) behave. This is the compile-check for DiffDetailResponse.
//
// Compiled and run by scripts/diff_model_check.sh, which first compiles the
// actual model files from the repo:
//   Sources/HSCC/Models.swift
//   Sources/Shared/SharedModels.swift
//   Sources/HSCC/APIError.swift
//   Sources/HSCC/SessionEvent.swift
// plus the same Theme shim model_decode_check uses (UI design token, not a
// model). It NEVER redeclares a model here — the Decodable type resolved below
// IS the real struct in Models.swift, so a decode mismatch means the real
// model no longer matches the live endpoint JSON.
//
// This is the same faithful pattern as model_decode_check.sh, scoped to the
// diff model + fixture so it reads quickly and fails precisely when the diff
// contract drifts.
// ===========================================================================

let fixtureDir = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : FileManager.default.currentDirectoryPath + "/scripts/diff_model_check/fixtures"

var failures = 0

func fail(_ message: String) {
    failures += 1
    print("FAIL  \(message)")
}

// ---- 1. The real DiffDetailResponse decodes the committed fixture. ----
let fixtureURL = URL(fileURLWithPath: fixtureDir + "/v1_review_diff.json")
guard let data = try? Data(contentsOf: fixtureURL) else {
    print("FAIL  missing fixture: \(fixtureURL.path)")
    fatalError("no fixture")
}
let decoded: DiffDetailResponse
do {
    decoded = try JSONDecoder().decode(DiffDetailResponse.self, from: data)
    print("OK    v1_review_diff.json  →  DiffDetailResponse decodes")
} catch {
    print("FAIL  v1_review_diff.json  →  DiffDetailResponse: \(error)")
    fatalError("decode failed")
}

// ---- 2. Files intact (paths/status/counts/hunks). ----
let files = decoded.files ?? []
guard files.count == 5 else {
    fail("expected 5 files, got \(files.count)")
    fatalError("file count mismatch")
}
print("OK    \(files.count) files decoded")

let statuses = files.map(\.status)
guard statuses == ["M", "M", "A", "D", "M"] else {
    fail("status order wrong: \(statuses)")
    fatalError("status mismatch")
}
print("OK    status order A/M/D: \(statuses.compactMap { $0 }.joined(separator: ","))")

// ---- 3. View-facing helpers behave. ----
let added = files[2]   // A
let modified = files[0] // M
let deleted = files[3]  // D
guard added.statusBadge == "A+90" else {
    fail("added statusBadge expected A+90, got \(added.statusBadge)")
    fatalError("badge mismatch")
}
guard modified.statusBadge == "M+12-2" else {
    fail("modified statusBadge expected M+12-2, got \(modified.statusBadge)")
    fatalError("badge mismatch")
}
guard deleted.statusBadge == "D-5" else {
    fail("deleted statusBadge expected D-5, got \(deleted.statusBadge)")
    fatalError("badge mismatch")
}
print("OK    statusBadge: \(added.statusBadge) | \(modified.statusBadge) | \(deleted.statusBadge)")

// renderedText re-adds the marker per line type. Find the first of each kind
// rather than hard-code a line index (the fixture could reorder in future).
let addLine = files[0].hunks![0].lines!.first { $0.isAddition }!   // "+" line
let delLine = deleted.hunks![0].lines!.first { $0.isDeletion }!    // "-" line
let ctxLine = files[0].hunks![0].lines!.first { !$0.isAddition && !$0.isDeletion }! // context
guard addLine.isAddition, addLine.renderedText.hasPrefix("+") else {
    fail("addition line not flagged/rendered: \(addLine.renderedText)")
    fatalError("add line mismatch")
}
guard delLine.isDeletion, delLine.renderedText.hasPrefix("-") else {
    fail("deletion line not flagged/rendered: \(delLine.renderedText)")
    fatalError("del line mismatch")
}
guard !ctxLine.isAddition, !ctxLine.isDeletion, ctxLine.renderedText.hasPrefix(" ") else {
    fail("context line rendered with wrong marker: '\(ctxLine.renderedText)'")
    fatalError("ctx line mismatch")
}
print("OK    renderedText markers: + / - / (space)")

// Binary file with no hunks decodes (empty hunk body is legal).
let binary = files[4]
guard binary.hunks?.isEmpty == true else {
    fail("binary file expected empty hunks")
    fatalError("binary hunks mismatch")
}
print("OK    binary file (empty hunks) decodes")

// ---- 4. Top-level flags preserved. ----
if decoded.file_count != 5 { fail("file_count expected 5, got \(decoded.file_count ?? -1)"); fatalError("file_count mismatch") }
if decoded.truncated != true { fail("truncated expected true, got \(String(describing: decoded.truncated))"); fatalError("truncated mismatch") }
if decoded.total_lines_served != 38 { fail("total_lines_served expected 38, got \(decoded.total_lines_served ?? -1)"); fatalError("lines mismatch") }
if decoded.speak != "3 of 5 files shown (truncated — more available)." {
    fail("speak mismatch: \(decoded.speak)")
    fatalError("speak mismatch")
}
print("OK    flags: file_count=\(decoded.file_count ?? -1) truncated=\(decoded.truncated ?? false) total_lines_served=\(decoded.total_lines_served ?? -1)")

// ---- 5. Offline-none (query GETs not persisted) is a documented non-issue;
// nothing to assert here — the diff is read-once review data.

if failures == 0 {
    print("ALL PASS  — DiffDetailResponse decodes and its helpers behave.")
} else {
    print("\(failures) FAILURE(S)")
    exit(1)
}
