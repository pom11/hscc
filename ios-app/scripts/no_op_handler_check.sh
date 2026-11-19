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

# TODO/FIXME are markers of unfinished work, not words that may never appear.
# The kanban widget draws a "TODO" column label, so the marker search runs on
# the source with STRING LITERALS removed: a label is data. Comments and code
# are untouched, so `Text("TODO") // FIXME` still fails on the FIXME. This is a
# scanner rather than a grep exclusion because excluding the whole line would
# weaken the check to make one label pass.
todo_hits=$(python3 - "$SRC" <<'PY'
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1])
marker = re.compile(r'TODO|FIXME|unimplemented\(\)')

def strip_strings(line, state):
    """Return the line without string literals, plus the multiline state.

    state: 0 = code, 1 = inside a multiline string, 2 = inside a block comment.
    Comments are kept in full (including quote characters), because a marker
    in a comment is real unfinished work. Only string literals are removed.
    """
    out = []
    i = 0
    if state == 1:
        end = line.find('"""')
        if end < 0:
            return '', 1
        i = end + 3
        state = 0
    while i < len(line):
        if state == 2:
            end = line.find('*/', i)
            if end < 0:
                out.append(line[i:])
                return ''.join(out), 2
            out.append(line[i:end + 2])
            i = end + 2
            state = 0
            continue
        if line.startswith('//', i):
            out.append(line[i:])
            break
        if line.startswith('/*', i):
            out.append('/*')
            i += 2
            state = 2
            continue
        if line.startswith('"""', i):
            end = line.find('"""', i + 3)
            if end < 0:
                return ''.join(out), 1
            i = end + 3
            continue
        if line[i] == '"':
            i += 1
            while i < len(line):
                if line[i] == '\\':
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        out.append(line[i])
        i += 1
    return ''.join(out), state

hits = []
for path in sorted(root.rglob('*.swift')):
    state = 0
    for lineno, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        code, state = strip_strings(line, state)
        if marker.search(code):
            hits.append(f'{path}:{lineno}:{line}')
for hit in hits:
    print(hit)
sys.exit(1 if hits else 0)
PY
)
todo_rc=$?
report "no TODO/FIXME/unimplemented in sources" "$todo_hits"
if [ "$todo_rc" -ne 0 ] && [ -z "$todo_hits" ]; then
  echo "FAIL: the TODO scanner could not read $SRC" >&2
  rc=1
fi

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
