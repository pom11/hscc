#!/usr/bin/env bash
# Decide which pytest legs a workflow run must execute (t_23bf61f3).
#
# This is the shell half of the gate: the workflow's `select` job runs it, and it
# writes `legs=`, `leg_count=`, `matrix=` and `full_suite=` to $GITHUB_OUTPUT.
# It lives here, not inline in the YAML, because "push to main runs everything"
# has to be executable in a test — the previous draft of this gate said that in a
# comment while the code diffed `github.event.before..github.sha` on push too, so
# the backstop it claimed did not exist.
#
# CONTRACT:
#   push (workflow only subscribes to main)  -> the FULL roster
#   pull_request                            -> the legs the diff can break
#   workflow_dispatch with `legs` input      -> exactly that input
#   workflow_dispatch without input          -> the FULL roster
#   unreadable / vanished diff base           -> the FULL roster (fail closed)
#
# The roster is never copied here: leg_select.py reads DIRS out of
# scripts/run_tests.sh, and scripts/run_tests.sh validates every name again
# (unknown name = exit 64 before any leg runs), so a typo cannot report green.
set -uo pipefail

REPO="${DECIDE_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
DIFF_REPO="${DECIDE_DIFF_REPO:-$REPO}"
SELECT="${REPO}/.github/scripts/leg_select.py"
OUT="${GITHUB_OUTPUT:-}"
EVENT="${EVENT:-}"
INPUT_LEGS="${INPUT_LEGS:-}"

if [ -z "$OUT" ]; then
  echo "decide_legs.sh: GITHUB_OUTPUT is not set (this runs as a workflow step)" >&2
  exit 2
fi
if [ ! -f "$SELECT" ]; then
  echo "decide_legs.sh: missing $SELECT" >&2
  exit 2
fi

full_roster() {
  local reason="$1"
  python3 "$SELECT" --all --github-output "$OUT" || exit 2
  printf 'reason=%s\n' "$reason" >>"$OUT"
  echo "legs: FULL SUITE — reason: ${reason}"
}

forced_selection() {
  LEGS_INPUT="${1//,/ }" SELECTOR="$SELECT" python3 - "$OUT" <<'PY' || exit 2
import os
import sys

sys.path.insert(0, os.path.dirname(os.environ["SELECTOR"]))
import leg_select

seen: set[str] = set()
legs = [
    leg
    for leg in os.environ["LEGS_INPUT"].split()
    if not (leg in seen or seen.add(leg))
]
if not legs:
    print("decide_legs: the legs input is empty", file=sys.stderr)
    raise SystemExit(2)
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(f"legs={' '.join(legs)}\n")
    fh.write(f"leg_count={len(legs)}\n")
    fh.write(f"matrix={leg_select.legs_json(legs)}\n")
    fh.write("full_suite=false\n")
    fh.write("reason=dispatch-input\n")
PY
  echo "legs: FORCED by workflow_dispatch input -> ${1}"
}

# A dispatch input is a deliberate operator override and wins over the event.
if [ -n "${INPUT_LEGS// /}" ]; then
  forced_selection "$INPUT_LEGS"
  exit 0
fi

if [ "$EVENT" = "pull_request" ]; then
  base="${BASE_SHA:-}"
  head="${PR_HEAD_SHA:-}"
  zero="0000000000000000000000000000000000000000"
  if [ -z "$base" ] || [ -z "$head" ] || [ "$base" = "$zero" ] \
     || ! git -C "$DIFF_REPO" cat-file -e "${base}^{commit}" 2>/dev/null \
     || ! git -C "$DIFF_REPO" cat-file -e "${head}^{commit}" 2>/dev/null; then
    # Same fail-closed rule as the map: not knowing what changed runs everything.
    full_roster "no-diffable-base"
    exit 0
  fi

  changed="$(mktemp "${RUNNER_TEMP:-/tmp}/changed.XXXXXX")" || exit 2
  trap 'rm -f "$changed"' EXIT
  # THREE dots: diff from the merge base, so main's newer commits are not
  # reversed into the PR's changed-file list (which would over-select forever on
  # a stale branch) and the PR's own changes are all that is considered.
  if ! git -C "$DIFF_REPO" diff --name-only "${base}...${head}" >"$changed"; then
    full_roster "diff-failed"
    exit 0
  fi
  echo "changed files:"
  sed 's/^/  /' "$changed"
  python3 "$SELECT" --paths-file "$changed" --github-output "$OUT" || exit 2
  printf 'reason=map\n' >>"$OUT"
  exit 0
fi

# push (main only, by the workflow's trigger) and a bare dispatch.
full_roster "full-suite-event:${EVENT:-unknown}"
