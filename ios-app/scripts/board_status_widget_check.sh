#!/bin/bash
# board_status_widget_check.sh — execute the board status widget's count rules
# against the committed source. No network, no simulator.
#
# WHY THIS GATE EXISTS. `BoardStatusWidget.swift` imports WidgetKit + AppIntents,
# so `build_check.sh` only ever COMPILES it and nothing in scripts/ could ever run
# it — the combination under which a widget ships a confident zero. The live
# measurement that made this concrete: on 2026-11-17 `/v1/kanban/blocked` answered
# `count: 18` while `stale?older_than=0` answered 15 rows with no `blocked` status
# among them, so the interim path that §5.2's wording invites (histogram the stale
# list) would have drawn `blocked: 0` beside eighteen stuck cards. A rule like that
# is only worth anything with a gate on it.
#
# So the count rules live in `Sources/HSCCWidgets/BoardStatusReading.swift` —
# Foundation only, no View, no WidgetKit — and this script compiles THAT plus the
# real `SharedModels.swift` / `SharedStore.swift` / `WidgetDataClient.swift` and
# `model_decode_check/ThemeStub.swift`, then runs `board_status_widget_check/
# main.swift` against fixture payloads. The harness redeclares nothing (proved
# below, because a re-declared mirror is exactly the false green scripts/ has
# deleted twice).
#
# WHAT IT PROVES, by contract section:
#   §5.2   the interim never invents `done`; `openOnly` implies `done == nil`.
#   §6 r.1 `blocked` is never taken from the stale histogram — a stale payload
#          WITH `status: "blocked"` rows leaves the blocked number exactly where
#          the `kanban/blocked` route put it; an absent status draws `—` and
#          cannot be rendered as `0` (numberText, and the committed
#          `v1_mobile_summary_missing_done.json` capture failing its decode).
#          A list that sends fewer rows than its own `count` REFUSES the read
#          with a `.decoding` failure — which `showsStaleData`, so the last-known
#          snapshot stays on screen instead of a number low by construction.
#          A failed `kanban/blocked` read nils ONLY `blocked`.
#          `openCount` exists only when all four open operands do.
#   §7     `BoardStatusSnapshotStore` keys by board slug (a foreign slug gets
#          nothing), refuses a numbers-less projection, and stores the projection
#          and nothing else. Driven through the REAL App Group suite, which the
#          harness captures and restores (asserting the restore).
#   audit-3 the six family bodies check `!configured` before any data read —
#          structural, via `board_status_widget_check/dispatch_order.py`, because
#          a SwiftUI body in a WidgetKit file cannot be rendered headlessly.
#
# WHAT IT DOES NOT PROVE (say so rather than imply it): no pixels are rendered,
# so view layout, fonts and colour are out; the timeline provider's fetch order
# and the board picker are not driven (WidgetDataClient's own behaviour is
# `widget_data_client_check.sh`'s job); the 15-minute cadence and the Smart Stack
# recommendations are WidgetKit's decisions, not this host's. Nothing here runs on
# a device.
#
# Usage: ios-app/scripts/board_status_widget_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
SDK=$(xcrun --sdk macosx --show-sdk-path 2>/dev/null) || {
  echo "error: no macOS SDK (is Xcode installed?)" >&2; exit 1; }

# The shared client group carries NeedsMeReading.swift, which declares the
# AlertsLite the client's alerts() read returns. Declared once for every gate.
. scripts/swift_source_groups.sh

WIDGET=Sources/HSCCWidgets/BoardStatusWidget.swift
SUBJECT=Sources/HSCCWidgets/BoardStatusReading.swift
HARNESS=scripts/board_status_widget_check/main.swift
ORDER=scripts/board_status_widget_check/dispatch_order.py
FIXTURES=scripts/board_status_widget_check/fixtures
SHARED_FIXTURES=scripts/model_decode_check/fixtures

for f in "$WIDGET" "$SUBJECT" "$HARNESS" "$ORDER"; do
  [ -f "$f" ] || { echo "error: $f missing" >&2; exit 1; }
done
for f in stale_with_blocked_rows.json stale_truncated.json stale_no_tasks.json \
         blocked_two_boards.json blocked_truncated.json blocked_measured_18.json \
         blocked_no_tasks.json; do
  [ -f "$FIXTURES/$f" ] || { echo "error: fixture $FIXTURES/$f missing" >&2; exit 1; }
done
for f in v1_mobile_summary.json v1_mobile_summary_missing_done.json \
         v1_mobile_summary_degraded.json v1_mobile_summary_board_error.json; do
  [ -f "$SHARED_FIXTURES/$f" ] || { echo "error: shared fixture $SHARED_FIXTURES/$f missing" >&2; exit 1; }
done

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

rc=0
report() {  # report <label> <grep-output> — non-empty output is a failure
  if [ -n "${2:-}" ]; then
    echo "FAIL: $1"; echo "$2" | sed 's/^/    /'; rc=1
  else
    echo "  ok: $1"
  fi
}
expect() {  # expect <label> <want> <got>
  if [ "${3:-}" = "$2" ]; then echo "  ok: $1 ($3)"; else
    echo "FAIL: $1 — want $2, got ${3:-none}"; rc=1; fi
}

echo "structural proofs (the rules the macOS SDK cannot execute)"

