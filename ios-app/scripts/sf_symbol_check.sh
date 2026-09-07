#!/usr/bin/env bash
# Fail if any SF Symbol name used in the app does not resolve.
#
# The device console reports these only as a runtime line —
#   "No symbol named 'broom' found in system symbol set"
# — and the view silently renders blank, so a typo ships unnoticed.
# Validation is against the HOST system symbol set via NSImage; that set is
# not byte-identical to the iOS one, but it catches names that exist nowhere,
# which is the failure this guards.
set -euo pipefail
cd "$(dirname "$0")/.."
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

{ grep -rhoE 'systemImage: *"[^"]+"' Sources/ | sed -E 's/.*"(.*)"/\1/'
  grep -rhoE 'systemName: *"[^"]+"' Sources/ | sed -E 's/.*"(.*)"/\1/'; } \
  | sort -u > "$WORK/syms.txt"

COUNT=$(wc -l < "$WORK/syms.txt" | tr -d ' ')
if [ "$COUNT" -eq 0 ]; then
    echo "FAIL: extracted 0 symbol names — the grep patterns have rotted"
    exit 1
fi

cat > "$WORK/check.swift" <<'SWIFT'
import AppKit
import Foundation
let names = (try! String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8))
    .split(separator: "\n").map(String.init)
var bad: [String] = []
for n in names where !n.isEmpty {
    if NSImage(systemSymbolName: n, accessibilityDescription: nil) == nil { bad.append(n) }
}
if bad.isEmpty { print("PASS: all \(names.count) SF Symbols resolve") }
else {
    print("FAIL: \(bad.count) SF Symbol(s) do not resolve:")
    bad.forEach { print("  \($0)") }
    exit(1)
}
SWIFT

swift "$WORK/check.swift" "$WORK/syms.txt"
