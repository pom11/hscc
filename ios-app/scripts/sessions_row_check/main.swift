import Foundation

// sessions_row_check/main.swift — decode ONE real live sessions capture and
// print the exact computed fields SessionsView renders, so RENDER claims are
// grounded in executed proof, not reasoning.
//
// Compiled with the real model sources by the inline script below.
let path = CommandLine.arguments.count > 1
    ? CommandLine.arguments[1]
    : "scripts/live_captures/v1_sessions.json"

let data = try Data(contentsOf: URL(fileURLWithPath: path))
let resp = try JSONDecoder().decode(SessionsListResponse.self, from: data)

print("ENVELOPE profile=\(resp.profile ?? "nil")")
print("ENVELOPE server count=\(resp.count.map(String.init) ?? "nil") bloated_count=\(resp.bloated_count.map(String.init) ?? "nil")")
print("ENVELOPE speak=\(resp.speak)")
let items = resp.sessions ?? []
print("CLIENT-RENDERED rows (sessions.count)=\(items.count)")
print("")
print("FIRST 3 ROWS as the view renders them:")
for (i, s) in items.prefix(3).enumerated() {
    print("  row \(i):")
    print("    displayTitle=\(s.displayTitle)")
    print("    message_count=\(s.message_count.map(String.init) ?? "nil")")
    print("    tokenSummary=\(s.tokenSummary)  (total_tokens=\(s.total_tokens.map(String.init) ?? "nil"))")
    print("    compaction_headroom=\(s.compaction_headroom.map(String.init) ?? "nil")")
    print("    isBloated=\(s.isBloated)  reason=\(s.reason ?? "nil")")
}
// Count check: client-rendered rows vs server count
let rowCountMatches = items.count == (resp.count ?? -1)
print("")
print("CLIENT rows == SERVER count? \(rowCountMatches)")
// Any row with title? (affects displayTitle readability)
let withTitle = items.filter { $0.title != nil && !($0.title?.isEmpty ?? true) }.count
print("rows with a human title: \(withTitle)/\(items.count)")
// any bloated rows
let bloated = items.filter { $0.isBloated }
print("client-bloated rows: \(bloated.count) (envelope bloated_count=\(resp.bloated_count.map(String.init) ?? "nil"))")