# The interim histogram must have no `blocked` branch. Sliced to the init that
# owns it, because the word "blocked" appears all over this widget legitimately.
interim_slice=$(awk '/init\(interimStale/,/self\.done = nil/' "$SUBJECT")
expect "the interim init is still findable by its signature (slice non-empty)" "yes" \
  "$( [ -n "$interim_slice" ] && echo yes )"
report "the interim stale switch has NO blocked case (measured: stale does not list them)" \
  "$(printf '%s\n' "$interim_slice" | grep -nE 'case +"blocked"|blocked *\+= 1')"
report "the interim init sets done = nil (never a count)" \
  "$(printf '%s\n' "$interim_slice" | grep -q 'self\.done = nil' || echo 'no self.done = nil inside the interim init')"

# The two refusals, by their literal reason strings: the harness asserts the
# behaviour, this asserts a rewrite cannot silently delete the reason.
expect "the truncation refusal names its reason" "1" \
  "$(grep -c 'kanban/stale sent fewer rows than its count' "$SUBJECT")"
expect "the missing-tasks refusal names its reason" "1" \
  "$(grep -c 'kanban/stale carried no tasks array' "$SUBJECT")"

# Dash vs zero is ONE rule, and every view must go through it. Two dash
# literals in two view files is two chances to render a zero by accident, so the
# em dash may exist exactly once in the whole widget pair — inside numberText.
report "no view formats a number by hand (every dash goes through numberText)" \
  "$(grep -nF '.map { "\($0)" } ?? "—"' "$WIDGET")"
dash_in_subject=$(grep -cF '.map { "\($0)" } ?? "—"' "$SUBJECT")
expect "the em dash exists exactly once in the widget pair" "1" "$dash_in_subject"
expect "and that one place is numberText" "1" \
  "$(awk '/static func numberText/,/^    \}/' "$SUBJECT" | grep -cF '.map { "\($0)" } ?? "—"')"
count=$(grep -c 'BoardStatusReading\.numberText(' "$WIDGET")
if [ "${count:-0}" -ge 3 ]; then
  echo "  ok: the tile grid, the large rows and the circular dial all call numberText ($count)"
else
  echo "FAIL: only $count numberText call sites — a view is formatting a count again"; rc=1
fi

# The provider must still be the thing that calls the gated code; otherwise the
# harness is testing a function the widget stopped using.
expect "the timeline provider calls BoardStatusReading.interim" "1" \
  "$(grep -c 'BoardStatusReading\.interim(' "$WIDGET")"
expect "the tiles draw the status list this gate asserts" "1" \
  "$(grep -c 'static let statuses: \[(key: String, label: String)\] = BoardStatusReading\.statuses' "$WIDGET")"

# The subject must stay buildable by a Foundation-only harness.
report "the subject file imports Foundation only (no WidgetKit/SwiftUI/AppIntents)" \
  "$(grep -nE '^import +(SwiftUI|WidgetKit|AppIntents)' "$SUBJECT")"

# project.yml is the only place the gates see a file (check_sources.sh owns the
# both-directions disk check; this one is the per-target half of it).
listed=$(python3 -c "
import re
spec = open('project.yml').read().split(chr(10) + 'targets:' + chr(10), 1)[1]
blk = spec.split(chr(10) + '  HSCCWidgets:', 1)[1]
blk = re.split(chr(10) + '  [A-Za-z][A-Za-z0-9_]*:' + chr(10), blk, maxsplit=1)[0]
print(blk.count('Sources/HSCCWidgets/BoardStatusReading.swift')
      + blk.count('Sources/HSCCWidgets/BoardStatusWidget.swift'))" 2>/dev/null)
expect "both widget files are listed under the HSCCWidgets target" "2" "$listed"

# Audit-3's invariant, for the six family bodies.
if ! python3 "$ORDER" "$WIDGET"; then
  echo "FAIL: the unconfigured-first invariant regressed (see above)"; rc=1
fi

# Anti-mirror: the harness must not re-declare a subject type. If it does, every
# assertion below is testing a transcription instead of the app.
report "the harness declares none of the subject's types (no orphan mirror)" \
  "$(grep -nE '^(struct|enum|class|extension|typealias) +(BoardStatus|WidgetData|WidgetFamily|KanbanStaleLite|KanbanBlockedLite|MobileSummary|StaleCardLite|BlockedCardLite|SharedStore|AppGroup)' "$HARNESS")"

echo ""
echo "compiling the real widget rules + real models + harness into a macOS CLI..."
if ! xcrun --sdk "$SDK" swiftc -o "$TMP/board_status_widget_check" \
     "$SUBJECT" \
     "${SWIFT_GROUP_WIDGET_CLIENT[@]}" \
     scripts/model_decode_check/ThemeStub.swift \
     "$HARNESS" 2>"$TMP/compile.err"; then
  echo "error: failed to compile the widget's own sources — see below" >&2
  cat "$TMP/compile.err" >&2
  exit 1
fi
if [ -s "$TMP/compile.err" ]; then
  echo "compiler diagnostics (treated as findings):" >&2
  cat "$TMP/compile.err" >&2
  rc=1
fi

echo "running the count rules against the fixtures..."
"$TMP/board_status_widget_check" "$PWD/$FIXTURES" "$PWD/$SHARED_FIXTURES" || rc=1

echo ""
if [ "$rc" -eq 0 ]; then
  echo "BOARD STATUS WIDGET CHECK PASSED — the interim counts what its routes measured, an absent status cannot become zero, a truncated read refuses, and a snapshot never changes boards"
else
  echo "BOARD STATUS WIDGET CHECK FAILED — see above"
fi
exit "$rc"
