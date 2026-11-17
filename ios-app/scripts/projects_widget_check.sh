#!/bin/bash
# projects_widget_check.sh — prove the projects widget (t_d739d163) keeps the
# four properties its card claims, by EXECUTING the committed widget file.
#
# Why this exists — each of these compiles perfectly and lies anyway:
#
#   * FAMILY DEPTHS. §5.1 gives small 3 rows, medium 6, large 8. A family that
#     quietly draws more is a truncated List on the Home Screen, discovered by the
#     operator. The table is on `ProjectsEntry.rowLimit(for:)` and is asserted here.
#   * UNCONFIGURED BEATS STALE (t_5c554c5b). The rule lives in
#     `WidgetDataFailure.showsStaleData` / `WidgetData.read`, and this harness
#     proves the widget reaches it with a REAL snapshot present — the exact case
#     where a hand-rolled fallback used to dress "no host configured" up as
#     "can't reach the cluster".
#   * THE SCOPE RULE. `scope(_:to:)` must return EMPTY for a selected project that
#     is gone. Falling back to the full list would make a deleted project look
#     live, which is the same class of lie as a zero that looks like a count.
#   * §7 STORAGE. `/v1/projects` carries `repo` — an absolute path on the
#     operator's machine — plus `speak`. The widget stores a projection, so the
#     harness decodes a payload that HAS both fields and then asserts the bytes
#     it writes contain neither, nor any absolute path.
#
# What it compiles (all real, nothing redeclared):
#   Sources/HSCCWidgets/ProjectsWidget.swift          (subject)
#   Sources/Shared/WidgetDataClient.swift             (WidgetData.read — the rule)
#   Sources/Shared/SharedModels.swift                 (ProjectsLite / ProjectLite)
#   Sources/Shared/SharedStore.swift                  (AppGroup, APIConfig)
#   scripts/projects_widget_check/ThemeStub.swift     (UI token shim — see file)
#   scripts/projects_widget_check/main.swift          (the assertions)
#
# NO NETWORK. `WidgetDataClient` is built only so `WidgetData.read` has a
# non-nil client for a closure that ignores it and returns a canned failure; the
# host is an unresolvable `.invalid` name and no request is ever built. The
# entity query's own options fetch is deliberately never called — it is the one
# path in the widget file that reads the cluster, and `widget_data_client_check.sh`
# owns that transport. There is no iOS runtime on this host: the views are
# asserted by mirroring their dispatch chain (as `widget_view_dispatch_check.sh`
# does) plus the greps below, which prove the real bodies still branch in that
# order. Nothing here is verified on a device.
#
# Usage: ios-app/scripts/projects_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SUBJECT=Sources/HSCCWidgets/ProjectsWidget.swift
[ -f "$SUBJECT" ] || { echo "error: $SUBJECT missing" >&2; exit 1; }

rc=0
report() {  # report <label> <grep-output>  — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}
count_is() {  # count_is <label> <file> <pattern> <want>
  local got
  got=$(grep -cE "$3" "$2" || true)
  if [ "$got" = "$4" ]; then
    echo "  ok: $1 ($got)"
  else
    echo "FAIL: $1 — got $got, want $4"; rc=1
  fi
}

echo "structural proofs on $SUBJECT"
# The widget reaches the cluster through the named read only: no route string of
# its own anywhere in the file (so it cannot widen the client's allowlist by
# inventing a path, and no other dataset can creep in through a literal).
report "no /v1/ route literal in the widget" \
  "$(grep -nE '"(/v1[^"]*)"' "$SUBJECT")"
# The typed outcome, not the RETIRE-era nil-on-failure shim.
report "no legacy nil-returning get() call" \
  "$(grep -nE '\.get\(' "$SUBJECT")"
report "no ExtensionClient alias use" \
  "$(grep -n 'ExtensionClient' "$SUBJECT")"
# One rule, two call sites (the timeline and the configuration sheet's options).
count_is "both reads go through WidgetData.read" "$SUBJECT" 'WidgetData\.read\(' 2
count_is "both reads build the client the same way" "$SUBJECT" 'WidgetDataClient\.makeIfConfigured\(\)' 2
# The unconfigured-first dispatch, in all three family bodies.
count_is "small+medium+large each check unconfigured first" "$SUBJECT" 'if entry\.isUnconfigured' 3
count_is "…then the failure branch, three times" "$SUBJECT" 'else if let failure = entry\.status\.failure' 3
count_is "…then the empty branch, three times" "$SUBJECT" 'else if entry\.rows\.isEmpty' 3
# §7: the App Group is touched on exactly two lines, and both are the projection
# store's own save/load — the response type is never written.
count_is "the App Group suite is touched twice, both in the store" "$SUBJECT" 'UserDefaults\(suiteName' 2
# `repo` must never become a field this file reads or writes.
report "repo is never a field" \
  "$(grep -nE '(let|var) repo|\.repo\b' "$SUBJECT")"
# One configuration, one widget, three families.
count_is "one configuration intent" "$SUBJECT" 'struct ProjectFilterIntent: WidgetConfigurationIntent' 1
count_is "one options provider" "$SUBJECT" 'struct ProjectEntityQuery: EntityQuery' 1
count_is "one widget, three families" "$SUBJECT" 'supportedFamilies\(\[\.systemSmall, \.systemMedium, \.systemLarge\]\)' 1
# The harness itself must not be able to reach the network either.
report "the harness never calls the options fetch" \
  "$(grep -n 'suggestedEntities\|allEntities\|entities(for' scripts/projects_widget_check/main.swift)"

echo ""
echo "compiling the real widget source + shared sources + harness into a macOS CLI..."
real_sources=(
  "$SUBJECT"
  Sources/Shared/WidgetDataClient.swift
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
)
harness=(
  scripts/projects_widget_check/ThemeStub.swift
  scripts/projects_widget_check/main.swift
)
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/projects_widget_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real widget sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
# Warnings are findings: a deprecation or Sendable complaint here is what the
# operator would otherwise meet in Xcode.
if [ -s "$TMP/compile.err" ]; then
  echo "compiler diagnostics (treated as findings):" >&2
  cat "$TMP/compile.err" >&2
  rc=1
fi

echo "running the widget's entry, status, scope and projection... (no network)"
"$TMP/projects_widget_check" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "PROJECTS WIDGET CHECK PASSED — §5.1 depths, unconfigured-beats-stale, no widen-on-miss, §7 projection carries no repo/prose"
else
  echo "PROJECTS WIDGET CHECK FAILED — see above"
fi
exit "$rc"
