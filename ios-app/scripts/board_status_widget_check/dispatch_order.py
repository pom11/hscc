#!/usr/bin/env python3
"""dispatch_order.py — the unconfigured-first invariant for BoardStatusWidget.

Audit-3's rule (t_5c554c5b), enforced for the cluster widget by
widget_view_dispatch_check.sh and here for the board status widget's six family
bodies: an app with no host/token must invite setup, and must never render
"can't reach the cluster" or a stale pane. In source terms, inside every family
body the FIRST thing that happens is the `!configured` check — no read of the
projection, the failure or the age may be evaluated before it.

This is checked structurally rather than executed because the bodies are SwiftUI
views in a file that imports WidgetKit + AppIntents: the macOS SDK marks the
accessory families unavailable, so nothing headless can render them. It dies by
NAME when a body disappears — a renamed view must not silently shrink the table
while the script still prints PASS.

Usage: dispatch_order.py <path to BoardStatusWidget.swift>
"""
import re
import sys

# (what to look for, its start marker, the token that means "configured was
# already handled", the tokens that mean "a data read")
BODIES = [
    ("accessoryInline (BoardStatusEntry.inlineLine)", r"^\s*var inlineLine: String \{", r"!configured"),
    ("accessoryCircular", r"^struct BoardStatusCircularView: View \{", r"!entry\.configured"),
    ("accessoryRectangular", r"^struct BoardStatusRectangularView: View \{", r"!entry\.configured"),
    ("systemSmall", r"^struct BoardStatusSmallView: View \{", r"!entry\.configured"),
    ("systemMedium", r"^struct BoardStatusMediumView: View \{", r"!entry\.configured"),
    ("systemLarge", r"^struct BoardStatusLargeView: View \{", r"!entry\.configured"),
]

DATA_TOKENS = [
    "entry.projection", "projection", "entry.failure", "failure",
    "ageMinutes", "entry.tiles", "tiles", "circularNumber",
    "circularIsUrgent", "countsShortLine", "footerLine",
]


def body(lines, start_re):
    """The slice of the file from a struct/var header to its closing brace."""
    for i, line in enumerate(lines):
        if re.match(start_re, line):
            # A struct closes at a `}` in column 0; an indented computed
            # property closes at the first `    }`.
            closing = r"^}" if line.startswith("struct") else r"^\s{4}\}"
            for j in range(i + 1, len(lines)):
                if re.match(closing, lines[j]):
                    return i, j, lines[i:j]
            return i, len(lines), lines[i:]
    return None


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
    except OSError as exc:
        print("error: cannot read %s (%s)" % (sys.argv[1], exc))
        return 1

    rc = 0
    for name, start_re, guard_re in BODIES:
        found = body(lines, start_re)
        if found is None:
            print("FAIL: body '%s' is gone from %s — the invariant is no longer "
                  "provable for this family (rename it here in the same commit)"
                  % (name, sys.argv[1]))
            rc = 1
            continue
        start, end, chunk = found
        guard = next((k for k, l in enumerate(chunk) if re.search(guard_re, l)), None)
        if guard is None:
            print("FAIL: body '%s' (line %d) never checks '%s' — an unconfigured "
                  "app would render a data state here" % (name, start + 1, guard_re))
            rc = 1
            continue
        first_data = None
        first_token = None
        for k, line in enumerate(chunk):
            for token in DATA_TOKENS:
                if token in line and k > guard:
                    if first_data is None:
                        first_data, first_token = k, token
                    break
            # the guard side may name `configured` only; data tokens there are fine
            if first_data is not None:
                break
        # A data token BEFORE the guard is the regression this exists for.
        before = [(k, token) for k, line in enumerate(chunk[:guard])
                  for token in DATA_TOKENS if token in line]
        if before:
            k, token = before[0]
            print("FAIL: body '%s' reads '%s' (line %d) BEFORE checking '%s' — "
                  "the setup invite must win over every data state"
                  % (name, token, start + k + 1, guard_re))
            rc = 1
        else:
            where = ("data first seen at line %d ('%s')" % (start + first_data + 1, first_token)
                     if first_data is not None else "no data read at all")
            print("  ok: %-38s guard at line %d, %s" % (name, start + guard + 1, where))
    return rc


if __name__ == "__main__":
    sys.exit(main())
