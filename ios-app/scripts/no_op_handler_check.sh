#!/bin/bash
# no_op_handler_check.sh — no user action may dead-end in a no-op.
#
# Why this exists: the Chat tab shipped with a relay that accepted the
# operator's message and silently discarded it. It compiled, the tests passed
# (they asserted the hook was CALLED, which a no-op satisfies), and the cluster
# sat idle while the operator waited. A user action that goes nowhere is the
# worst failure mode in this app because it is indistinguishable from working.
#
# This greps the app sources for the shapes that dead-end:
#   * an empty action closure on a Button / onTapGesture / swipe action
#   * TODO / FIXME / unimplemented() sitting in a user-reachable path
#   * a catch block that swallows an error with no state change and no comment
#
# Deliberately narrow: `try? await Task.sleep` and `_ = await (a, b)` (a
# concurrent-fetch join) are NOT dead ends and are excluded by pattern, not by
# a blanket ignore.
#
# Usage: ios-app/scripts/no_op_handler_check.sh
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
SRC=Sources
rc=0

report() {  # report <label> <grep-output>
  if [ -n "$2" ]; then
    echo "FAIL: $1"
    echo "$2" | sed 's/^/    /'
    rc=1
  else
    echo "  ok: $1"
  fi
}

# `Button("Cancel", role: .cancel) {}` is idiomatic: in an alert the dismissal
# IS the action, so an empty body is correct there and only there.
report "no empty action closures" \
  "$(grep -rnE 'action: *\{ *\}|onTapGesture *\{ *\}|Button\([^)]*\) *\{ *\}' \
      --include='*.swift' "$SRC" 2>/dev/null | grep -v 'role: *\.cancel')"

report "no TODO/FIXME/unimplemented in sources" \
  "$(grep -rnE 'TODO|FIXME|unimplemented\(\)' --include='*.swift' "$SRC" 2>/dev/null)"

# A bare `catch {}` or `catch { }` with nothing in it: the error vanishes.
report "no empty catch blocks" \
  "$(grep -rnE 'catch *\{ *\}' --include='*.swift' "$SRC" 2>/dev/null)"

echo ""
if [ "$rc" -eq 0 ]; then
  echo "NO-OP HANDLER CHECK PASSED — no user action dead-ends"
else
  echo "NO-OP HANDLER CHECK FAILED — see above"
fi
exit $rc
