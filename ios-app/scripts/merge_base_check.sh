#!/bin/bash
# merge_base_check.sh — fail a branch whose merge-base with main has drifted too
# far behind main to be reviewed safely.
#
# WHY THIS GATE EXISTS. The board-status-widget line died four times (#316, #317,
# #319, #335) and in three of them the failure was reported as something else.
# The cause was never content: it was a branch whose merge-base with `main`
# drifted 12-20 commits behind while GitHub kept answering
# `mergeable=MERGEABLE/CLEAN`. That badge is structurally blind to this failure,
# because re-adding a file `main` deleted is not a conflict: #319 (closed
# superseded, merge-base `52310791`) still carried 37 paths `main` had deleted on
# purpose — the console screens (`Sources/HSCC/ContentView.swift`,
# `Views/AppShell.swift`, `Views/Sidebar.swift`), `Shared/ExtensionClient.swift`,
# the retired `model_decode_check/fixtures/` payloads and the UITests. An author
# reading only the green badge rebases forever, and every rebase re-lands files
# the retirement work removed.
#
# WHAT IT MEASURES. `git rev-list --count <merge-base>..origin/main` — the number
# of commits `main` has that the branch's BASE lacks. That is the honest number
# for this failure: it is the set of changes the branch never saw, and it is
# exactly what `git rebase --onto origin/main <merge-base>` has to absorb, so the
# gate and the fix measure the same thing. Note the consequence of measuring the
# BASE rather than the content: a branch that absorbed main's commits by
# CHERRY-PICK (how this repo's operator lands work) keeps its old merge-base, so
# this reports drift it has already absorbed. That is deliberate — a rebase moves
# the merge-base and clears it, and the gate does not try to guess which absorbed
# commits are complete.
#
# IT NEVER FETCHES. A gate must be reproducible offline and take no network side
# effects, so it measures against the `origin/main` you already have. That makes
# a stale local fetch a false green, which is why every run prints the SHA of the
# base ref it used: if that SHA is not the `main` you think you are rebasing
# onto, run `git fetch origin main` and re-run before believing this.
#
# THRESHOLD: fails at STALE_BASE_FAIL_AT commits of drift (one named constant,
# nothing else to tune). Justification, measured on this repo 2026-11-17 against
# `main` = `3b5e475c` (re-run these to re-check the number):
#   * The three earliest deaths of the line — #316 (`45b16015`), #317
#     (`77b17984`), #319 (`223f863b`) — all carried the SAME merge-base
#     `52310791`, 16 commits behind. Three PRs rebasing forever onto a base they
#     never moved is the exact shape this gate exists to break, and a limit of 10
#     catches it with headroom.
#   * Healthy work sits at the other end: the two PRs open at that measurement
#     were both at ZERO drift, because a card that lands normally is cut, gated
#     and merged inside a day.
#   * `main` moves in bursts — 24 merges in the last 24 h against a 36-merge
#     week — so the limit means "one burst day of board velocity and you must
#     rebase", which is the condition that let whole-file retirements land under
#     a live branch. It is not 1: a hard fail at 1 would redden every long-lived
#     branch and teach people to ignore the gate.
#   * The knob is one line. If the fleet finds it too aggressive during a burst,
#     raise STALE_BASE_FAIL_AT; do not add per-branch exceptions.
# ADVISORY vs hard: HARD. A green `mergeable` already tells the author nothing
# here, so an advisory warning would be read as noise by the same author who
# shipped four superseded PRs. The escape hatch is a rebase, not an env var.
#
# WHAT IT DOES NOT PROVE (say so rather than imply it): it does not know whether
# the drift is *harmful* — 10 commits of doc typos are not 10 commits of file
# retirement, and #335 was superseded with its base only 5 behind, so drift is not
# the only way that line died. It proves you are far enough behind that a human
# must look before this is reviewed. It also cannot see the deleted-path symptom
# that actually killed #319: zero drift is not a claim that the branch tree
# matches `main`.
#
# Usage:  ios-app/scripts/merge_base_check.sh
#         MERGE_BASE_CHECK_BASE=<ref> ios-app/scripts/merge_base_check.sh
#                  (default `origin/main`; point it at another ref to test)
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

STALE_BASE_FAIL_AT=10
BASE_REF="${MERGE_BASE_CHECK_BASE:-origin/main}"

fail() {
  echo ""
  echo "MERGE BASE CHECK FAILED — $1"
  exit 1
}

git rev-parse --git-dir >/dev/null 2>&1 || fail "not inside a git repository"

git rev-parse --verify --quiet "$BASE_REF^{commit}" >/dev/null \
  || fail "base ref '$BASE_REF' does not resolve — run \`git fetch origin main\` (or set MERGE_BASE_CHECK_BASE)"

BASE_SHA=$(git rev-parse --short=8 "$BASE_REF^{commit}")
git rev-parse --verify --quiet "HEAD^{commit}" >/dev/null \
  || fail "HEAD does not resolve to a commit — an unborn branch (\`git checkout --orphan\` with no commit yet) has no merge-base with anything"
HEAD_SHA=$(git rev-parse --short=8 HEAD)

MERGE_BASE=$(git merge-base HEAD "$BASE_REF^{commit}" 2>/dev/null) \
  || fail "HEAD has no merge-base with $BASE_REF — the branch does not descend from it (orphan history or a shallow clone: \`git fetch --unshallow\`)"

SHORT_BASE=$(git rev-parse --short=8 "$MERGE_BASE")
DRIFT=$(git rev-list --count "$MERGE_BASE..$BASE_REF^{commit}")

echo "branch   : $HEAD_SHA"
echo "base ref : $BASE_REF @ $BASE_SHA"
echo "merge-base: $SHORT_BASE"
echo "drift    : $DRIFT commit(s) of $BASE_REF since the merge-base (fails at $STALE_BASE_FAIL_AT)"

if [ "$DRIFT" -ge "$STALE_BASE_FAIL_AT" ]; then
  echo ""
  echo "The branch's base '$SHORT_BASE' is $DRIFT commits behind $BASE_REF ($BASE_SHA)."
  echo "Rebase before review — a green 'mergeable' does NOT mean this is safe: re-adding"
  echo "files $BASE_REF deleted is not a conflict, and that is what killed #316/#317/#319/#335:"
  echo "    git rebase --onto $BASE_REF $SHORT_BASE <your-branch>   # GIT_EDITOR=true if it stops"
  fail "stale merge-base: $DRIFT commits (>= $STALE_BASE_FAIL_AT) since $SHORT_BASE"
fi

echo ""
echo "MERGE BASE CHECK PASSED — base $SHORT_BASE is $DRIFT commit(s) behind $BASE_REF ($BASE_SHA), under the $STALE_BASE_FAIL_AT limit"
exit 0
