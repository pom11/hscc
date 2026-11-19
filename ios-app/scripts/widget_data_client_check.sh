#!/bin/bash
# widget_data_client_check.sh — prove the extension client does what the pivot
# says it does, by EXECUTING the committed source.
#
# Why this exists: `Sources/Shared/WidgetDataClient.swift` is the only path a
# widget or Live Activity has to the cluster, and three of its properties are the
# kind that compile perfectly and lie anyway:
#
#   * READ-ONLY. Nothing in the type system says "GET". The allowlist
#     (`WidgetDataPath.all`) and the single `httpMethod` literal are what keep an
#     extension from ever firing a mutation, and a widget card that "just adds one
#     more path" breaks it silently — so the gate owns the exact audited set and
#     the refusal is proven to build no request at all.
#   * TIMEOUT. An extension that overruns its own budget is killed without a
#     message, so the ceiling must be on the request, not inherited from
#     URLSession's 60 s default.
#   * FAILURE TYPES + stale-while-error. `nil` cannot distinguish "not
#     configured" from "cluster down" from "answered and unreadable", and those
#     three need different widgets. The rule that an UNCONFIGURED app must never
#     render as stale data (fix t_5c554c5b) lives in `showsStaleData`, and this
#     harness asserts it with a real snapshot present — the exact case that used
#     to be re-derived per widget.
#
# What it compiles (all real, nothing redeclared):
#   Sources/Shared/WidgetDataClient.swift   (subject)
#   Sources/Shared/SharedModels.swift       (the payload vocabulary)
#   Sources/Shared/SharedStore.swift        (APIConfig — the shared settings read)
#   scripts/model_decode_check/ThemeStub.swift  (UI token shim, see that file)
# plus scripts/widget_data_client_check/main.swift, which drives the client
# through a stub URLProtocol. NO NETWORK is touched: every response is a fixture.
#
# Runs on macOS: the client is pure Foundation, and there is no iOS runtime here.
# The stub does not itself enforce `timeoutIntervalForRequest` (URLSession's job,
# not exercisable headlessly) — the harness proves the deadline is set on the
# request and that a timed-out load maps to `.timeout`.
#
# Usage: ios-app/scripts/widget_data_client_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

SUBJECT=Sources/Shared/WidgetDataClient.swift
[ -f "$SUBJECT" ] || { echo "error: $SUBJECT missing" >&2; exit 1; }

rc=0
report() {  # report <label> <grep-output>  — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}

echo "structural proofs on $SUBJECT"
# One method only: any `httpMethod = "…"` that is not GET, anywhere in the file.
report "GET is the only HTTP method" \
  "$(grep -nE 'httpMethod *= *"' "$SUBJECT" | grep -v '= *"GET*"')"
# No mutating verb even in prose, so a reader cannot mistake this for a writer.
report "no POST/PUT/PATCH/DELETE anywhere" \
  "$(grep -nE '\b(POST|PUT|PATCH|DELETE)\b' "$SUBJECT")"
# It reads shared settings; it must never WRITE shared state (§7).
report "no App Group or keychain writes" \
  "$(grep -nE 'UserDefaults\(|suiteName|AppGroup\.|SecItem(Add|Update|Delete|CopyMatching)' "$SUBJECT")"
# The read-only path allowlist must stay a closed set: this file's literal list
# and the harness's expected set have to be edited together, deliberately.
report "allowlist has one definition" \
  "$(grep -c 'static let all: Set<String>' "$SUBJECT" | awk '$1 != 1 {print $0}')"
# The compat alias the two pre-pivot providers rely on — present, and only one.
report "ExtensionClient alias present exactly once" \
  "$(grep -c 'typealias ExtensionClient = WidgetDataClient' "$SUBJECT" | awk '$1 != 1 {print $0}')"
# The old client must not come back alongside this one (two clients, two rules).
report "ExtensionClient.swift stayed deleted" \
  "$( [ -f Sources/Shared/ExtensionClient.swift ] && echo 'Sources/Shared/ExtensionClient.swift is back' )"

echo ""
echo "compiling the real client + real models + harness into a macOS CLI..."
real_sources=(
  Sources/Shared/WidgetDataClient.swift
  Sources/Shared/SharedModels.swift
  Sources/Shared/SharedStore.swift
  # The history trend projection compiles here too: its whole job is two
  # rules — a not-reported metric stays a gap, and a peak rides beside its
  # average — and a rule nobody compiles is a rule nobody keeps. It reads only
  # the canonical `GET /v1/cluster/history` answer (t_0c99b017), and its field
  # type IS the canonical metric vocabulary, so that file compiles here as
  # well: one vocabulary, executed by the gate that draws a series from it.
  Sources/Shared/ClusterHistorySeries.swift
  Sources/Shared/ClusterHistoryTrend.swift
  # The alert-feed projection (card R3): the widget and macOS dropdown share
  # this file, and the harness drives its order/filter/first-line rules through
  # the real client.
  Sources/Shared/NeedsMeReading.swift
  # The merge-queue projection (card MQ5): `mergeQueue()` returns
  # `MergeQueueLite`, declared here, so the client is not buildable without it.
  Sources/Shared/MergeQueueReading.swift
)
harness=(
  scripts/model_decode_check/ThemeStub.swift
  scripts/widget_data_client_check/main.swift
)
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/client_check" \
     "${real_sources[@]}" "${harness[@]}" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the real client sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
# Warnings are findings here: a deprecation or a Sendable complaint in the client
# is exactly the kind of drift that would otherwise land in Xcode.
if [ -s "$TMP/compile.err" ]; then
  echo "compiler diagnostics (treated as findings):" >&2
  cat "$TMP/compile.err" >&2
  rc=1
fi

echo "running the client against a stubbed transport..."
"$TMP/client_check" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "WIDGET DATA CLIENT CHECK PASSED — read-only by construction, deadline on the request, typed failures, one stale-while-error rule"
else
  echo "WIDGET DATA CLIENT CHECK FAILED — see above"
fi
exit "$rc"
